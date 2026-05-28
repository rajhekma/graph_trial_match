# graph-trial-match

Clinical trial **patient matching** backend: convert eligibility criteria to structured rules (LLM), find matching patients in Neo4j, store results in MySQL, and visualize matches (PIR graphs).

Extracted from `hekma_data_pipleline` — includes **trial_matching** and **pir_visualization** only (no `disease_analysis`).

Related repos:

- [graph-db-service](https://github.com/rajhekma/graph-db-service) — disease graph explorer (`disease_analysis`)
- [hekma_data_pipleline](https://github.com/HekmaAI/hekma_data_pipleline) — full monolith (all three modules)

## Quick start

```powershell
git clone https://github.com/rajhekma/graph_trial_match.git
cd graph_trial_match
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
# edit .env with OpenAI, Neo4j, MySQL, EXTRACTOR_API_URL
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

**Docs:** http://127.0.0.1:8000/docs

## Pipeline

```
NCT / criteria text → trial_matching → Neo4j match → MySQL → pir_visualization graphs
```

## Documentation

See [TRIAL_MATCH_DOCUMENTATION.md](./TRIAL_MATCH_DOCUMENTATION.md) for full API reference, implementation details, and database schema.

## Project structure

```
graph_trial_match/
├── app.py                 # FastAPI entry point
├── trial_matching/         # LLM criteria parser + Neo4j matching engine
├── pir_visualization/      # PIR results graph APIs
├── db_writer.py            # MySQL read/write for match results
├── requirements.txt
└── TRIAL_MATCH_DOCUMENTATION.md
```
