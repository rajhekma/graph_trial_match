# Graph Trial Match — QA Test Plan

## Scope

End-to-end API validation for `graph_trial_match` at `http://127.0.0.1:8000`.

## Prerequisites

| Requirement | Purpose |
|---|---|
| Server running | `python -m uvicorn app:app --reload --port 8000` |
| `.env` configured | OpenAI, Neo4j, MySQL, `EXTRACTOR_API_URL` |
| Neo4j patient graph | `/test_engine` matching |
| MySQL tables | `patient_match_pir`, `model_prediction_pir` |

## Test cases

| ID | Endpoint | Method | Expected |
|---|---|---|---|
| TC-01 | `/health` | GET | 200, `status=ok`, `neo4j_runner=true` |
| TC-02 | `/api/health` | GET | 200, `status=ok` |
| TC-03 | `/generate_json` | POST | 200, `inclusion_criteria` + `exclusion_criteria` arrays |
| TC-04 | `/test_engine` | POST | 200, `status=success`, `patients`, `final_count` |
| TC-05 | `/test_engine?page=0` | POST | 200, `mode=pagination` |
| TC-06 | `/api/nct/{id}/results` | GET | 200, `nct_id`, `records` |
| TC-07 | `/api/nct/{id}/all_inclusions` | GET | 200, `nodes`, `edges` |
| TC-08 | `/api/nct/{id}/all_exclusions` | GET | 200, `nodes`, `edges` |
| TC-09 | `/api/nct/{id}/inclusion/0` | GET | 200, `criteria_index`, `patients` |
| TC-10 | `/api/nct/{id}/exclusion/0` | GET | 200 |
| TC-11 | `/api/expand/nodes` | POST | 200, array (or skip if no nodes) |
| TC-12 | `/generate_json` (NCT) | POST | 200 (optional, slow) |
| TC-13 | `/generate_and_run` | POST | 200, counts (optional, slow) |

## Run (manual)

See **[tests/README.md](README.md)** for step-by-step commands.

| Suite | Time | What it tests |
|---|---|---|
| `smoke` | ~2 s | `/health`, `/api/health` |
| `pir` | ~30 s | PIR APIs (needs prior `/test_engine`) |
| `engine` | minutes | `/test_engine` (+ pagination), use `--criteria-file` |
| `full` | 5–30+ min | Everything including LLM `/generate_json` |

```powershell
python tests/run_api_qa.py --suite smoke --mode http
```

Reports: `tests/reports/QA_REPORT_<timestamp>.json` and `.md`
