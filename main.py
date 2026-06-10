import asyncio
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="LinkedIn Jobs Scraper")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_DELAY = 0.75      # seconds between each detail fetch (sequential)
DETAIL_RETRY_AFTER = 3   # seconds to wait before retrying a 429


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

WORK_TYPE_MAP = {
    "onsite": "1",
    "remote": "2",
    "hybrid": "3",
}


class SearchCriteria(BaseModel):
    location: str = ""
    work_type: str = ""  # "onsite", "remote", "hybrid", or ""


class JobRequest(BaseModel):
    keywords: list[str]
    searches: list[SearchCriteria]
    debug: bool = False


class Job(BaseModel):
    title: str
    company: str
    location: str
    url: str
    work_types: list[str] = []
    employment_type: str | None = None
    seniority_level: str | None = None
    industry: str | None = None
    salary: str | None = None
    description: str | None = None


class JobsResponse(BaseModel):
    jobs: list[Job]
    logs: list[str] | None = None


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3] + "Z"


class DebugLog:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.entries: list[str] = []

    def log(self, msg: str) -> None:
        entry = f"[{_ts()}] {msg}"
        if self.enabled:
            self.entries.append(entry)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

class _PartialJob(BaseModel):
    """Lightweight struct from the search listing — no description yet."""
    title: str
    company: str
    location: str
    url: str
    work_types: list[str] = []  # from the SearchCriteria that found this job


_LINKEDIN_JOB_URL = re.compile(
    r"https://www\.linkedin\.com/jobs/view/[^/]+-(\d+)/?$"
)


def _parse_search_page(html: str, work_type: str) -> list[_PartialJob]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[_PartialJob] = []
    for card in soup.find_all("div", class_="base-card"):
        title_el = card.find("h3", class_="base-search-card__title")
        company_el = card.find("h4", class_="base-search-card__subtitle")
        location_el = card.find("span", class_="job-search-card__location")
        link_el = card.find("a", class_="base-card__full-link")

        if not all([title_el, company_el, location_el, link_el]):
            continue

        url = link_el.get("href", "").split("?")[0]

        # Skip promoted/sponsored cards — their URLs are ad redirects, not real job pages
        if not _LINKEDIN_JOB_URL.match(url):
            continue

        jobs.append(
            _PartialJob(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True),
                location=location_el.get_text(strip=True),
                url=url,
                work_types=[work_type] if work_type else [],
            )
        )
    return jobs


_AUTH_WALL_SIGNALS = ("authwall", 'name="session_key"', "join-now", "sign-in-modal")


def _is_auth_wall(html: str, url: str) -> bool:
    """Return True if LinkedIn served a login/auth wall instead of the job page."""
    if "authwall" in url:
        return True
    lower = html[:4000].lower()
    return any(signal in lower for signal in _AUTH_WALL_SIGNALS)


def _parse_detail_page(html: str, partial: _PartialJob) -> Job:
    soup = BeautifulSoup(html, "html.parser")

    def _criteria(label: str) -> str | None:
        for item in soup.find_all("li", class_="description__job-criteria-item"):
            header = item.find("h3", class_="description__job-criteria-subheader")
            value = item.find("span", class_="description__job-criteria-text")
            if header and value and label.lower() in header.get_text(strip=True).lower():
                return value.get_text(strip=True)
        return None

    desc_el = (
        soup.find("div", class_="show-more-less-html__markup")
        or soup.find("div", class_="description__text")
        or soup.find("section", class_="description")
        or soup.find("div", attrs={"data-automation-id": "jobPostingDescription"})
    )
    if desc_el:
        raw = desc_el.get_text(separator="\n", strip=True)
        description: str | None = re.sub(r"\n{3,}", "\n\n", raw)
    else:
        description = None

    # Salary is inconsistently structured — try the criteria sidebar first,
    # then fall back to a dedicated compensation element some listings use
    salary = (
        _criteria("Base salary")
        or _criteria("Salary")
        or _criteria("Compensation")
    )
    if salary is None:
        comp_el = soup.find("div", class_="compensation__salary")
        if comp_el:
            salary = comp_el.get_text(strip=True)

    return Job(
        title=partial.title,
        company=partial.company,
        location=partial.location,
        url=partial.url,
        work_types=partial.work_types,
        employment_type=_criteria("Employment type"),
        seniority_level=_criteria("Seniority level"),
        industry=_criteria("Industries"),
        salary=salary,
        description=description,
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _run_search(
    client: httpx.AsyncClient,
    keyword: str,
    criteria: SearchCriteria,
    log: DebugLog,
) -> list[_PartialJob]:
    """Fetch up to 3 pages of search results for a single SearchCriteria."""
    params_base: dict[str, str] = {
        "keywords": keyword,
        "location": criteria.location,
        "f_TPR": "r86400",
    }
    if criteria.work_type:
        params_base["f_WT"] = WORK_TYPE_MAP[criteria.work_type]

    label = f"location={criteria.location!r} work_type={criteria.work_type!r}"
    log.log(f"SEARCH_START {label} f_WT={params_base.get('f_WT', 'none')}")

    partials: list[_PartialJob] = []
    for start in range(0, 75, 25):
        log.log(f"SEARCH page start={start} {label}")
        try:
            resp = await client.get(SEARCH_URL, params={**params_base, "start": start})
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Request failed: {exc}")

        log.log(f"SEARCH_RESULT start={start} status={resp.status_code} {label}")

        if resp.status_code == 429:
            log.log(f"SEARCH_RATE_LIMITED — aborting {label}")
            raise HTTPException(status_code=429, detail="LinkedIn rate-limited this request. Try again later.")
        if resp.status_code != 200:
            log.log(f"SEARCH_STOPPED status={resp.status_code} {label}")
            break

        page_jobs = _parse_search_page(resp.text, criteria.work_type or "")
        log.log(f"SEARCH_PARSED start={start} found={len(page_jobs)} {label}")
        if not page_jobs:
            break
        partials.extend(page_jobs)

    return partials



async def _fetch_detail(
    client: httpx.AsyncClient, partial: _PartialJob, log: DebugLog
) -> Job:
    for attempt in range(2):
        await asyncio.sleep(DETAIL_DELAY)
        try:
            resp = await client.get(partial.url)
        except httpx.RequestError as exc:
            log.log(f"NETWORK_ERROR url={partial.url} error={exc}")
            return Job(**partial.model_dump())

        log.log(f"DETAIL status={resp.status_code} attempt={attempt + 1} url={partial.url}")

        if resp.status_code == 200:
            if _is_auth_wall(resp.text, str(resp.url)):
                log.log(f"AUTH_WALL_200 — LinkedIn returned login page as 200 url={partial.url}")
                break
            job = _parse_detail_page(resp.text, partial)
            if job.description is None:
                log.log(f"PARSE_MISS — 200 OK but no description selector matched url={partial.url}")
            return job

        if resp.status_code == 429:
            if attempt == 0:
                log.log(f"RATE_LIMITED — backing off {DETAIL_RETRY_AFTER}s then retrying url={partial.url}")
                await asyncio.sleep(DETAIL_RETRY_AFTER)
                continue
            else:
                log.log(f"RATE_LIMITED_AGAIN — retry also 429, giving up url={partial.url}")
                break

        if resp.status_code in (302, 303, 401, 403):
            log.log(f"AUTH_WALL status={resp.status_code} url={partial.url} — LinkedIn requiring login, skipping")
        else:
            log.log(f"UNEXPECTED_STATUS status={resp.status_code} url={partial.url} — skipping")
        break

    log.log(f"DETAIL_FAILED — returning partial (no description) for: {partial.title} @ {partial.company}")
    return Job(**partial.model_dump())


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/jobs", response_model=JobsResponse)
async def scrape_jobs(body: JobRequest):
    log = DebugLog(enabled=body.debug)

    for criteria in body.searches:
        if criteria.work_type and criteria.work_type not in WORK_TYPE_MAP:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown work_type: {criteria.work_type!r}. Use: {list(WORK_TYPE_MAP)}",
            )

    keyword_query = " ".join(body.keywords)
    log.log(f"KEYWORDS query={keyword_query!r}")

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        # 1. Run each search criteria and collect all partials
        all_partials: list[_PartialJob] = []
        for i, criteria in enumerate(body.searches):
            log.log(f"SEARCH {i + 1}/{len(body.searches)} location={criteria.location!r} work_type={criteria.work_type!r}")
            partials = await _run_search(client, keyword_query, criteria, log)
            all_partials.extend(partials)

        # 2. Deduplicate across all searches by URL
        seen: set[str] = set()
        unique_partials: list[_PartialJob] = []
        for p in all_partials:
            if p.url not in seen:
                seen.add(p.url)
                unique_partials.append(p)

        log.log(f"DEDUP total={len(all_partials)} unique={len(unique_partials)}")

        # 3. Enrich each listing sequentially to avoid rate limiting
        jobs: list[Job] = []
        for p in unique_partials:
            jobs.append(await _fetch_detail(client, p, log))

    enriched = sum(1 for j in jobs if j.description is not None)
    log.log(f"DONE total={len(jobs)} with_description={enriched} without_description={len(jobs) - enriched}")

    return JobsResponse(
        jobs=jobs,
        logs=log.entries if body.debug else None,
    )
