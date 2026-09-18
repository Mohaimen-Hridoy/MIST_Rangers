# GridWise — BUP CSE Fest 2026 Preliminary

LLM-assisted energy scheduling and optimization API for the BUP CSE Fest 2026 hackathon preliminary.

## What it does

A deployed public HTTP API:

- `GET /health` → `{"status": "ok"}`
- `POST /optimize-energy` → interprets 1–3 operator notes via Google Gemini,
  validates the structured directives, runs a 24-hour OR-Tools optimization,
  replays the result against every energy rule, and returns the interpretation
  plus the cost-minimized schedule.

## Stack

| Layer    | Tech                                                              |
|----------|-------------------------------------------------------------------|
| API      | Python 3.11 · FastAPI · OR-Tools (CBC) · google-genai (Gemini) |
| Deploy   | Docker; Render Blueprint included                             |

## Repo layout

```
backend/         FastAPI service and Docker deployment files
samples/         Public-style sample request (gridwise_sample.json)
validate.py      Offline CLI validator (replays a response against its request)
```

## Run locally

### Local API

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Required for the judging path; never commit the key.
# PowerShell: $env:GEMINI_API_KEY = "your-key"
# Bash:       export GEMINI_API_KEY=your-key
uvicorn app.main:app --reload --port 8000
curl http://localhost:8000/health
curl -s -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @../samples/gridwise_sample.json | tee /tmp/resp.json
python ../validate.py /tmp/resp.json ../samples/gridwise_sample.json
```

On Windows PowerShell, replace `tee /tmp/resp.json` with
`Tee-Object response.json`, then run
`python ..\validate.py response.json ..\samples\gridwise_sample.json`.

## Deploy

### Backend → Render

`backend/render.yaml` is a Blueprint deploy spec. Render auto-builds the
Dockerfile and starts `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

1. Push the repo to GitHub.
2. Create a new Render Blueprint from the repo.
3. In the Render dashboard, set the env var `GEMINI_API_KEY`.
4. Once live: `curl https://<service>.onrender.com/health` → `{"status":"ok"}`.

### Docker

```bash
docker build -t gridwise-optimizer:local ./backend
docker run --rm -p 8000:8000 -e GEMINI_API_KEY=your-key gridwise-optimizer:local
curl http://localhost:8000/health
```

The container binds to `0.0.0.0:8000`. Render can use
`backend/render.yaml`; set `GEMINI_API_KEY` as a secret environment variable.

## Notes for the judge harness

- The service matches the canonical Problem Statement contract (Sections 06, 07, 10).
- Gemini is used to interpret `operator_notes` into directive constraints. The
  response is then deterministically normalized and validated before it reaches
  the optimizer. If Gemini is temporarily unavailable, a conservative
  deterministic safe-failure interpreter keeps the API operational; production
  judging should configure `GEMINI_API_KEY`.
- OR-Tools CBC enforces energy balance, battery transitions, directive windows,
  reserve floors, grid caps, and end-of-day battery neutrality.
- The completed schedule is replayed before it is returned.
- Numeric tolerance: 0.01 kWh / 0.01 BDT (see `TOL` in `app/replay.py`).
- Malformed model output never reaches the optimizer; unsupported directives,
  invalid hours, and invalid numeric values fail closed.

Known limitation: without `GEMINI_API_KEY`, the service uses its documented
safe fallback interpreter. The mandatory LLM path is enabled when the key and
configured Gemini model are available.
