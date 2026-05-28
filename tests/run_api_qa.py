"""
Graph Trial Match — API QA runner (run manually).

Suites:
  smoke   — health only (~1 sec)
  pir     — PIR APIs only; needs prior /test_engine for QA_NCT_ID
  engine  — health + test_engine; requires --criteria-file (skips LLM)
  full    — all 11 steps (slow: LLM + Neo4j)

Reports: tests/reports/QA_REPORT_<timestamp>.json|.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

BASE_URL = os.getenv("QA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
QA_MODE = os.getenv("QA_MODE", "auto")  # auto | http | testclient
NCT_ID = os.getenv("QA_NCT_ID", "NCT_QA_E2E")
RUN_NCT_FLOW = os.getenv("QA_RUN_NCT", "0") == "1"
RUN_GENERATE_AND_RUN = os.getenv("QA_RUN_COMBINED", "0") == "1"
REQUEST_TIMEOUT = int(os.getenv("QA_TIMEOUT_SEC", "600"))
CRITERIA_FILE = os.getenv("QA_CRITERIA_FILE", "")

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Shared state between steps
_state: Dict[str, Any] = {}
_client_mode = "http"


class ApiResponse:
    def __init__(self, status_code: int, body: bytes, text: str):
        self.status_code = status_code
        self.content = body
        self.text = text

    def json(self) -> Any:
        return json.loads(self.text) if self.text else {}


class ApiClient:
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        timeout: int = 30,
    ) -> ApiResponse:
        raise NotImplementedError


class HttpApiClient(ApiClient):
    def request(self, method, path, *, json_body=None, params=None, timeout=30):
        url = f"{BASE_URL}{path}"
        if method == "GET":
            r = requests.get(url, params=params, timeout=timeout)
        else:
            r = requests.post(url, json=json_body, params=params, timeout=timeout)
        return ApiResponse(r.status_code, r.content, r.text)


class TestClientApiClient(ApiClient):
    def __init__(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from fastapi.testclient import TestClient
        from app import app

        self._tc = TestClient(app)

    def request(self, method, path, *, json_body=None, params=None, timeout=30):
        if method == "GET":
            r = self._tc.get(path, params=params)
        else:
            r = self._tc.post(path, json=json_body, params=params)
        return ApiResponse(r.status_code, r.content, r.text)


def _resolve_client() -> ApiClient:
    global _client_mode
    if QA_MODE == "testclient":
        _client_mode = "testclient"
        return TestClientApiClient()
    if QA_MODE == "http":
        _client_mode = "http"
        return HttpApiClient()
    try:
        requests.get(f"{BASE_URL}/health", timeout=3)
        _client_mode = "http"
        return HttpApiClient()
    except requests.RequestException:
        _client_mode = "testclient"
        return TestClientApiClient()


_api: Optional[ApiClient] = None


def _get_api() -> ApiClient:
    global _api
    if _api is None:
        _api = _resolve_client()
    return _api


class TestCase:
    def __init__(
        self,
        case_id: str,
        name: str,
        method: str,
        path: str,
        fn: Callable[[], None],
        *,
        optional: bool = False,
    ):
        self.case_id = case_id
        self.name = name
        self.method = method
        self.path = path
        self.fn = fn
        self.optional = optional
        self.status = "PENDING"
        self.duration_ms = 0
        self.error: Optional[str] = None
        self.notes: Optional[str] = None
        self.response_preview: Optional[str] = None

    def run(self) -> None:
        t0 = time.perf_counter()
        try:
            self.fn(self)
            self.status = "PASS"
        except AssertionError as e:
            self.status = "FAIL"
            self.error = str(e)
        except requests.RequestException as e:
            self.status = "FAIL"
            self.error = f"Request error: {e}"
        except Exception as e:
            self.status = "FAIL"
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.duration_ms = int((time.perf_counter() - t0) * 1000)


def _preview(data: Any, max_len: int = 400) -> str:
    try:
        s = json.dumps(data, default=str)[:max_len]
    except Exception:
        s = str(data)[:max_len]
    return s + ("..." if len(s) >= max_len else "")


def _req(
    case: TestCase,
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: int = REQUEST_TIMEOUT,
) -> ApiResponse:
    r = _get_api().request(
        method, path, json_body=json_body, params=params, timeout=timeout
    )
    try:
        body = r.json() if r.content else {}
    except Exception:
        body = r.text[:200]
    case.response_preview = f"HTTP {r.status_code} — {_preview(body)}"
    if r.status_code >= 400:
        detail = r.text[:500]
        try:
            detail = r.json().get("detail", detail)
        except Exception:
            pass
        raise AssertionError(f"Expected 2xx, got {r.status_code}: {detail}")
    return r


def tc01_health(case: TestCase) -> None:
    r = _req(case, "GET", "/health", timeout=30)
    data = r.json()
    assert data.get("status") == "ok", data
    assert data.get("neo4j_runner") is True, "Neo4j runner not initialized — check .env"
    case.notes = "Neo4j runner OK"


def tc02_pir_health(case: TestCase) -> None:
    r = _req(case, "GET", "/api/health", timeout=30)
    assert r.json().get("status") == "ok"


def _load_criteria_file(path: str) -> None:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Criteria file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    _state["criteria_json"] = data
    global NCT_ID
    if data.get("nct_id"):
        NCT_ID = data["nct_id"]


def tc03_generate_json_arrays(case: TestCase) -> None:
    if _state.get("criteria_json"):
        case.status = "SKIP"
        case.notes = "Using criteria from --criteria-file"
        return
    body = {
        "nct_id": NCT_ID,
        "inclusion": [
            "Adults aged 18 to 65 years",
            "Diagnosis of Type 2 diabetes mellitus",
        ],
        "exclusion": ["Pregnant or breastfeeding"],
    }
    r = _req(case, "POST", "/generate_json", json_body=body)
    data = r.json()
    inc = data.get("inclusion_criteria") or []
    exc = data.get("exclusion_criteria") or []
    assert isinstance(inc, list) and len(inc) >= 1, "inclusion_criteria empty"
    assert isinstance(exc, list), "exclusion_criteria missing"
    for i, c in enumerate(inc):
        assert isinstance(c, dict), f"inclusion[{i}] not object"
        assert c.get("categories") or c.get("description"), f"inclusion[{i}] missing structure"
    _state["criteria_json"] = data
    case.notes = f"inclusion={len(inc)}, exclusion={len(exc)}"


def tc04_test_engine_full(case: TestCase) -> None:
    criteria = _state.get("criteria_json")
    assert criteria, "Run TC-03 first"
    r = _req(case, "POST", "/test_engine", json_body=criteria, timeout=REQUEST_TIMEOUT)
    data = r.json()
    assert data.get("status") == "success", data
    assert data.get("nct_id") == NCT_ID or criteria.get("nct_id"), data
    assert "patients" in data, data
    assert "final_count" in data, data
    fc = data.get("final_count")
    assert fc is None or isinstance(fc, int), "final_count should be int"
    _state["final_count"] = fc
    case.notes = f"final_count={fc}, patients_returned={len(data.get('patients') or [])}"


def tc05_test_engine_pagination(case: TestCase) -> None:
    criteria = _state.get("criteria_json")
    assert criteria, "Run TC-03 first"
    r = _req(
        case,
        "POST",
        "/test_engine",
        json_body=criteria,
        params={"page": 0},
        timeout=120,
    )
    data = r.json()
    assert data.get("status") == "success"
    assert data.get("mode") == "pagination", data
    case.notes = f"page={data.get('page')}"


def tc06_pir_results(case: TestCase) -> None:
    r = _req(case, "GET", f"/api/nct/{NCT_ID}/results", timeout=60)
    data = r.json()
    assert data.get("nct_id") == NCT_ID
    assert "records" in data
    case.notes = f"records={len(data.get('records') or [])}"


def tc07_all_inclusions(case: TestCase) -> None:
    r = _req(case, "GET", f"/api/nct/{NCT_ID}/all_inclusions", timeout=120)
    data = r.json()
    assert "nodes" in data and "edges" in data
    nodes = data.get("nodes") or []
    _state["pir_nodes"] = nodes
    case.notes = f"nodes={len(nodes)}, edges={len(data.get('edges') or [])}"


def tc08_all_exclusions(case: TestCase) -> None:
    r = _req(case, "GET", f"/api/nct/{NCT_ID}/all_exclusions", timeout=120)
    data = r.json()
    assert "nodes" in data and "edges" in data
    case.notes = f"nodes={len(data.get('nodes') or [])}"


def tc09_single_inclusion(case: TestCase) -> None:
    r = _req(case, "GET", f"/api/nct/{NCT_ID}/inclusion/0", timeout=120)
    data = r.json()
    assert data.get("nct_id") == NCT_ID
    assert "patients" in data
    case.notes = f"labels={len(data.get('labels') or [])}"


def tc10_single_exclusion(case: TestCase) -> None:
    r = _req(case, "GET", f"/api/nct/{NCT_ID}/exclusion/0", timeout=120)
    data = r.json()
    assert data.get("nct_id") == NCT_ID


def tc11_expand_nodes(case: TestCase) -> None:
    nodes = _state.get("pir_nodes") or []
    items = []
    for n in nodes:
        if n.get("type") == "label":
            props = n.get("props") or {}
            mid = props.get("matched_node_id")
            lab = props.get("matched_label") or n.get("label")
            if mid:
                items.append({"id": str(mid), "label": lab or "Condition"})
                break
    if not items:
        case.status = "SKIP"
        case.notes = "No label nodes to expand (empty match graph)"
        return
    r = _req(case, "POST", "/api/expand/nodes", json_body={"items": items}, timeout=120)
    data = r.json()
    assert isinstance(data, list), "expand/nodes should return a list"
    case.notes = f"expanded={len(data)}"


def tc12_generate_json_nct(case: TestCase) -> None:
    r = _req(
        case,
        "POST",
        "/generate_json",
        json_body={"nctCode": "NCT05545020"},
        timeout=REQUEST_TIMEOUT,
    )
    data = r.json()
    assert data.get("inclusion_criteria") is not None
    case.notes = "NCT extractor + LLM flow OK"


def tc13_generate_and_run(case: TestCase) -> None:
    body = {
        "nct_id": NCT_ID,
        "inclusion": ["Adults aged 18 to 65 years with Type 2 diabetes"],
        "exclusion": [],
    }
    r = _req(case, "POST", "/generate_and_run", json_body=body, timeout=REQUEST_TIMEOUT)
    data = r.json()
    for key in ("nct_id", "final_count", "included_count", "excluded_count"):
        assert key in data, f"missing {key}"
    case.notes = f"final_count={data.get('final_count')}"


SUITE_CASES = {
    "smoke": ["TC-01", "TC-02"],
    "pir": ["TC-01", "TC-02", "TC-06", "TC-07", "TC-08", "TC-09", "TC-10", "TC-11"],
    "engine": ["TC-01", "TC-02", "TC-04", "TC-05"],
    "full": [
        "TC-01", "TC-02", "TC-03", "TC-04", "TC-05",
        "TC-06", "TC-07", "TC-08", "TC-09", "TC-10", "TC-11",
    ],
}


def build_cases(suite: str = "full") -> List[TestCase]:
    all_cases = {
        "TC-01": TestCase("TC-01", "Root health", "GET", "/health", tc01_health),
        "TC-02": TestCase("TC-02", "PIR health", "GET", "/api/health", tc02_pir_health),
        "TC-03": TestCase("TC-03", "Generate JSON (arrays)", "POST", "/generate_json", tc03_generate_json_arrays),
        "TC-04": TestCase("TC-04", "Test engine full run", "POST", "/test_engine", tc04_test_engine_full),
        "TC-05": TestCase("TC-05", "Test engine pagination", "POST", "/test_engine?page=0", tc05_test_engine_pagination),
        "TC-06": TestCase("TC-06", "PIR results", "GET", f"/api/nct/{NCT_ID}/results", tc06_pir_results),
        "TC-07": TestCase("TC-07", "All inclusions graph", "GET", f"/api/nct/{NCT_ID}/all_inclusions", tc07_all_inclusions),
        "TC-08": TestCase("TC-08", "All exclusions graph", "GET", f"/api/nct/{NCT_ID}/all_exclusions", tc08_all_exclusions),
        "TC-09": TestCase("TC-09", "Single inclusion cluster", "GET", f"/api/nct/{NCT_ID}/inclusion/0", tc09_single_inclusion),
        "TC-10": TestCase("TC-10", "Single exclusion cluster", "GET", f"/api/nct/{NCT_ID}/exclusion/0", tc10_single_exclusion),
        "TC-11": TestCase("TC-11", "Expand nodes", "POST", "/api/expand/nodes", tc11_expand_nodes),
    }
    ids = SUITE_CASES.get(suite, SUITE_CASES["full"])
    cases = [all_cases[i] for i in ids]
    if RUN_NCT_FLOW:
        cases.append(
            TestCase(
                "TC-12",
                "Generate JSON (NCT)",
                "POST",
                "/generate_json",
                tc12_generate_json_nct,
                optional=True,
            )
        )
    if RUN_GENERATE_AND_RUN:
        cases.append(
            TestCase(
                "TC-13",
                "Generate and run",
                "POST",
                "/generate_and_run",
                tc13_generate_and_run,
                optional=True,
            )
        )
    return cases


def write_reports(
    cases: List[TestCase], started: datetime, suite: str = "full"
) -> tuple[Path, Path]:
    finished = datetime.now(timezone.utc)
    passed = sum(1 for c in cases if c.status == "PASS")
    failed = sum(1 for c in cases if c.status == "FAIL")
    skipped = sum(1 for c in cases if c.status == "SKIP")
    pending = sum(1 for c in cases if c.status == "PENDING")

    stamp = finished.strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS_DIR / f"QA_REPORT_{stamp}.json"
    md_path = REPORTS_DIR / f"QA_REPORT_{stamp}.md"

    payload = {
        "project": "graph_trial_match",
        "base_url": BASE_URL,
        "client_mode": _client_mode,
        "suite": suite,
        "nct_id": NCT_ID,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "summary": {
            "total": len(cases),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pending": pending,
            "overall": "PASS" if failed == 0 else "FAIL",
        },
        "cases": [
            {
                "id": c.case_id,
                "name": c.name,
                "method": c.method,
                "path": c.path,
                "status": c.status,
                "duration_ms": c.duration_ms,
                "error": c.error,
                "notes": c.notes,
                "response_preview": c.response_preview,
            }
            for c in cases
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    sep = "|---|---|"
    lines = [
        "# Graph Trial Match — QA Report",
        "",
        "| Field | Value |",
        sep,
        f"| Client mode | `{_client_mode}` |",
        f"| Base URL | `{BASE_URL}` |",
        f"| NCT ID | `{NCT_ID}` |",
        f"| Started | {started.isoformat()} |",
        f"| Finished | {finished.isoformat()} |",
        f"| **Overall** | **{payload['summary']['overall']}** |",
        f"| Passed | {passed} |",
        f"| Failed | {failed} |",
        f"| Skipped | {skipped} |",
        "",
        "## Results",
        "",
        "| ID | Test | Method | Path | Status | ms | Notes |",
        "|---|---|---|---|---|---:|---|",
    ]
    for c in cases:
        notes = (c.notes or "").replace("|", "/")
        err = f" — {c.error}" if c.error else ""
        lines.append(
            f"| {c.case_id} | {c.name} | {c.method} | `{c.path}` | **{c.status}** | {c.duration_ms} | {notes}{err} |"
        )
    lines.extend(["", "## Failures", ""])
    failures = [c for c in cases if c.status == "FAIL"]
    if not failures:
        lines.append("None.")
    else:
        for c in failures:
            lines.append(f"### {c.case_id} — {c.name}")
            lines.append(f"- Error: `{c.error}`")
            if c.response_preview:
                lines.append(f"- Response: `{c.response_preview}`")
            lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def parse_args():
    p = argparse.ArgumentParser(description="Graph Trial Match API QA")
    p.add_argument(
        "--suite",
        choices=["smoke", "pir", "engine", "full"],
        default=os.getenv("QA_SUITE", "full"),
        help="smoke=health only; pir=PIR APIs; engine=test_engine; full=all (slow)",
    )
    p.add_argument("--base-url", default=BASE_URL, help="API base URL (http mode)")
    p.add_argument("--mode", choices=["auto", "http", "testclient"], default=QA_MODE)
    p.add_argument("--nct-id", default=NCT_ID, help="Trial ID for engine/PIR tests")
    p.add_argument(
        "--criteria-file",
        default=CRITERIA_FILE,
        help="JSON from POST /generate_json — skips TC-03 LLM call",
    )
    return p.parse_args()


def main() -> int:
    global _api, BASE_URL, QA_MODE, NCT_ID, CRITERIA_FILE

    args = parse_args()
    BASE_URL = args.base_url.rstrip("/")
    QA_MODE = args.mode
    NCT_ID = args.nct_id
    CRITERIA_FILE = args.criteria_file or ""

    if CRITERIA_FILE:
        _load_criteria_file(CRITERIA_FILE)

    _api = _resolve_client()
    print(f"QA suite={args.suite} mode={_client_mode} base={BASE_URL} nct={NCT_ID}")
    if CRITERIA_FILE:
        print(f"  criteria-file={CRITERIA_FILE}")

    started = datetime.now(timezone.utc)
    cases = build_cases(args.suite)
    for case in cases:
        print(f"  [{case.case_id}] {case.name}...", end=" ", flush=True)
        case.run()
        print(case.status, f"({case.duration_ms}ms)")
        if case.status == "FAIL" and not case.optional:
            print(f"    FAIL: {case.error}")
            # Continue to collect all failures (QA best practice)

    json_path, md_path = write_reports(cases, started, args.suite)
    failed = sum(1 for c in cases if c.status == "FAIL")
    print()
    print(f"Report JSON: {json_path}")
    print(f"Report MD:   {md_path}")
    print(f"Summary: {sum(1 for c in cases if c.status == 'PASS')} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
