# graph-trial-match

Clinical trial **patient matching** backend: convert eligibility criteria to structured rules (LLM), find matching patients in Neo4j, store results in MySQL, and visualize matches (PIR graphs).

Extracted from `hekma_data_pipleline` — includes **trial_matching** and **pir_visualization** only (no `disease_analysis`).

Related repos:

- [graph-db-service](https://github.com/rajhekma/graph-db-service) — disease graph explorer (`disease_analysis`)
- [hekma_data_pipleline](https://github.com/HekmaAI/hekma_data_pipleline) — full monolith (all three modules)

## Quick start (clone & setup)

```powershell
git clone https://github.com/rajhekma/graph_trial_match.git
cd graph_trial_match
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` with at least:

| Variable | Used for |
|----------|----------|
| `OPENAI_API_KEY` | `/generate_json` (LLM) |
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASS` | Patient matching |
| `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASS`, `MYSQL_DB` | Match storage + PIR UI |
| `EXTRACTOR_API_URL` | `/generate_json` when using `nctCode` |

You can copy a working `.env` from `hekma_data_pipleline` if you already have one.

---

## Run backend and frontend

Use **two terminals**. Backend must stay running while you use the UI.

### Terminal 1 — Backend (FastAPI)

```powershell
cd C:\Users\ADMIN\Documents\graph_trial_match
.\.venv\Scripts\Activate.ps1
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

| Check | URL |
|-------|-----|
| Health | http://127.0.0.1:8000/health → `"neo4j_runner": true` |
| Swagger | http://127.0.0.1:8000/docs |

Leave this terminal open.

### Terminal 2 — Frontend (React + Vite)

```powershell
cd C:\Users\ADMIN\Documents\graph_trial_match\frontend
npm install
npm run dev
```

| Check | URL |
|-------|-----|
| PIR UI | http://localhost:5173 (or the URL Vite prints) |

Vite proxies `/api` → `http://localhost:8000` (see `frontend/vite.config.js`).

Leave this terminal open.

### Load data before using the UI (one-time per NCT)

The frontend reads **MySQL** after a matching run. In Postman or Swagger:

1. **POST** `/generate_json` — e.g. `{ "nctCode": "NCT05545020" }`
2. Copy the **full** JSON response
3. **POST** `/test_engine` — paste that JSON as the request body

Then in the UI:

1. Enter the same **NCT ID** (e.g. `NCT05545020`)
2. Click **Load**

More detail: [frontend/README.md](./frontend/README.md) · [code_implementation.md](./code_implementation.md)

### Troubleshooting

| Problem | Fix |
|---------|-----|
| `.venv` missing | `py -3.10 -m venv .venv` then `pip install -r requirements.txt` |
| Port 8000 in use | Stop other uvicorn, or use `--port 8001` and set `VITE_API_BASE=http://127.0.0.1:8001` in `frontend/.env.local` |
| `neo4j_runner: false` | Fix `NEO4J_*` in `.env`, restart backend |
| UI shows “No records” | Run `/test_engine` for that NCT first |
| `EXTRACTOR_API_URL not configured` | Add to `.env` or use `/generate_json` with `inclusion`/`exclusion` arrays instead of `nctCode` |
| Frontend API errors | Confirm backend is running on port **8000** |

## Pipeline

```
NCT / criteria text → trial_matching → Neo4j match → MySQL → pir_visualization graphs
```

## API tests (run manually)

```powershell
python tests/run_api_qa.py --suite smoke --mode http
```

Suites: `smoke` (fast), `pir`, `engine`, `full` (slow). Reports in `tests/reports/`.  
Guide: [tests/README.md](./tests/README.md)

## Documentation

- [code_implementation.md](./code_implementation.md) — end-to-end workflow with examples (how the code runs)
- [TRIAL_MATCH_DOCUMENTATION.md](./TRIAL_MATCH_DOCUMENTATION.md) — full API reference and database schema

## Project structure

```
graph_trial_match/
├── app.py                 # FastAPI entry point
├── trial_matching/        # LLM criteria parser + Neo4j matching engine
├── pir_visualization/     # PIR results graph APIs (backend)
├── frontend/              # React + Vite PIR UI
├── db_writer.py           # MySQL read/write for match results
├── requirements.txt
├── tests/                 # QA runner + reports
├── postman/               # Postman collection
└── TRIAL_MATCH_DOCUMENTATION.md
```
