# Fixtures

Save the **full JSON response** from `POST /generate_json` here, for example:

`my_criteria.json`

Then run engine tests without calling OpenAI again:

```powershell
python tests/run_api_qa.py --suite engine --mode http --criteria-file tests/fixtures/my_criteria.json --nct-id NCT05545020
```

Do not commit files that contain secrets. Add `*.json` to `.gitignore` under fixtures if needed.
