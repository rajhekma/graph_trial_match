# Quick health check (~2 sec). Server must be running on port 8000.
Set-Location $PSScriptRoot\..
.\.venv\Scripts\python.exe tests\run_api_qa.py --suite smoke --mode http @args
