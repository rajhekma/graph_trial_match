# API QA Tests — Manual Run Guide

Automated checks for all `graph_trial_match` endpoints. **You run these locally** — nothing runs in the background from the agent.

## 1. Start the API

```powershell
cd C:\Users\ADMIN\Documents\graph_trial_match
.\.venv\Scripts\Activate.ps1
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

Keep this terminal open.

## 2. Run tests (new terminal)

```powershell
cd C:\Users\ADMIN\Documents\graph_trial_match
.\.venv\Scripts\Activate.ps1
```

### Quick smoke (~2 seconds)

Health only — confirms server + Neo4j runner:

```powershell
python tests/run_api_qa.py --suite smoke --mode http
```

### PIR APIs only (~30 seconds)

Requires you already ran `POST /test_engine` for the same `nct_id` (data in MySQL):

```powershell
python tests/run_api_qa.py --suite pir --mode http --nct-id NCT05545020
```

### Engine only (Neo4j + MySQL, no new LLM call)

Save criteria once from Postman (`POST /generate_json` → copy full response to a file), then:

```powershell
python tests/run_api_qa.py --suite engine --mode http --criteria-file tests/fixtures/my_criteria.json --nct-id NCT05545020
```

### Full pipeline (slow: 5–30+ minutes)

LLM + Neo4j + MySQL + all PIR endpoints:

```powershell
python tests/run_api_qa.py --suite full --mode http
```

## 3. Read the report

After each run:

- `tests/reports/QA_REPORT_<timestamp>.md` — human-readable table
- `tests/reports/QA_REPORT_<timestamp>.json` — machine-readable

## Options

| Flag | Example | Meaning |
|---|---|---|
| `--suite` | `smoke`, `pir`, `engine`, `full` | Which tests to run |
| `--mode` | `http` | Call live server (use while uvicorn is running) |
| `--mode` | `testclient` | In-process (no port; still hits Neo4j/MySQL/OpenAI) |
| `--base-url` | `http://127.0.0.1:8000` | API URL |
| `--nct-id` | `NCT05545020` | Trial ID for engine/PIR tests |
| `--criteria-file` | `tests/fixtures/my_criteria.json` | Skip LLM generate step |

Environment variables (optional): `QA_SUITE`, `QA_BASE_URL`, `QA_NCT_ID`, `QA_CRITERIA_FILE`, `QA_MODE`.

## Recommended manual workflow

1. **smoke** — server up?
2. Postman: `POST /generate_json` → save response as `tests/fixtures/my_criteria.json`
3. **engine** with `--criteria-file` — matching + MySQL write
4. **pir** with same `--nct-id` — visualization APIs

## Test case list

See [QA_TEST_PLAN.md](QA_TEST_PLAN.md).

## Optional: pytest

```powershell
pip install -r tests/requirements-test.txt
pytest tests/test_api_integration.py -v
```

(Runs the same script as `run_api_qa.py`.)
