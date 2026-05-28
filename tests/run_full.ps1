# Full E2E (slow). Server must be running on port 8000.
Set-Location $PSScriptRoot\..
.\.venv\Scripts\python.exe tests\run_api_qa.py --suite full --mode http @args
