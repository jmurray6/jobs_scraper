# Jobs Scraper API

Simple FastAPI service that scrapes LinkedIn's public job search pages. Designed to be deployed on Railway and called from workflow tools like Make and Zapier.

## Stack

- **Python 3.11+**, FastAPI, httpx, BeautifulSoup4
- Managed with [Poetry](https://python-poetry.org/)

## Local development

```bash
poetry install
poetry run uvicorn main:app --reload
```

The API will be available at `http://localhost:8000`. Interactive docs at `/docs`.

## Deploy to Railway

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/jobs-scraper?referralCode=jguZ-u&utm_medium=integration&utm_source=template&utm_campaign=generic)

Railway reads the `Procfile` and runs:

```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

No extra configuration needed — just connect the repo and deploy.

## API

### `POST /jobs`

Searches LinkedIn for jobs matching the given keywords and search criteria, then enriches each result with detail-page data (description, seniority, salary, etc.).

**Request body**

```json
{
  "keywords": ["software engineer", "python"],
  "searches": [
    { "location": "San Francisco, CA", "work_type": "remote" },
    { "location": "New York, NY", "work_type": "" }
  ],
  "debug": false
}
```

| Field | Type | Description |
|---|---|---|
| `keywords` | `string[]` | Search terms joined into a single query |
| `searches` | `SearchCriteria[]` | One or more location/work-type combos to run |
| `debug` | `bool` | If `true`, includes a `logs` array with timestamped request traces |

**SearchCriteria**

| Field | Type | Description |
|---|---|---|
| `location` | `string` | City, region, or country (passed to LinkedIn's `location` param) |
| `work_type` | `string` | `"onsite"`, `"remote"`, `"hybrid"`, or `""` for any |

**Response**

```json
{
  "jobs": [
    {
      "title": "Software Engineer",
      "company": "Acme Corp",
      "location": "San Francisco, CA",
      "url": "https://www.linkedin.com/jobs/view/...",
      "work_types": ["remote"],
      "employment_type": "Full-time",
      "seniority_level": "Mid-Senior level",
      "industry": "Software Development",
      "salary": "$130,000/yr - $160,000/yr",
      "description": "..."
    }
  ],
  "logs": null
}
```

Jobs that hit a rate limit or auth wall during detail enrichment are still returned — they'll have `null` for `description` and detail fields.

## Behavior notes

- Fetches up to 3 pages (75 listings) per search criteria, filtered to the last 24 hours (`f_TPR=r86400`).
- Detail pages are fetched sequentially with a 0.75s delay to reduce rate limiting.
- Duplicate URLs across multiple search criteria are deduplicated before enrichment.
- Promoted/sponsored cards are filtered out (their URLs don't match the standard job URL pattern).
- LinkedIn will occasionally return auth walls as HTTP 200 — these are detected and the job is returned without detail fields.
