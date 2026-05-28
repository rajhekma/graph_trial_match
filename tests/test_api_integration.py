"""
Pytest wrapper for API QA (optional).

  pytest tests/test_api_integration.py -v
"""
import os
import subprocess
import sys
from pathlib import Path


def test_api_qa_suite():
    script = Path(__file__).resolve().parent / "run_api_qa.py"
    env = os.environ.copy()
    env.setdefault("QA_BASE_URL", "http://127.0.0.1:8000")
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=int(env.get("QA_TIMEOUT_SEC", "900")),
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    assert result.returncode == 0, "QA suite failed — see tests/reports/"
