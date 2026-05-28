import os
import json
import logging
from typing import Dict, List, Set, Optional, Any, Tuple, Iterable
from neo4j import GraphDatabase
from datetime import datetime
from collections import defaultdict
from dateutil import parser as _dt_parser
from datetime import datetime as _dt
import re
# ---------- Setup ----------
logger = logging.getLogger("trial_matching.cypher_engine")
logging.basicConfig(level=logging.INFO)
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASS", "password")
# ---------- Helpers ----------
# Mapping of which fields are CodeableConcept→Coding by resource type
CODEABLECONCEPT_SYSTEMS = {
    "Condition": {
        "clinicalStatus": "condition-clinical",
        "verificationStatus": "condition-ver-status"
    },
    "AllergyIntolerance": {
        "clinicalStatus": "allergyintolerance-clinical",
        "verificationStatus": "allergyintolerance-verification"
    },
    "MedicationRequest": {
        "status": "medication-status",
        "verificationStatus": "medication-ver-status"
    },
    # Add other resource types as needed later
}
def _normalize_logic(logic: Optional[str], default: str = "OR") -> str:
    if not logic:
        return default
    l = (logic or "").upper()
    return l if l in ("AND", "OR") else default
def _python_check_status(rec, category_obj):
    status_field = category_obj.get("statusField")
    status_values = category_obj.get("statusValues")
    if not status_field or not status_values:
        return True
    if status_field not in rec:
        return True
    val = rec[status_field]
    if val is None:
        return False
    # Extract embedded code='xyz' from stringified CodeableConcept
    if isinstance(val, str) and "code='" in val:
        match = re.search(r"code='([^']+)'", val)
        if match:
            val = match.group(1)
    return str(val).strip().lower() in [s.lower() for s in status_values]
def extract_code_from_chain(record, system_keyword):
    """
    Robust extraction of a 'code' from either:
    - nested dict/list (CodeableConcept -> Coding),
    - OR stringified CodeableConcept/Coding text (e.g. "Coding(code='active', system='...')").
    Returns the first matching code (string) or None.
    """
    try:
        def scan(obj):
            if isinstance(obj, dict):
                # Look for common CodeableConcept shapes
                # - 'coding': [ { 'system':..., 'code':... }, ... ]
                # - 'Coding': [...]
                if "coding" in obj and isinstance(obj["coding"], (list, tuple)):
                    for coding in obj["coding"]:
                        if isinstance(coding, dict):
                            sys = str(coding.get("system", "") or "")
                            code = coding.get("code") or coding.get("id") or coding.get("value")
                            if code and system_keyword.lower() in sys.lower():
                                return str(code)
                            if code and not sys:
                                return str(code)
                if "Coding" in obj and isinstance(obj["Coding"], (list, tuple)):
                    for coding in obj["Coding"]:
                        if isinstance(coding, dict):
                            sys = str(coding.get("system", "") or "")
                            code = coding.get("code") or coding.get("id") or coding.get("value")
                            if code and system_keyword.lower() in sys.lower():
                                return str(code)
                            if code and not sys:
                                return str(code)
                # direct 'code' key
                if "code" in obj and isinstance(obj["code"], (str, int, float)):
                    return str(obj["code"])
                # generic nested scan
                for k, v in obj.items():
                    if isinstance(v, (dict, list)):
                        res = scan(v)
                        if res:
                            return res
            elif isinstance(obj, (list, tuple)):
                for item in obj:
                    res = scan(item)
                    if res:
                        return res
            return None
        # 1st attempt: structured scan
        res = scan(record)
        if res:
            return res
        # 2nd attempt: parse stringified representations
        def parse_from_string(s):
            import re
            if not isinstance(s, str):
                return None
            # code='active' or code="active" or code=active
            m = re.search(r"code\s*[:=]\s*['\"]?([A-Za-z0-9._-]+)['\"]?", s)
            if m:
                return m.group(1)
            # coding=[...code='x'...]
            m2 = re.search(r"coding.*?code\s*[:=]\s*['\"]?([A-Za-z0-9._-]+)['\"]?", s)
            if m2:
                return m2.group(1)
            # fallback: look for simple tokens that look like 'active','resolved', etc.
            m3 = re.search(r"\b(active|inactive|resolved|recurrence|confirmed|unconfirmed|entered-in-error|provisional)\b", s, re.I)
            if m3:
                return m3.group(1)
            return None
        if isinstance(record, str):
            return parse_from_string(record)
        if isinstance(record, dict):
            for v in record.values():
                if isinstance(v, str):
                    parsed = parse_from_string(v)
                    if parsed:
                        return parsed
        return None
    except Exception:
        return None
def normalize_codeableconcept_fields(record, record_type=None):
    """
    Dynamically normalizes CodeableConcept→Coding fields to scalar string values.
    - Detects the record type (Condition, AllergyIntolerance, etc.)
    - Looks up which fields need flattening using CODEABLECONCEPT_SYSTEMS
    - Extracts the proper 'code' from nested structures or stringified values
    """
    record_type = record.get("resourceType", record_type)
    if not record_type:
        return record
    concept_map = CODEABLECONCEPT_SYSTEMS.get(record_type, {})
    if not concept_map:
        return record
    for field, keyword in concept_map.items():
        try:
            value = record.get(field)
            # If field missing or falsy -> try deep extraction
            if not value:
                extracted_code = extract_code_from_chain(record, keyword)
                if extracted_code:
                    record[field] = str(extracted_code).lower()
                    continue
            # If field is a dict-like structure, try to extract
            if isinstance(value, dict):
                extracted = extract_code_from_chain(value, keyword)
                if extracted:
                    record[field] = str(extracted).lower()
                    continue
                # direct code present
                if "code" in value and value.get("code"):
                    record[field] = str(value.get("code")).lower()
                    continue
                # display/text fallback
                for k in ("display", "text"):
                    if k in value and value.get(k):
                        record[field] = str(value.get(k)).lower()
                        break
            # If field is stringified CodeableConcept, parse "code='x'"
            elif isinstance(value, str):
                import re
                # common pattern code='active' or code="active"
                m = re.search(r"code\s*[:=]\s*['\"]?([A-Za-z0-9._-]+)['\"]?", value)
                if m:
                    record[field] = m.group(1).lower()
                    continue
                # coding.*code='x' pattern
                m2 = re.search(r"coding.*?code\s*[:=]\s*['\"]?([A-Za-z0-9._-]+)['\"]?", value)
                if m2:
                    record[field] = m2.group(1).lower()
                    continue
                # last resort: attempt to parse tokens
                parsed = extract_code_from_chain(value, keyword)
                if parsed:
                    record[field] = str(parsed).lower()
                    continue
        except Exception:
            # be conservative: leave original value if any failure
            continue
    return record
def _python_check_date_window(rec: Dict[str, Any], category_obj: Dict[str, Any], ref_event: str = "now") -> bool:
    """
    Re-check date windows in Python. If category has daysBefore/daysAfter, ensure
    at least one relevant timestamp in the record satisfies the window.
    """
    days_before = category_obj.get("daysBefore")
    days_after = category_obj.get("daysAfter")
    if days_before is None and days_after is None:
        return True
    # choose reference fields according to ref_event
    date_fields = REFERENCE_EVENT_DATE_FIELDS.get((category_obj.get("referenceEvent") or ref_event).lower(), ["effectiveDateTime"])
    # gather candidate timestamps from record
    timestamps = []
    for f in date_fields:
        # property naming in cypher returns: 'dt','issued','recordedDate','onsetDateTime', etc.
        for key in (f, f.replace('.', '_'), "dt", "issued", "recordedDate", "onsetDateTime", "authoredOn", "effectiveDateTime", "valueDateTime"):
            if key in rec and rec.get(key):
                timestamps.append(rec.get(key))
    if not timestamps:
        # no date information to check -> conservatively treat as passed
        return True
    now = _dt.utcnow()
    for t in timestamps:
        try:
            dt = _dt_parser.parse(t)
        except Exception:
            continue
        if days_before is not None:
            # check recorded within last days_before
            delta_days = (now - dt).days
            if delta_days <= int(days_before):
                # passes daysBefore
                pass
            else:
                # this timestamp is too old; continue checking other timestamps
                continue
        if days_after is not None:
            delta_days2 = (dt - now).days
            if delta_days2 <= int(days_after):
                pass
            else:
                continue
        # if reached here, this timestamp satisfied both provided constraints (or constraints missing)
        return True
    # none of the timestamps satisfied window
    return False
def _normalize_unit_string(s: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Extracts numeric value and canonical unit from a string like '200 mg/dL' or '150 mmHg'.
    Returns (value, unit) where unit is lowercase canonical form.
    Handles typical medical units such as mg/dL, mmol/L, mmHg, etc.
    """
    import re
    if not isinstance(s, str):
        return None, None
    # Extract numeric portion
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    val = float(m.group(1)) if m else None
    # Extract possible unit portion (remove numbers and punctuation)
    u = re.sub(r"[0-9.\s:/=<>-]", "", s).strip().lower()
    if not u:
        return val, None
    # Canonicalize common lab units
    u_map = {
        "mgdl": "mg/dl",
        "mg/dl": "mg/dl",
        "mmol/l": "mmol/l",
        "mmol": "mmol/l",
        "mmhg": "mmhg",
        "meql": "meq/l",
        "meq/l": "meq/l",
        "gdl": "g/dl",
        "g/dl": "g/dl",
        "iu/l": "iu/l",
        "u/l": "iu/l",
    }
    unit = u_map.get(u, u)
    return val, unit
def _parse_value_quantity(value_q: Any) -> Optional[float]:
    """
    Parse the Observation.valueQuantity (or similar fields) into a float, robustly.
    Now supports unit normalization from strings like '12.3 mg/dL' or '150 mmHg'.
    Handles:
    - numeric literals (int, float)
    - dicts with 'value', 'valueDecimal', etc.
    - string-based encodings with embedded units or 'value=Decimal()'
    Returns:
        float value if parsed, else None.
    """
    if value_q is None:
        return None
    # --- Direct numeric types ---
    if isinstance(value_q, (int, float)):
        return float(value_q)
    # --- Dictionary-style fields ---
    if isinstance(value_q, dict):
        for k in ("value", "valueDecimal", "valueQuantity", "valueInteger"):
            v = value_q.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    pass
        return None
    # --- String-based formats ---
    if isinstance(value_q, str):
        import re
        # First attempt: normalize unit string (our custom helper)
        val, unit = _normalize_unit_string(value_q)
        if val is not None:
            return val
        # Strict pattern: value=Decimal('12.34')
        m = re.search(r"value\s*=\s*Decimal\(['\"]([0-9.+-eE]+)['\"]\)", value_q)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
        # Simple assignment: value=12.34
        m = re.search(r"value\s*=\s*([0-9]+(?:\.[0-9]+)?)", value_q)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
        # Alternate keys: v=12.34 or val=12.34
        m = re.search(r"(?:value|v|val)\s*[:=]\s*['\"]?([0-9]+(?:\.[0-9]+)?)['\"]?", value_q)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
        # Embedded numeric (fallback)
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", value_q)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    # --- Fallback ---
    return None
def _log_query(tag: str, q: str, params: Dict[str, Any]):
    """
    Log the generated query and parameter keys only (sanitize values).
    Do not log patient PHI or full parameter values.
    """
    try:
        # Build a sanitized param summary: only keys and types/lengths
        sanitized = {}
        for k, v in (params or {}).items():
            try:
                if v is None:
                    sanitized[k] = None
                elif isinstance(v, (list, tuple)):
                    sanitized[k] = f"<list len={len(v)}>"
                elif isinstance(v, dict):
                    sanitized[k] = "<dict>"
                else:
                    sanitized[k] = f"<{type(v).__name__}>"
            except Exception:
                sanitized[k] = "<error>"
        logger.info(
            "\n[QUERY] Category: %s\n%s\nParamKeys: %s\n",
            tag,
            q.strip(),
            json.dumps(sanitized),
        )
    except Exception:
        logger.warning("Failed to log query for %s", tag)
        # ---------- Reference Event Mapping ----------
# Defines which date/time fields correspond to each possible referenceEvent.
REFERENCE_EVENT_DATE_FIELDS = {
    "sampling": ["effectiveDateTime", "issued"],
    "observation": ["effectiveDateTime", "issued", "valueDateTime"],
    "diagnosticreport": ["effectiveDateTime", "issued"],
    "procedure": ["onsetDateTime", "recordedDate"],
    "diagnosis": ["onsetDateTime", "recordedDate"],
    "medication": ["authoredOn", "effectiveDateTime", "issued"],
    "allergy": ["recordedDate", "onsetDateTime"],
    "now": [], # means current datetime()
}
def _merge_term_codes_into_category(category_obj):
    merged = defaultdict(list)
    for td in category_obj.get("term_details", {}).values():
        for system, codes in (td.get("codes_by_system") or {}).items():
            merged[system].extend(codes)
    if merged:
        category_obj["codes"] = [
            {"system": system, "code": c.get("code")}
            for system, codes in merged.items()
            for c in codes
            if isinstance(c, dict) and c.get("code")
        ]
def _build_date_window_clause(
    node_alias: str, ref_event: str, days_param: str = "days", after: bool = False
) -> str:
    """
    Safe temporal clause builder — skips empty output, supports multiple reference fields.
    """
    if not ref_event:
        return ""
    ref_event = ref_event.lower()
    date_fields = REFERENCE_EVENT_DATE_FIELDS.get(ref_event, ["effectiveDateTime"])
    if not date_fields:
        return ""
    clauses = []
    for df in date_fields:
        field_expr = f"{node_alias}.{df.replace('.', '_')}"
        if after:
            clauses.append(
                f"(datetime({field_expr}) <= datetime() + duration({{days:${days_param}}}) "
                f"AND datetime({field_expr}) >= datetime())"
            )
        else:
            clauses.append(
                f"(datetime({field_expr}) >= datetime() - duration({{days:${days_param}}}) "
                f"AND datetime({field_expr}) <= datetime())"
            )
    return " OR ".join(clauses)
# ---------- Query Builders (returning patient details) ----------
def _build_condition_query_codes(
    codes_by_system: Dict[str, List[str]],
    status_field: Optional[str] = None,
    status_values: Optional[List[str]] = None,
    previous_ids: Optional[Iterable[str]] = None,
    days_before: Optional[int] = None,
    ref_event: Optional[str] = None,
    days_after: Optional[int] = None,
    limit: Optional[int] = None,
) -> Tuple[str, Dict]:
    if not codes_by_system:
        return "", {}
    params = {}
    if ref_event:
        params["referenceEvent"] = ref_event
    if days_after is not None:
        try:
            params["daysAfter"] = int(days_after)
        except Exception:
            logger.warning("Invalid days_after provided to _build_condition_query_codes: %s", days_after)
    if days_before is not None:
        try:
            params["daysBefore"] = int(days_before)
        except Exception:
            logger.warning("Invalid days_before provided to _build_condition_query_codes: %s", days_before)
    code_clauses = []
    # parameterize systems and codes
    for i, (system, codes) in enumerate(codes_by_system.items()):
        if not codes:
            continue
        codes_key = f"codes_{i}"
        sys_key = f"system_{i}"
        normalized_codes = []
        for entry in codes:
            if entry is None:
                continue
            if isinstance(entry, dict):
                code_val = entry.get("code") or entry.get("id") or entry.get("value")
                if code_val:
                    normalized_codes.append(str(code_val))
            else:
                normalized_codes.append(str(entry))
        seen = set()
        deduped_codes = []
        for cc in normalized_codes:
            if cc not in seen:
                seen.add(cc)
                deduped_codes.append(cc)
        if not deduped_codes:
            continue
        params[codes_key] = deduped_codes
        params[sys_key] = system
        code_clauses.append(f"(c.system = ${sys_key} AND c.code IN ${codes_key})")
    if not code_clauses:
        return "", {}
    code_where = " OR ".join(code_clauses)
    where_parts = [f"({code_where})"]
    ref_event_local = params.get("referenceEvent", "now")
    if params.get("daysBefore") is not None:
        where_parts.append(
            _build_date_window_clause(
                "cond", ref_event_local, days_param="daysBefore", after=False
            )
        )
    if params.get("daysAfter") is not None:
        where_parts.append(
            _build_date_window_clause(
                "cond", ref_event_local, days_param="daysAfter", after=True
            )
        )
    where_parts = [wp for wp in where_parts if wp.strip()]
    where_clause = " AND ".join(where_parts) if where_parts else "true"
    # Build queries that USE the combined code_where expression in both branches
    if previous_ids:
        params["prev_ids"] = list(previous_ids)
        q = f"""
        UNWIND $prev_ids AS pid
        MATCH (c:Coding)
        WHERE {where_clause}
        MATCH (c)-[:REFERENCES]->(cc:CodeableConcept)
        MATCH (cc)-[:REFERENCES]->(cond:Condition)
        MATCH (cond)-[:REFERENCES]->(p:Patient {{id: pid}})
        RETURN DISTINCT
            p.id AS id,
            p.birthDate AS birthDate,
            p.gender AS gender,
            p.fieldFamilyName AS familyName,
            p.fieldGivenName AS givenName,
            p.address AS addressText,
            p.fieldMaritalStatusText AS maritalStatus,
            p.fieldIdentifierValue AS identifierValue,
            cond.onsetDateTime AS onsetDateTime,
            cond.recordedDate AS recordedDate,
            cond.clinicalStatus AS clinicalStatus,
            cond.verificationStatus AS verificationStatus,
            c.code AS matched_node_id,
            labels(cond)[0] AS matched_label

        """
    else:
        q = f"""
        MATCH (c:Coding)
        WHERE {where_clause}
        MATCH (c)-[:REFERENCES]->(cc:CodeableConcept)
        MATCH (cc)-[:REFERENCES]->(cond:Condition)
        MATCH (cond)-[:REFERENCES]->(p:Patient)
        RETURN DISTINCT
            p.id AS id,
            p.birthDate AS birthDate,
            p.gender AS gender,
            p.fieldFamilyName AS familyName,
            p.fieldGivenName AS givenName,
            p.address AS addressText,
            p.fieldMaritalStatusText AS maritalStatus,
            p.fieldIdentifierValue AS identifierValue,
            cond.onsetDateTime AS onsetDateTime,
            cond.recordedDate AS recordedDate,
            cond.clinicalStatus AS clinicalStatus,
            cond.verificationStatus AS verificationStatus,
            c.code AS matched_node_id,
            labels(cond)[0] AS matched_label

        """
    _log_query("Condition", q, params)
    return q, params
def _build_medication_query_codes(
    codes_by_system: Dict[str, List[str]],
    status_field: Optional[str] = None,
    status_values: Optional[List[str]] = None,
    previous_ids: Optional[Iterable[str]] = None,
    days_before: Optional[int] = None,
    ref_event: Optional[str] = None,
    days_after: Optional[int] = None,
    limit: Optional[int] = None,
) -> Tuple[str, Dict]:
    if not codes_by_system:
        return "", {}
    params = {}
    if ref_event:
        params["referenceEvent"] = ref_event
    if days_after is not None:
        try:
            params["daysAfter"] = int(days_after)
        except Exception:
            logger.warning("Invalid days_after for medication")
    if days_before is not None:
        try:
            params["daysBefore"] = int(days_before)
        except Exception:
            logger.warning("Invalid days_before for medication")
    code_clauses = []
    for i, (system, codes) in enumerate(codes_by_system.items()):
        if not codes:
            continue
        codes_key = f"codes_{i}"
        sys_key = f"system_{i}"
        deduped_codes = list({str(entry.get("code") if isinstance(entry, dict) else entry)
                              for entry in codes if entry})
        if not deduped_codes:
            continue
        params[codes_key] = deduped_codes
        params[sys_key] = system
        code_clauses.append(f"(c.system = ${sys_key} AND c.code IN ${codes_key})")
    if not code_clauses:
        return "", {}
    code_where = " OR ".join(code_clauses)
    where_parts = [f"({code_where})"]
    ref_event_local = params.get("referenceEvent", "now")
    if params.get("daysBefore") is not None:
        where_parts.append(
            _build_date_window_clause("m", ref_event_local, days_param="daysBefore", after=False)
        )
    if params.get("daysAfter") is not None:
        where_parts.append(
            _build_date_window_clause("m", ref_event_local, days_param="daysAfter", after=True)
        )
    where_clause = " AND ".join(where_parts) if where_parts else "TRUE"
    if previous_ids:
        params["prev_ids"] = list(previous_ids)
        q = f"""
        UNWIND $prev_ids AS pid
        MATCH (c:Coding)
        WHERE ({code_where})
        MATCH (c)<-[:REFERENCES]-(cc:CodeableConcept)
        MATCH (cc)<-[:REFERENCES]-(m)-[:REFERENCES]->(p:Patient {id: pid})
        WHERE (m:MedicationRequest OR m:MedicationOrder)
        RETURN DISTINCT
            p.id AS id,
            p.birthDate AS birthDate,
            p.gender AS gender,
            p.fieldFamilyName AS familyName,
            p.fieldGivenName AS givenName,
            m.authoredOn AS authoredOn,
            m.effectiveDateTime AS effectiveDateTime,
            m.issued AS issued,
            m.status AS medStatus,
            m.verificationStatus AS verificationStatus,
            c.code AS matched_node_id,
            labels(m)[0] AS matched_label

        """
    else:
        q = f"""
        MATCH (c:Coding)
        WHERE ({code_where})
        MATCH (c)<-[:REFERENCES]-(cc:CodeableConcept)
        MATCH (cc)<-[:REFERENCES]-(m)-[:REFERENCES]->(p:Patient)
        WHERE (m:MedicationRequest OR m:MedicationOrder)
        RETURN DISTINCT
            p.id AS id,
            p.birthDate AS birthDate,
            p.gender AS gender,
            p.fieldFamilyName AS familyName,
            p.fieldGivenName AS givenName,
            m.authoredOn AS authoredOn,
            m.effectiveDateTime AS effectiveDateTime,
            m.issued AS issued,
            m.status AS medStatus,
            m.verificationStatus AS verificationStatus
            c.code AS matched_node_id,
            labels(m)[0] AS matched_label

        """
    _log_query("Medication", q, params)
    return q, params
def _build_observation_query_codes(
    codes_by_system: Dict[str, List[str]],
    value_filter: Optional[Any] = None,
    days_before: Optional[int] = None, # kept for signature compatibility
    previous_ids: Optional[Iterable[str]] = None,
    return_values: bool = False,
    ref_event: Optional[str] = None,
    days_after: Optional[int] = None, # kept for signature compatibility
    limit: Optional[int] = 100000,
) -> Tuple[str, Dict]:
    if not codes_by_system:
        return "", {}
    params = {}
    if limit is not None:
        params["limit"] = int(limit)
    # -----------------------------
    # Build CODE filter ONLY
    # -----------------------------
    code_clauses = []
    for i, (_, codes) in enumerate(codes_by_system.items()):
        if not codes:
            continue
        codes_key = f"codes_{i}"
        normalized_codes = [
            str(e.get("code") if isinstance(e, dict) else e)
            for e in codes
            if e
        ]
        deduped_codes = list(dict.fromkeys(normalized_codes))
        if not deduped_codes:
            continue
        params[codes_key] = deduped_codes
        code_clauses.append(f"c.code IN ${codes_key}")
    if not code_clauses:
        return "", {}
    code_where = " OR ".join(code_clauses)
    # =====================================================
    # CASE 1: previous_ids → PATIENT-FIRST (EXCLUSION)
    # =====================================================
    if previous_ids:
        params["prev_ids"] = list(previous_ids)
        q = f"""
        UNWIND $prev_ids AS pid
        MATCH (p:Patient {{id: pid}})
        MATCH (p)<-[:REFERENCES]-(o:Observation)
        MATCH (o)<-[:REFERENCES]-(cc:CodeableConcept)
        MATCH (cc)<-[:REFERENCES]-(c:Coding)
        WHERE {code_where}
        RETURN
            p.id AS pid,
            p.birthDate AS birthDate,
            p.gender AS gender,
            p.fieldFamilyName AS familyName,
            p.fieldGivenName AS givenName,
            p.address AS addressText,
            p.fieldMaritalStatusText AS maritalStatus,
            p.fieldIdentifierValue AS identifierValue,
            o.valueQuantity AS valueQuantity,
            o.valueDecimal AS valueDecimal,
            o.valueInteger AS valueInteger,
            o.valueString AS valueString,
            o.effectiveDateTime AS dt,
            o.issued AS issued,
            o.valueDateTime AS valueDateTime,
            o.code AS obsCode,
            o.status AS obsStatus,
            c.code AS matched_node_id,
            labels(o)[0] AS matched_label


        """
    # CASE 2: NO previous_ids → CODE-FIRST (INCLUSION)
    else:
        q = f"""
        MATCH (c:Coding)
        WHERE {code_where}
        MATCH (c)-[:REFERENCES]->(cc:CodeableConcept)
        MATCH (cc)-[:REFERENCES]->(o:Observation)
        MATCH (o)-[:REFERENCES]->(p:Patient)
        RETURN
            p.id AS pid,
            p.birthDate AS birthDate,
            p.gender AS gender,
            p.fieldFamilyName AS familyName,
            p.fieldGivenName AS givenName,
            p.address AS addressText,
            p.fieldMaritalStatusText AS maritalStatus,
            p.fieldIdentifierValue AS identifierValue,
            o.valueQuantity AS valueQuantity,
            o.valueDecimal AS valueDecimal,
            o.valueInteger AS valueInteger,
            o.valueString AS valueString,
            o.effectiveDateTime AS dt,
            o.issued AS issued,
            o.valueDateTime AS valueDateTime,
            o.code AS obsCode,
            o.status AS obsStatus,
            o.id AS matched_node_id,
            labels(o)[0] AS matched_label

        """
    if limit is not None:
        q += "\nLIMIT $limit"
    _log_query("Observation (Clean, No Dates)", q, params)
    return q, params
def _build_demographics_query(
    demo_obj: Dict[str, Any],
    previous_ids: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> Tuple[str, Dict]:
    params = {}
    where_parts = []
    # AGE FILTER
    if "age" in demo_obj:
        age_obj = demo_obj["age"]
        if isinstance(age_obj, dict):
            op_raw = (age_obj.get("operator") or "").lower()
            op_map = {
                "greaterthan": ">",
                "greaterthanorequal": ">=",
                "lessthan": "<",
                "lessthanorequal": "<=",
                "equal": "=",
                "equals": "=",
                "equalto": "=",
                "between": "between",
                ">": ">",
                ">=": ">=",
                "<": "<",
                "<=": "<=",
            }
            op = op_map.get(op_raw, op_raw)
            today = datetime.utcnow().date()
            if op == "between":
                lower = age_obj.get("lower")
                upper = age_obj.get("upper")
                if lower is not None and upper is not None:
                    min_birth = datetime(today.year - upper, today.month, today.day).date()
                    max_birth = datetime(today.year - lower, today.month, today.day).date()
                    params["min_birth"] = str(min_birth)
                    params["max_birth"] = str(max_birth)
                    where_parts.append(
                        "p.birthDate >= date($min_birth) AND p.birthDate <= date($max_birth)"
                    )
            else:
                val = age_obj.get("value")
                try:
                    val_int = int(val)
                except Exception:
                    val_int = None
                if val_int is not None:
                    threshold_year = today.year - val_int
                    params["threshold"] = f"{threshold_year}-{today.month:02d}-{today.day:02d}"
                    if op == ">":
                        where_parts.append("p.birthDate < date($threshold)")
                    elif op == ">=":
                        where_parts.append("p.birthDate <= date($threshold)")
                    elif op == "<":
                        where_parts.append("p.birthDate > date($threshold)")
                    elif op == "<=":
                        where_parts.append("p.birthDate >= date($threshold)")
                    elif op == "=":
                        where_parts.append("p.birthDate = date($threshold)")
    # GENDER FILTER
    if "gender" in demo_obj:
        g = demo_obj["gender"]
        if isinstance(g, dict):
            op = (g.get("operator") or "").upper()
            vals = g.get("values") or []
            if op == "IN" and vals:
                params["gvals"] = vals
                where_parts.append("p.gender IN $gvals")
            elif op in ("=", "==") and vals:
                params["gval"] = vals[0]
                where_parts.append("p.gender = $gval")
        elif isinstance(g, list):
            params["gvals"] = g
            where_parts.append("p.gender IN $gvals")
        elif isinstance(g, str):
            params["gval"] = g
            where_parts.append("p.gender = $gval")
    # LIMIT
    if limit is not None:
        params["limit"] = int(limit)
    where_clause = " AND ".join(where_parts) if where_parts else "true"
    # CASE 1: previous_ids PRESENT → START FROM IDs
    if previous_ids:
        params["prev_ids"] = list(previous_ids)
        q = f"""
        UNWIND $prev_ids AS pid
        MATCH (p:Patient {{id: pid}})
        WHERE {where_clause}
        RETURN DISTINCT
            p.id AS id,
            p.birthDate AS birthDate,
            p.gender AS gender,
            p.fieldFamilyName AS familyName,
            p.fieldGivenName AS givenName,
            p.address AS addressText,
            p.fieldMaritalStatusText AS maritalStatus,
            p.fieldIdentifierValue AS identifierValue
        """
    # CASE 2: NO previous_ids → FULL SCAN (fallback)
    else:
        q = f"""
        MATCH (p:Patient)
        WHERE {where_clause}
        RETURN DISTINCT
            p.id AS id,
            p.birthDate AS birthDate,
            p.gender AS gender,
            p.fieldFamilyName AS familyName,
            p.fieldGivenName AS givenName,
            p.address AS addressText,
            p.fieldMaritalStatusText AS maritalStatus,
            p.fieldIdentifierValue AS identifierValue
        """
    if limit is not None:
        q += "\nLIMIT $limit"
    _log_query("Demographics (Optimized)", q, params)
    return q, params
def _build_allergy_query_codes(
    codes_by_system: Dict[str, List[str]],
    status_field: Optional[str] = None,
    status_values: Optional[List[str]] = None,
    previous_ids: Optional[Iterable[str]] = None,
    days_before: Optional[int] = None,
    ref_event: Optional[str] = None,
    days_after: Optional[int] = None,
    limit: Optional[int] = None,
) -> Tuple[str, Dict]:
    if not codes_by_system:
        return "", {}
    params = {}
    if ref_event:
        params["referenceEvent"] = ref_event
    if days_after is not None:
        params["daysAfter"] = int(days_after)
    if days_before is not None:
        params["daysBefore"] = int(days_before)
    code_clauses = []
    for i, (system, codes) in enumerate(codes_by_system.items()):
        if not codes:
            continue
        codes_key = f"codes_{i}"
        sys_key = f"system_{i}"
        deduped_codes = list({str(entry.get("code") if isinstance(entry, dict) else entry)
                              for entry in codes if entry})
        if not deduped_codes:
            continue
        params[codes_key] = deduped_codes
        params[sys_key] = system
        code_clauses.append(f"(c.system = ${sys_key} AND c.code IN ${codes_key})")
    if not code_clauses:
        return "", {}
    code_where = " OR ".join(code_clauses)
    where_parts = [f"({code_where})"]
    ref_event_local = params.get("referenceEvent", "now")
    if params.get("daysBefore") is not None:
        where_parts.append(
            _build_date_window_clause("a", ref_event_local, days_param="daysBefore", after=False)
        )
    if params.get("daysAfter") is not None:
        where_parts.append(
            _build_date_window_clause("a", ref_event_local, days_param="daysAfter", after=True)
        )
    where_clause = " AND ".join(where_parts) if where_parts else "TRUE"
    if previous_ids:
        params["prev_ids"] = list(previous_ids)
        q = f"""
        UNWIND $prev_ids AS pid
        MATCH (c:Coding)
        WHERE ({code_where})
        MATCH (c)<-[:REFERENCES]-(cc:CodeableConcept)
        MATCH (cc)<-[:REFERENCES]-(a:AllergyIntolerance)-[:REFERENCES]->(p:Patient {id: pid})
        RETURN DISTINCT
            p.id AS id,
            p.birthDate AS birthDate,
            p.gender AS gender,
            a.recordedDate AS recordedDate,
            a.onsetDateTime AS onsetDateTime,
            a.lastOccurrence AS lastOccurrence,
            a.clinicalStatus AS clinicalStatus,
            a.verificationStatus AS verificationStatus,
            a.status AS allergyStatus,
            c.code AS matched_node_id,
            labels(a)[0] AS matched_label

        """
    else:
        q = f"""
        MATCH (c:Coding)
        WHERE ({code_where})
        MATCH (c)<-[:REFERENCES]-(cc:CodeableConcept)
        MATCH (cc)<-[:REFERENCES]-(a:AllergyIntolerance)-[:REFERENCES]->(p:Patient)
        RETURN DISTINCT
            p.id AS id,
            p.birthDate AS birthDate,
            p.gender AS gender,
            a.recordedDate AS recordedDate,
            a.onsetDateTime AS onsetDateTime,
            a.lastOccurrence AS lastOccurrence,
            a.clinicalStatus AS clinicalStatus,
            a.verificationStatus AS verificationStatus,
            a.status AS allergyStatus,
            c.code AS matched_node_id,
            labels(a)[0] AS matched_label

        """
    _log_query("AllergyIntolerance", q, params)
    return q, params
# ---------- Runner Class ----------
class JsonToCypherRunnerV2:
    def __init__(self, uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASS):
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
    def close(self):
        self._driver.close()
    def _run_and_collect_ids(
        self, cypher: str, params: Optional[Dict] = None
    ) -> List[str]:
        params = params or {}
        logger.debug(
            "Executing cypher (ids):\n%s\nparams=%s",
            cypher,
            json.dumps({k: "<redacted>" for k in params.keys()}),
        )
        with self._driver.session() as session:
            try:
                result = session.run(cypher, params)
                ids: List[str] = []
                for rec in result:
                    if "ids" in rec:
                        ids = rec["ids"] or []
                        break
                    if "id" in rec:
                        ids.append(rec["id"])
                    elif "pid" in rec:
                        ids.append(rec["pid"])
                return list(dict.fromkeys(ids))
            except Exception:
                logger.exception("Error running cypher (ids)")
                raise
   
    def _normalize_obs_row(self, rec):
        rec = dict(rec)
        rec["v"] = (
            rec.get("valueQuantity")
            or rec.get("valueDecimal")
            or rec.get("valueInteger")
            or rec.get("valueString")
        )
        rec["dt"] = (
            rec.get("dt")
            or rec.get("effectiveDateTime")
            or rec.get("issued")
            or rec.get("valueDateTime")
        )
        if rec.get("obsStatus"):
            s = rec["obsStatus"]
            if isinstance(s, dict):
                code = extract_code_from_chain(s, "observation-interpretation")
                if code:
                    rec["obsStatus"] = code.lower()
            elif isinstance(s, str):
                rec["obsStatus"] = s.strip().lower()
        if "pid" in rec and "id" not in rec:
            rec["id"] = rec["pid"]
        return rec
    def _get_all_patient_ids(self) -> List[str]:
        """Fetch all patient IDs once for inclusion batching."""
        with self._driver.session() as session:
            result = session.run("MATCH (p:Patient) RETURN p.id AS id")
            return [r["id"] for r in result]
    def _run_batched_unwind_query(
        self, cypher: str, params: Optional[Dict] = None, batch_size: int = 5000
    ) -> List[Dict[str, Any]]:
        """
        Execute UNWIND-based Cypher queries in batches to avoid transaction timeouts.
        """
        params = params or {}
        prev_ids = params.get("prev_ids") or []
        results: List[Dict[str, Any]] = []
        # If there are no prev_ids, just run normally
        if not prev_ids:
            with self._driver.session() as session:
                res = session.run(cypher, params)
                for rec in res:
                    results.append({k: rec.get(k) for k in rec.keys()})
            return results
        total = len(prev_ids)
        logger.info(
            f"Running batched UNWIND query: {total} IDs (batch size {batch_size})"
        )
        for start in range(0, total, batch_size):
            batch_ids = prev_ids[start : start + batch_size]
            batch_params = dict(params)
            batch_params["prev_ids"] = batch_ids
            with self._driver.session() as session:
                res = session.run(cypher, batch_params)
                for rec in res:
                    results.append({k: rec.get(k) for k in rec.keys()})
            logger.info(
                f"Completed batch {start//batch_size + 1} ({len(batch_ids)} IDs)"
            )
        return results
    def _run_and_collect_patients(
        self, cypher: str, params: Optional[Dict] = None
    ) -> List[Dict[str, Any]]:
        """
        Executes the Cypher query in safe batches if 'prev_ids' is large.
        Avoids transaction timeouts when UNWIND expands large ID lists.
        """
        params = params or {}
        logger.debug(
            "Executing cypher (patients):\n%s\nparams=%s",
            cypher,
            json.dumps({k: "<redacted>" for k in params.keys()}),
        )
        patients: List[Dict[str, Any]] = []
        prev_ids = params.get("prev_ids")
        # Batch execution only if prev_ids exists and is large
        if prev_ids and isinstance(prev_ids, (list, tuple)) and len(prev_ids) > 5000:
            BATCH_SIZE = 5000
            total = len(prev_ids)
            logger.info(
                f"Running batched UNWIND query: {total} IDs (batch size {BATCH_SIZE})"
            )
            with self._driver.session() as session:
                for start in range(0, total, BATCH_SIZE):
                    batch_ids = prev_ids[start : start + BATCH_SIZE]
                    batch_params = params.copy()
                    batch_params["prev_ids"] = batch_ids
                    try:
                        result = session.run(cypher, batch_params)
                        for record in result:
                            patients.append({k: record.get(k) for k in record.keys()})
                        logger.info(
                            f"Completed batch {start//BATCH_SIZE + 1} ({len(batch_ids)} IDs)"
                        )
                    except Exception:
                        logger.exception(
                            f"Error during batch starting at index {start}"
                        )
                        raise
        else:
            # Default single-run execution for smaller queries
            with self._driver.session() as session:
                try:
                    result = session.run(cypher, params)
                    for record in result:
                      patients.append({k: record.get(k) for k in record.keys()})
                except Exception:
                    logger.exception("Error running cypher (patients)")
                    raise
        return patients
    def _run_and_collect_obs(
        self, cypher: str, params: Optional[Dict] = None
    ) -> List[Dict[str, Any]]:
        params = params or {}
        logger.debug(
            "Executing cypher (obs):\n%s\nparams=%s",
            cypher,
            json.dumps({k: "<redacted>" for k in params.keys()}),
        )
        with self._driver.session() as session:
            try:
                rows: List[Dict[str, Any]] = []
                res = session.run(cypher, params)
                for r in res:
                    # collect all returned keys
                    rec = {k: r.get(k) for k in r.keys()}
                    # NORMALIZE OBSERVATION VALUE (CRITICAL)
                    rec["v"] = (
                        rec.get("valueQuantity")
                        or rec.get("valueDecimal")
                        or rec.get("valueInteger")
                        or rec.get("valueString")
                        or rec.get("v")
                    )
                    # NORMALIZE DATETIME (CRITICAL)
                    rec["dt"] = (
                        rec.get("dt")
                        or rec.get("effectiveDateTime")
                        or rec.get("issued")
                        or rec.get("valueDateTime")
                    )
                    # NORMALIZE OBSERVATION STATUS
                    if rec.get("obsStatus"):
                        s = rec.get("obsStatus")
                        if isinstance(s, dict):
                            code = (
                                extract_code_from_chain(s, "observation-interpretation")
                                or s.get("code")
                                or s.get("text")
                                or s.get("display")
                            )
                            if code:
                                rec["obsStatus"] = str(code).lower()
                        elif isinstance(s, str):
                            parsed = extract_code_from_chain(s, "observation-interpretation")
                            if parsed:
                                rec["obsStatus"] = str(parsed).lower()
                            else:
                                rec["obsStatus"] = s.strip().lower()
                    rows.append(rec)
                return rows
            except Exception:
                logger.exception("Error running cypher (obs)")
                raise
    def _fetch_patient_details_by_ids(
        self, ids: Iterable[str]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Given a set/list of patient ids, fetch their details in one query.
        """
        ids = list(ids or [])
        if not ids:
            return {}
        q = """
        MATCH (p:Patient)
        WHERE p.id IN $ids
        RETURN DISTINCT
            p.id AS id,
            p.birthDate AS birthDate,
            p.gender AS gender,
            p.fieldFamilyName AS familyName,
            p.fieldGivenName AS givenName,
            p.address AS addressText,
            p.fieldMaritalStatusText AS maritalStatus,
            p.fieldIdentifierValue AS identifierValue
        """
        params = {"ids": ids}
        rows = self._run_and_collect_patients(q, params)
        return {r["id"]: r for r in rows}
    def _criterion_category_match(
        self,
        category_name: str,
        category_obj: Dict[str, Any],
        previous_ids: Optional[Iterable[str]] = None,
        limit: Optional[int] = None,
    ) -> Tuple[Set[str], Dict[str, Dict[str, Any]]]:
        """
        Returns (set_of_matching_patient_ids, patient_detail_map)
        """
        _merge_term_codes_into_category(category_obj)
        # Support both "codes" and "tests" keys for labs
        raw_codes = []
        if isinstance(category_obj.get("codes"), list):
            raw_codes.extend(category_obj.get("codes") or [])
        if isinstance(category_obj.get("tests"), list):
            raw_codes.extend(category_obj.get("tests") or [])
        # Normalize codes_by_system
        codes_by_system = defaultdict(list)
        for c in raw_codes:
            if not isinstance(c, dict):
                continue
            code = c.get("code")
            system = c.get("system")
            if code and system:
                codes_by_system[system].append(code)
        # --- CONDITION ---
        if category_name == "condition":
            ref_event = category_obj.get("referenceEvent", "now")
            days_after = category_obj.get("daysAfter")
            q, params = _build_condition_query_codes(
                codes_by_system,
                status_field=category_obj.get("statusField"),
                status_values=category_obj.get("statusValues"),
                previous_ids=previous_ids,
                days_before=category_obj.get("daysBefore"),
                ref_event=ref_event,
                days_after=days_after,
                limit=limit,
            )
            if not q:
                return set(), {}
            # Use batching for large queries
            # if previous_ids and len(previous_ids) > 0:
            # records = self._run_batched_unwind_query(q, params, batch_size=5000)
            # else:
            # all_ids = self._get_all_patient_ids()
            # if len(all_ids) > 5000:
            # logger.info(f"Running inclusion {category_name} in batches ({len(all_ids)} patients)")
            # records = self._run_batched_unwind_query(q, {**params, "prev_ids": all_ids}, batch_size=5000)
            # else:
            # records = self._run_and_collect_patients(q, params)
            records = self._run_and_collect_patients(q, params)
           
            # Normalize CodeableConcept-based fields (Condition)
            records = [normalize_codeableconcept_fields(r, "Condition") for r in records]
           
            filtered_records = []
            for r in records:
                if not _python_check_status(r, category_obj):
                    continue
                if not _python_check_date_window(r, category_obj):
                    continue
                filtered_records.append(r)
            records = filtered_records
            matched_set = {r["id"] for r in records if r.get("id")}
            patient_map = {r["id"]: r for r in records if r.get("id")}
            # Handle category-level negation
            if category_obj.get("negation"):
                if previous_ids:
                    universe = set(previous_ids)
                else:
                    universe = set(self._get_all_patient_ids())
                matched_set = universe - matched_set
                missing = [pid for pid in matched_set if pid not in patient_map]
                if missing:
                    extra_map = self._fetch_patient_details_by_ids(missing)
                    patient_map.update(extra_map)
            return {r["id"] for r in records}, {r["id"]: r for r in records}
        # --- MEDICATION ---
        if category_name == "medication":
            ref_event = category_obj.get("referenceEvent", "now")
            days_after = category_obj.get("daysAfter")
            q, params = _build_medication_query_codes(
                codes_by_system,
                status_field=category_obj.get("statusField"),
                status_values=category_obj.get("statusValues"),
                days_before=category_obj.get("daysBefore"),
                previous_ids=previous_ids,
                ref_event=ref_event,
                days_after=days_after,
                limit=limit,
            )
            if not q:
                return set(), {}
            # Use batching
            # if previous_ids and len(previous_ids) > 0:
            # records = self._run_batched_unwind_query(q, params, batch_size=5000)
            # else:
            # all_ids = self._get_all_patient_ids()
            # if len(all_ids) > 5000:
            # logger.info(f"Running inclusion {category_name} in batches ({len(all_ids)} patients)")
            # records = self._run_batched_unwind_query(q, {**params, "prev_ids": all_ids}, batch_size=5000)
            # else:
            # records = self._run_and_collect_patients(q, params)
            records = self._run_and_collect_patients(q, params)
            # Normalize med status fields (medStatus, verificationStatus) into scalars
            records = [normalize_codeableconcept_fields(r, "MedicationRequest") if isinstance(r, dict) else r for r in records]
            # Also try generic normalization in case shape differs
            records = [normalize_codeableconcept_fields(r, None) if isinstance(r, dict) else r for r in records]
            filtered_records = []
            for r in records:
                if not _python_check_status(r, category_obj):
                    continue
                if not _python_check_date_window(r, category_obj):
                    continue
                filtered_records.append(r)
            records = filtered_records
            matched_set = {r["id"] for r in records if r.get("id")}
            patient_map = {r["id"]: r for r in records if r.get("id")}
            # Handle category-level negation
            if category_obj.get("negation"):
                if previous_ids:
                    universe = set(previous_ids)
                else:
                    universe = set(self._get_all_patient_ids())
                matched_set = universe - matched_set
                missing = [pid for pid in matched_set if pid not in patient_map]
                if missing:
                    extra_map = self._fetch_patient_details_by_ids(missing)
                    patient_map.update(extra_map)
            # --- Python-side temporal filtering ---
            days_before = category_obj.get("daysBefore")
            days_after = category_obj.get("daysAfter")
            ref_event = category_obj.get("referenceEvent", "now")
            if days_before or days_after:
                from dateutil import parser
                from datetime import datetime, timedelta
                filtered_records = []
                now = datetime.utcnow()
                for r in records:
                    keep = True
                    for field in ["authoredOn", "effectiveDateTime", "issued"]:
                        val = r.get(field)
                        if not val:
                            continue
                        try:
                            dt = parser.parse(val)
                            if days_before and (now - dt).days > days_before:
                                keep = False
                            if days_after and (dt - now).days > days_after:
                                keep = False
                        except Exception:
                            continue
                    if keep:
                        filtered_records.append(r)
                records = filtered_records
            return {r["id"] for r in records}, {r["id"]: r for r in records}
        # --- OBSERVATION / LAB / DIAGNOSTICREPORT ---
        if category_name in ("lab", "observation", "diagnosticreport"):
            change = category_obj.get("change")
            value_filter = category_obj.get("value")
            days_before = category_obj.get("daysBefore")
            days_after = category_obj.get("daysAfter")
            ref_event = category_obj.get("referenceEvent", "now")
            if change:
                window_days = int(days_before) if days_before else 365
                q, params = _build_observation_query_codes(
                    codes_by_system,
                    value_filter=None,
                    days_before=window_days,
                    previous_ids=previous_ids,
                    return_values=True,
                    ref_event=ref_event,
                    days_after=days_after,
                    limit=limit,
                )
                if not q:
                    return set(), {}
                # Use batching
                if previous_ids and len(previous_ids) > 0:
                    rows = self._run_batched_unwind_query(q, params, batch_size=5000)
                    rows = [self._normalize_obs_row(r) for r in rows]
                else:
                    all_ids = self._get_all_patient_ids()
                    if len(all_ids) > 5000:
                        logger.info(
                            f"Running inclusion {category_name} in batches ({len(all_ids)} patients)"
                        )
                        rows = self._run_batched_unwind_query(
                            q, {**params, "prev_ids": all_ids}, batch_size=5000
                        )
                    else:
                        rows = self._run_and_collect_obs(q, params)
                         # rows already normalized by _run_and_collect_obs
                        for r in rows:
                            s = r.get("obsStatus")
                            if isinstance(s, str) and s:
                                r["obsStatus"] = s.strip().lower()
                by_pid = defaultdict(list)
                for r in rows:
                    pid = r.get("pid")
                    if pid is None:
                        continue
                    val = _parse_value_quantity(r.get("v"))
                    dt = r.get("dt")
                    by_pid[pid].append({"dt": dt, "v": val})
                matched = set()
                op = change.get("operator") or ""
                try:
                    delta_threshold = float(change.get("value"))
                except Exception:
                    delta_threshold = 0.0
                overall_value = category_obj.get("value") or {}
                lower = overall_value.get("lower")
                upper = overall_value.get("upper")
                for pid, samples in by_pid.items():
                    def _safe_dt(x):
                        try:
                            return _dt_parser.parse(x["dt"])
                        except Exception:
                            return _dt.min
                    samples_sorted = sorted(samples, key=_safe_dt)
                    if not samples_sorted:
                        continue
                    baseline = samples_sorted[0]["v"]
                    followup = samples_sorted[-1]["v"]
                    if baseline is None or followup is None:
                        continue
                    try:
                        delta = float(followup) - float(baseline)
                    except Exception:
                        continue
                    ok_delta = False
                    if op == ">=":
                        ok_delta = delta >= delta_threshold
                    elif op == ">":
                        ok_delta = delta > delta_threshold
                    elif op == "<=":
                        ok_delta = delta <= delta_threshold
                    elif op == "<":
                        ok_delta = delta < delta_threshold
                    else:
                        ok_delta = delta >= delta_threshold
                    ok_overall = True
                    if lower is not None:
                        try:
                            ok_overall = ok_overall and (
                                float(followup) >= float(lower)
                            )
                        except Exception:
                            ok_overall = False
                    if upper is not None:
                        try:
                            ok_overall = ok_overall and (
                                float(followup) <= float(upper)
                            )
                        except Exception:
                            ok_overall = False
                    if ok_delta and ok_overall:
                        matched.add(pid)
                patient_map = self._fetch_patient_details_by_ids(matched)
                return matched, patient_map
            else:
                q, params = _build_observation_query_codes(
                    codes_by_system,
                    value_filter=None,
                    days_before=days_before,
                    previous_ids=previous_ids,
                    return_values=True,
                    ref_event=ref_event,
                    days_after=days_after,
                    limit=limit,
                )
                if not q:
                    return set(), {}
                # use batching
                # if previous_ids and len(previous_ids) > 0:
                # rows = self._run_batched_unwind_query(q, params, batch_size=5000)
                # else:
                # all_ids = self._get_all_patient_ids()
                # if len(all_ids) > 5000:
                # logger.info(f"Running inclusion {category_name} in batches ({len(all_ids)} patients)")
                # rows = self._run_batched_unwind_query(q, {**params, "prev_ids": all_ids}, batch_size=5000)
                # else:
                # rows = self._run_and_collect_obs(q, params)
                rows = self._run_and_collect_obs(q, params)
                # rows already normalized by _run_and_collect_obs
                for r in rows:
                    s = r.get("obsStatus")
                    if isinstance(s, str) and s:
                        r["obsStatus"] = s.strip().lower()
               
                by_pid = defaultdict(list)
                for r in rows:
                    pid = r.get("pid")
                    if not pid:
                        continue
                    raw_val = r.get("v")
                    num_val = _parse_value_quantity(raw_val)
                    if num_val is None:
                        continue
                    by_pid[pid].append(num_val)
                matched = set()
                if isinstance(value_filter, dict):
                    op = (value_filter.get("operator") or "").lower()
                    val_raw = value_filter.get("value")
                    # Qualitative (string)
                    if (
                        isinstance(val_raw, str)
                        and not val_raw.replace(".", "", 1).isdigit()
                    ):
                        cmp_val = val_raw.strip().lower()
                        for pid, values in by_pid.items():
                            for v in values:
                                vs = str(v).strip().lower()
                                if cmp_val == vs:
                                    matched.add(pid)
                                    break
                    else:
                        try:
                            cmp_val = float(val_raw)
                        except Exception:
                            cmp_val = None
                        if cmp_val is not None:
                            for pid, values in by_pid.items():
                                for v in values:
                                    num_val = _parse_value_quantity(v)
                                    if num_val is None:
                                        continue
                                    if op in (">", "gt") and num_val > cmp_val:
                                        matched.add(pid)
                                        break
                                    if op in (">=", "gte") and num_val >= cmp_val:
                                        matched.add(pid)
                                        break
                                    if op in ("<", "lt") and num_val < cmp_val:
                                        matched.add(pid)
                                        break
                                    if op in ("<=", "lte") and num_val <= cmp_val:
                                        matched.add(pid)
                                        break
                                    if op in ("=", "==", "eq") and num_val == cmp_val:
                                        matched.add(pid)
                                        break
                    if op == "between":
                        try:
                            lower = float(value_filter.get("lower"))
                            upper = float(value_filter.get("upper"))
                            for pid, values in by_pid.items():
                                for num_val in values:
                                    if (
                                        num_val is not None
                                        and lower <= num_val <= upper
                                    ):
                                        matched.add(pid)
                                        break
                        except Exception:
                            pass
                patient_map = self._fetch_patient_details_by_ids(matched)
                return matched, patient_map
        # --- ALLERGY ---
        if category_name == "allergy":
            ref_event = category_obj.get("referenceEvent", "now")
            days_after = category_obj.get("daysAfter")
            q, params = _build_allergy_query_codes(
                codes_by_system,
                status_field=category_obj.get("statusField"),
                status_values=category_obj.get("statusValues"),
                days_before=category_obj.get("daysBefore"),
                previous_ids=previous_ids,
                ref_event=ref_event,
                days_after=days_after,
                limit=limit,
            )
            if not q:
                return set(), {}
            # Use batching
            # if previous_ids and len(previous_ids) > 0:
            # records = self._run_batched_unwind_query(q, params, batch_size=5000)
            # else:
            # all_ids = self._get_all_patient_ids()
            # if len(all_ids) > 5000:
            # logger.info(f"Running inclusion {category_name} in batches ({len(all_ids)} patients)")
            # records = self._run_batched_unwind_query(q, {**params, "prev_ids": all_ids}, batch_size=5000)
            # else:
            # records = self._run_and_collect_patients(q, params)
            records = self._run_and_collect_patients(q, params)
           
            records = [normalize_codeableconcept_fields(r, "AllergyIntolerance") for r in records]
            filtered_records = []
            for r in records:
                if not _python_check_status(r, category_obj):
                    continue
                if not _python_check_date_window(r, category_obj):
                    continue
                filtered_records.append(r)
            records = filtered_records
            matched_set = {r["id"] for r in records if r.get("id")}
            patient_map = {r["id"]: r for r in records if r.get("id")}
            # Handle category-level negation
            if category_obj.get("negation"):
                if previous_ids:
                    universe = set(previous_ids)
                else:
                    universe = set(self._get_all_patient_ids())
                matched_set = universe - matched_set
                missing = [pid for pid in matched_set if pid not in patient_map]
                if missing:
                    extra_map = self._fetch_patient_details_by_ids(missing)
                    patient_map.update(extra_map)
            return {r["id"] for r in records}, {r["id"]: r for r in records}
        # --- DEMOGRAPHICS ---
        if category_name == "demographics":
            q, params = _build_demographics_query(
                category_obj, previous_ids, limit=limit
            )
            if not q:
                return set(), {}
            # Use batching
            # if previous_ids and len(previous_ids) > 0:
            # records = self._run_batched_unwind_query(q, params, batch_size=5000)
            # else:
            # all_ids = self._get_all_patient_ids()
            # if len(all_ids) > 5000:
            # logger.info(f"Running inclusion {category_name} in batches ({len(all_ids)} patients)")
            # records = self._run_batched_unwind_query(q, {**params, "prev_ids": all_ids}, batch_size=5000)
            # else:
            records = self._run_and_collect_patients(q, params)
           
            filtered_records = []
            for r in records:
                if not _python_check_status(r, category_obj):
                    continue
                if not _python_check_date_window(r, category_obj):
                    continue
                filtered_records.append(r)
            records = filtered_records
            matched_set = {r["id"] for r in records if r.get("id")}
            patient_map = {r["id"]: r for r in records if r.get("id")}
            # Handle category-level negation
            if category_obj.get("negation"):
                if previous_ids:
                    universe = set(previous_ids)
                else:
                    universe = set(self._get_all_patient_ids())
                matched_set = universe - matched_set
                missing = [pid for pid in matched_set if pid not in patient_map]
                if missing:
                    extra_map = self._fetch_patient_details_by_ids(missing)
                    patient_map.update(extra_map)
            return {r["id"] for r in records}, {r["id"]: r for r in records}
        return set(), {}
    def _combine_category_results(
        self, results: List[Set[str]], logic: str
    ) -> Set[str]:
        if not results:
            return set()
        logic = _normalize_logic(logic, default="AND")
        if logic == "AND":
            out = results[0].copy()
            for s in results[1:]:
                out.intersection_update(s)
            return out
        else:
            out = set()
            for s in results:
                out.update(s)
            return out
    def _run_single_criterion(
        self, criterion: Dict[str, Any], restrict_to: Optional[Iterable[str]] = None
    ) -> Tuple[Set[str], Dict[str, Dict[str, Any]]]:
        """
        Run a single inclusion or exclusion criterion by evaluating all its categories
        (condition, medication, observation, demographics, etc.), combining results
        according to its logic (AND/OR). Optionally restrict evaluation to a subset
        of patients (restrict_to) for performance.
        """
        category_map = criterion.get("categories", {}) or {}
        per_cat_results: List[Set[str]] = []
        per_cat_patient_maps: List[Dict[str, Dict[str, Any]]] = []
        # Evaluate each category and collect results
        for cat_name, cat_obj in category_map.items():
            try:
                matched_ids, patient_map = self._criterion_category_match(
                    cat_name, cat_obj, previous_ids=restrict_to # pass restriction set
                )
            except Exception as e:
                logger.error(f"Category {cat_name} failed: {e}", exc_info=True)
                matched_ids, patient_map = set(), {}
            per_cat_results.append(matched_ids)
            per_cat_patient_maps.append(patient_map)
        # Combine category results AFTER evaluating all categories
        logic = _normalize_logic(criterion.get("logic"), default="AND")
        combined_ids = self._combine_category_results(per_cat_results, logic)
        # Build combined patient details for the matched ids
        combined_patients: Dict[str, Dict[str, Any]] = {}
        for pm in per_cat_patient_maps:
            for pid, info in pm.items():
                if pid in combined_ids:
                    combined_patients[pid] = info
        return set(combined_ids), combined_patients
    def run(
        self, criteria_json: Dict[str, Any], nct_id: Optional[str] = "NCT_UNKNOWN"
    ) -> Dict[str, Any]:
        """
        Main runner with independent inclusion/exclusion evaluation.
        Adds nct_id tagging and filters exclusion queries to only inclusion hits.
        """
        criteria_json = self._normalize_llm_structure(criteria_json)
        inc_list = (
            criteria_json.get("inclusion_criteria")
            or criteria_json.get("inclusion")
            or []
        )
        exc_list = (
            criteria_json.get("exclusion_criteria")
            or criteria_json.get("exclusion")
            or []
        )
        limit = criteria_json.get("constraints", {}).get("limit")
        per_criterion_matches: Dict[int, Set[str]] = {}
        inclusion_map: Dict[int, Set[str]] = {}
        exclusion_map: Dict[int, Set[str]] = {}
        patient_detail_map: Dict[str, Dict[str, Any]] = {}
        patient_match_records: List[Dict[str, Any]] = []
        logger.info("Starting inclusion criteria processing...")
        # ---------------------------
        # Step 1: Run all inclusion criteria independently
        # ---------------------------
        for idx, crit in enumerate(inc_list):
            cid = int(crit.get("id", idx))
            criteria_text = (crit.get("description") or crit.get("text") or crit.get("criterion") or "").strip()
            logger.info(
                f"Processing inclusion criterion {cid}: {crit.get('description')}"
            )
            try:
                matched_ids, patient_map = self._run_single_criterion(crit)
                inclusion_map[cid] = set(matched_ids)
                per_criterion_matches[cid] = set(matched_ids)
                patient_detail_map.update(patient_map)
                for pid in matched_ids:
                    info = patient_map.get(pid, {}) or {}
                    mid = info.get("matched_node_id") or info.get("code")
                    lab = info.get("matched_label") or "Condition"
                    if not mid:
                        mid = f"criterion_{cid}"
                    if criteria_text and (not lab or lab == "Condition"):
                        lab = criteria_text[:120]

                    patient_match_records.append(
                        {
                            "claim_id": pid,
                            "nct_id": nct_id,
                            "ie": "I",
                            "criteria_index": cid,
                            "model_pred": 1,
                            "criteria_text": criteria_text,
                            "pred_list": [
                                {
                                    "matched_label": lab,
                                    "matched_node_id": str(mid),
                                    "criteria_index": cid,
                                }
                            ],
                        }
                    )

                logger.info(f"Inclusion {cid}: {len(matched_ids)} matched patients")
            except Exception as e:
                logger.error(f"Inclusion criterion {cid} failed: {e}", exc_info=True)
                inclusion_map[cid] = set()
        # Union of all patients who matched at least one inclusion
        inclusion_union = set().union(*inclusion_map.values())
        logger.info(
            f"Total unique patients after all inclusions: {len(inclusion_union)}"
        )
        # ---------------------------
        # Step 2: Run exclusion criteria only on inclusion hits
        # ---------------------------
        if inclusion_union:
            logger.info("Starting exclusion criteria (filtered on inclusion hits)...")
            for idx, crit in enumerate(exc_list):
                cid = int(crit.get("id", idx))
                criteria_text = (crit.get("description") or crit.get("text") or crit.get("criterion") or "").strip()
                logger.info(
                    f"Processing exclusion criterion {cid}: {crit.get('description')}"
                )
                try:
                    matched_ids, _ = self._run_single_criterion(
                        crit, restrict_to=inclusion_union
                    )
                    exclusion_map[cid] = set(matched_ids)
                    for pid in matched_ids:
                        info = patient_detail_map.get(pid, {}) or {}
                        mid = info.get("matched_node_id") or info.get("code")
                        lab = info.get("matched_label") or "Condition"
                        if not mid:
                            mid = f"criterion_{cid}"
                        if criteria_text and (not lab or lab == "Condition"):
                            lab = criteria_text[:120]

                        patient_match_records.append(
                            {
                                "claim_id": pid,
                                "nct_id": nct_id,
                                "ie": "E",
                                "criteria_index": cid,
                                "model_pred": 1,
                                "criteria_text": criteria_text,
                                "pred_list": [
                                    {
                                        "matched_label": lab,
                                        "matched_node_id": str(mid),
                                        "criteria_index": cid,
                                    }
                                ],
                            }
                        )
                    logger.info(
                        f" Exclusion {cid}: {len(matched_ids)} matched patients"
                    )
                except Exception as e:
                    logger.error(
                        f"Exclusion criterion {cid} failed: {e}", exc_info=True
                    )
                    exclusion_map[cid] = set()
        else:
            logger.info("No inclusion matches found — skipping exclusions.")
            for idx, crit in enumerate(exc_list):
                exclusion_map[int(crit.get("id", idx))] = set()
        # ---------------------------
        # Step 3: Aggregate scoring & buckets (Dynamic % version)
        # ---------------------------
        patient_score = defaultdict(int)
        pool_ids = set().union(*inclusion_map.values())
        for cid, ids in inclusion_map.items():
            for pid in ids:
                patient_score[pid] += 1
        total_criteria = max(1, len(inclusion_map))
        match_results = []
        # Create dynamic % buckets (e.g., 25%, 50%, 75%, 100%)
        buckets = {
            f"{round((i / total_criteria) * 100)}%": []
            for i in range(1, total_criteria + 1)
        }
        buckets["Excluded"] = []
        for pid in pool_ids:
            inc_hits = patient_score.get(pid, 0)
            match_percent = (inc_hits / total_criteria) * 100.0 if total_criteria else 0
            exc_hits = sum(pid in exclusion_map[cid] for cid in exclusion_map)
            detail = patient_detail_map.get(pid, {})
            # Determine bucket dynamically
            if exc_hits > 0:
                bucket = "Excluded"
            else:
                percent_step = round((inc_hits / total_criteria) * 100)
                available_keys = [
                    int(k.rstrip("%")) for k in buckets.keys() if k != "Excluded"
                ]
                nearest = min(available_keys, key=lambda x: abs(x - percent_step))
                bucket = f"{nearest}%"
            # --- Enhanced criteria text assignment (unchanged) ---
            inclusion_texts = [
                rec["criteria_text"].strip()
                for rec in patient_match_records
                if rec["claim_id"] == pid and rec.get("ie", "").lower() == "i" and rec.get("criteria_text")
            ]
            exclusion_texts = [
                rec["criteria_text"].strip()
                for rec in patient_match_records
                if rec["claim_id"] == pid and rec.get("ie", "").lower() == "e" and rec.get("criteria_text")
            ]
            def _unique(seq):
                seen = set()
                out = []
                for x in seq:
                    if x and x not in seen:
                        seen.add(x)
                        out.append(x)
                return out
            inclusion_texts = _unique(inclusion_texts)
            exclusion_texts = _unique(exclusion_texts)
            if exclusion_texts:
                pid_texts = exclusion_texts
            elif inclusion_texts:
                pid_texts = inclusion_texts
            else:
                pid_texts = []
            criteria_text = "; ".join(pid_texts)
            record = {
                "claim_id": pid,
                "nct_id": nct_id,
                "inclusion_hits": inc_hits,
                "exclusion_hits": exc_hits,
                "match_percent": match_percent,
                "bucket": bucket,
                "details": detail,
                "criteria_text": criteria_text,
            }
            match_results.append(record)
            buckets[bucket].append(record)
        # --- Choose highest non-empty bucket ---
        non_empty = [b for b in buckets if buckets[b] and b != "Excluded"]
        highest_bucket = (
            sorted(non_empty, key=lambda k: int(k.rstrip("%")), reverse=True)[0]
            if non_empty else "Excluded"
        )
        final_patients = [r for r in match_results if r["bucket"] == highest_bucket]
        # Extract numeric percent from highest_bucket for display
        if highest_bucket != "Excluded":
            final_match_percent = int(highest_bucket.rstrip("%"))
        else:
            final_match_percent = 0
        out = {
            "nct_id": nct_id,
            "inclusion_counts": {cid: len(v) for cid, v in inclusion_map.items()},
            "exclusion_counts": {cid: len(v) for cid, v in exclusion_map.items()},
            "final_count": len(final_patients),
            "final_patients": [r["details"] for r in final_patients],
            "final_match_percent": f"{final_match_percent}%",
            "match_buckets": buckets,
            "match_results": match_results,
            "patient_match_records": patient_match_records,
        }
        logger.info(
            f"Run complete: {len(final_patients)} final matches in top bucket ({highest_bucket}) for trial {nct_id}"
        )
        return out
    def _normalize_llm_structure(self, criteria_json):
        """
        Normalize the JSON input from LLM to match what cypher_generator expects.
        Handles:
        - Converts codes_by_system → flat codes[]
        - Ensures each code entry has 'system' and 'code'
        - Converts value[] → single dict (splits multiple labs)
        - Handles operator='between' with value=[lower, upper]
        - Normalizes daysBefore / daysAfter fields
        - Adds default logic and negation keys
        - Works for all categories (condition, medication, lab, observation, diagnosticreport, allergy)
         merges per-term 'term_details' into category-level fields so the runner
        will not miss term-level codes/values/temporal info while preserving the
        original per-term structure.
        """
        import copy
        import logging
        logger = logging.getLogger("cypher_normalizer")
        if not criteria_json:
            return criteria_json
        normalized = copy.deepcopy(criteria_json)
        if isinstance(normalized.get("inclusion"), list):
            normalized["inclusion"] = [
                {"id": i + 1, "description": c} if isinstance(c, str) else c
                for i, c in enumerate(normalized["inclusion"])
            ]
        if isinstance(normalized.get("exclusion"), list):
            normalized["exclusion"] = [
                {"id": i + 1, "description": c} if isinstance(c, str) else c
                for i, c in enumerate(normalized["exclusion"])
            ]
        # ---------------------------------------------------------------------
        # Helper: dedupe codes_by_system while preserving order
        # Input: { system_uri: [ {code:..., display:...} or 'code' , ... ], ... }
        # Output: same shape with duplicates removed per (system,code)
        # ---------------------------------------------------------------------
        def _dedupe_codes_by_system(codes_by_system):
            out = {}
            for system, entries in (codes_by_system or {}).items():
                seen = set()
                new_entries = []
                if not entries:
                    continue
                for e in entries:
                    if e is None:
                        continue
                    if isinstance(e, dict):
                        code = e.get("code") or e.get("id") or e.get("value")
                        display = e.get("display")
                        if not code:
                            continue
                        key = (system, str(code))
                        if key in seen:
                            continue
                        seen.add(key)
                        if display is not None:
                            new_entries.append({"code": str(code), "display": str(display)})
                        else:
                            new_entries.append({"code": str(code)})
                    else:
                        code = str(e)
                        key = (system, code)
                        if key in seen:
                            continue
                        seen.add(key)
                        new_entries.append({"code": code})
                if new_entries:
                    out[system] = new_entries
            return out
        # -----------------------------------------------------
        # Normalize codes_by_system → codes[]
        # -----------------------------------------------------
        def _normalize_codes(cat):
            """Flatten codes_by_system to a simple codes[] list."""
            if not isinstance(cat, dict):
                return cat
            # Convert codes_by_system to flat codes[]
            if "codes_by_system" in cat and "codes" not in cat:
                new_codes = []
                for system, entries in cat["codes_by_system"].items():
                    if not entries:
                        continue
                    for e in entries:
                        if isinstance(e, dict):
                            code = e.get("code") or e.get("id") or e.get("value")
                            if code:
                                new_codes.append({"system": system, "code": str(code)})
                        else:
                            new_codes.append({"system": system, "code": str(e)})
                cat["codes"] = new_codes
                cat.pop("codes_by_system", None)
            # Ensure all entries have both system and code
            if isinstance(cat.get("codes"), list):
                fixed = []
                for c in cat["codes"]:
                    if not isinstance(c, dict):
                        continue
                    system = c.get("system") or c.get("systemUri") or "unknown"
                    code = c.get("code") or c.get("id") or c.get("value")
                    if code:
                        fixed.append({"system": str(system), "code": str(code)})
                cat["codes"] = fixed
            return cat
        # -----------------------------------------------------
        # Flatten value[] lists and fix 'between' arrays
        # -----------------------------------------------------
        def _flatten_value_list(crit, category):
            cats = crit.get("categories", {})
            cat = cats.get(category)
            if not cat:
                return [crit]
            value_field = cat.get("value")
            # Case: value is a plain list with operator defined at same level
            if isinstance(value_field, list) and isinstance(cat.get("operator"), str):
                op = cat.get("operator")
                cat["value"] = {"operator": op, "value": value_field}
                value_field = cat["value"]
            # Case A: Already dict but might have "between" array
            if isinstance(value_field, dict):
                op = (value_field.get("operator") or "").lower()
                val = value_field.get("value")
                if op == "between" and isinstance(val, list) and len(val) == 2:
                    value_field["lower"], value_field["upper"] = val
                    value_field.pop("value", None)
                    cat["value"] = value_field
                    crit["categories"][category] = cat
                return [crit]
            # Case B: If value is not list of dicts → nothing to flatten
            if not isinstance(value_field, list):
                return [crit]
            # Case C: Multiple lab criteria → split into separate criteria
            split_criteria = []
            for v in value_field:
                new_c = copy.deepcopy(crit)
                val_dict = {}
                op = v.get("operator")
                val = v.get("value")
                code = v.get("code") or v.get("id")
                if op:
                    val_dict["operator"] = op
                # Numeric or range handling
                if isinstance(val, list) and op == "between" and len(val) == 2:
                    val_dict["lower"], val_dict["upper"] = val
                elif val is not None:
                    val_dict["value"] = val
                # Threshold operators
                if op in (">=", ">") and isinstance(val, (int, float)):
                    val_dict["lower"] = val
                elif op in ("<=", "<") and isinstance(val, (int, float)):
                    val_dict["upper"] = val
                if code:
                    val_dict["related_code"] = code
                new_c["categories"][category]["value"] = val_dict
                split_criteria.append(new_c)
            return split_criteria
        # -----------------------------------------------------
        # Normalize each category block
        # -----------------------------------------------------
        def _normalize_category(cat):
            """Ensure each category has logic, negation, daysBefore/After, and normalized values."""
            if not isinstance(cat, dict):
                return cat
            # Codes normalization (category-level)
            cat = _normalize_codes(cat)
            # Logic default
            if not cat.get("logic"):
                cat["logic"] = "OR"
            # Negation default
            if "negation" not in cat:
                cat["negation"] = False
            # DaysBefore/DaysAfter normalization
            if "daysBefore" in cat and cat["daysBefore"] is not None:
                try:
                    cat["daysBefore"] = int(cat["daysBefore"])
                except Exception:
                    cat["daysBefore"] = None
            if "daysAfter" in cat and cat["daysAfter"] is not None:
                try:
                    cat["daysAfter"] = int(cat["daysAfter"])
                except Exception:
                    cat["daysAfter"] = None
            # Normalize simple value types (e.g., "pregnant", 100, ["HIV", "positive"])
            val = cat.get("value")
            if isinstance(val, str):
                # Convert string into operator dict
                cat["value"] = {"operator": "=", "value": val.strip()}
            elif isinstance(val, (int, float)):
                cat["value"] = {"operator": "=", "value": val}
            elif isinstance(val, list) and all(isinstance(v, str) for v in val):
                # list of qualitative terms (e.g., ["HIV", "pregnant"])
                cat["value"] = {"operator": "IN", "values": val}
            return cat
        # -----------------------------------------------------
        # Process inclusion/exclusion lists
        # -----------------------------------------------------
        def _process_criteria_list(lst):
            normalized_list = []
            for crit in lst:
                cats = crit.get("categories", {}) or {}
                # Normalize all category blocks (codes, logic, negation, temporal)
                for cname, cobj in cats.items():
                    cats[cname] = _normalize_category(cobj)
                crit["categories"] = cats
                # Normalize criterion-level logic name
                criterion_logic = crit.get("logic") or crit.get("criterion_logic", "AND")
                crit["logic"] = criterion_logic # unify the field name
                if "criterion_logic" in crit:
                    crit.pop("criterion_logic", None) # remove redundant key
                # Flatten value structures for lab/observation/diagnosticreport
                flattened = False
                for key in ("lab", "observation", "diagnosticreport"):
                    if key in cats:
                        split_criteria = _flatten_value_list(crit, key)
                        normalized_list.extend(split_criteria)
                        flattened = True
                        break
                if not flattened:
                    normalized_list.append(crit)
            return normalized_list
        # -----------------------------------------------------
        # Apply normalization globally
        # -----------------------------------------------------
        for key in (
            "inclusion_criteria",
            "inclusion",
            "exclusion_criteria",
            "exclusion",
        ):
            if key in normalized and isinstance(normalized[key], list):
                normalized[key] = _process_criteria_list(normalized[key])
        # ---------------------------------------------------------------------
        # Merge per-term term_details into category-level fields so runner
        # will not ignore term-specific codes/values/temporal info.
        # ---------------------------------------------------------------------
        for list_key in ("inclusion_criteria", "inclusion", "exclusion_criteria", "exclusion"):
            if list_key not in normalized or not isinstance(normalized[list_key], list):
                continue
            for crit in normalized[list_key]:
                cats = crit.get("categories", {}) or {}
                for cname, cobj in cats.items():
                    # only process dicts
                    if not isinstance(cobj, dict):
                        continue
                    term_details = cobj.get("term_details") or {}
                    if not isinstance(term_details, dict) or not term_details:
                        continue
                    # aggregate codes_by_system from term_details
                    aggregated_codes = {}
                    for term, td in term_details.items():
                        if not isinstance(td, dict):
                            continue
                        t_codes = td.get("codes_by_system") or {}
                        if isinstance(t_codes, dict):
                            for sys, entries in t_codes.items():
                                if not entries:
                                    continue
                                aggregated_codes.setdefault(sys, []).extend(entries if isinstance(entries, list) else [entries])
                    # dedupe aggregated_codes
                    if aggregated_codes:
                        aggregated_codes = _dedupe_codes_by_system(aggregated_codes)
                        # if category already has codes_by_system, merge
                        existing = cobj.get("codes_by_system") or {}
                        # merge existing and aggregated (existing first to preserve category preference)
                        merged = {}
                        for sys, ents in existing.items():
                            merged.setdefault(sys, []).extend(ents if isinstance(ents, list) else [ents])
                        for sys, ents in aggregated_codes.items():
                            merged.setdefault(sys, []).extend(ents if isinstance(ents, list) else [ents])
                        # final dedupe
                        merged = _dedupe_codes_by_system(merged)
                        cobj["codes_by_system"] = merged
                        # IMPORTANT: flatten merged codes_by_system → codes[]
                        # so _criterion_category_match can see them
                        if "codes_by_system" in cobj:
                            _normalize_codes(cobj)
                    # propagate other term-level simple fields into category when missing
                    # (value, change, daysBefore/After, referenceEvent, statusField/statusValues, negation, visit_window)
                    # If multiple terms provide different values, prefer existing category field or leave as-is (do not overwrite)
                    # value: if category has no value and there is at least one term with value, pick the first non-null
                    if cobj.get("value") in (None, {}, []) or "value" not in cobj:
                        for td in term_details.values():
                            if isinstance(td, dict) and td.get("value") not in (None, {}, []):
                                cobj["value"] = td.get("value")
                                break
                    # change: gather if any term has change -> if exactly one unique change, set it, otherwise set category change to list
                    changes = []
                    for td in term_details.values():
                        if isinstance(td, dict) and td.get("change") not in (None, {}, []):
                            changes.append(td.get("change"))
                    if changes:
                        # dedupe by repr
                        uniq = []
                        seen = set()
                        for ch in changes:
                            key = json.dumps(ch, sort_keys=True) if isinstance(ch, dict) else str(ch)
                            if key not in seen:
                                seen.add(key)
                                uniq.append(ch)
                        cobj["change"] = uniq[0] if len(uniq) == 1 else uniq
                    # daysBefore/daysAfter: if missing at category, and all term values are the same (or first non-null), set it
                    for fld in ("daysBefore", "daysAfter"):
                        if cobj.get(fld) is None:
                            vals = [td.get(fld) for td in term_details.values() if isinstance(td, dict) and td.get(fld) is not None]
                            if vals:
                                # if all same, choose that, else pick first non-null (conservative)
                                if all(v == vals[0] for v in vals):
                                    cobj[fld] = vals[0]
                                else:
                                    cobj[fld] = vals[0]
                    # referenceEvent
                    if not cobj.get("referenceEvent"):
                        refs = [td.get("referenceEvent") for td in term_details.values() if isinstance(td, dict) and td.get("referenceEvent")]
                        if refs:
                            if all(r == refs[0] for r in refs):
                                cobj["referenceEvent"] = refs[0]
                            else:
                                # prefer a canonical one if present else keep first
                                cobj["referenceEvent"] = refs[0]
                    # statusField/statusValues
                    if not cobj.get("statusField"):
                        for td in term_details.values():
                            if isinstance(td, dict) and td.get("statusField"):
                                cobj["statusField"] = td.get("statusField")
                                cobj["statusValues"] = td.get("statusValues")
                                break
                    # negation
                    if "negation" not in cobj or cobj.get("negation") is None:
                        for td in term_details.values():
                            if isinstance(td, dict) and td.get("negation") is not None:
                                cobj["negation"] = bool(td.get("negation"))
                                break
                    # visit_window
                    if not cobj.get("visit_window"):
                        vws = [td.get("visit_window") for td in term_details.values() if isinstance(td, dict) and td.get("visit_window")]
                        if vws:
                            if all(v == vws[0] for v in vws):
                                cobj["visit_window"] = vws[0]
                            else:
                                # multiple windows present — join them for transparency
                                cobj["visit_window"] = ",".join(sorted({str(v) for v in vws if v}))
                    # write back
                    crit["categories"][cname] = cobj
        def _ensure_criterion_ids(lst):
            if not isinstance(lst, list):
                return lst
            for i, crit in enumerate(lst):
                if not isinstance(crit, dict):
                    continue
                if crit.get("id") is None:
                    crit["id"] = i
            return lst

        for list_key in (
            "inclusion_criteria",
            "inclusion",
            "exclusion_criteria",
            "exclusion",
        ):
            if list_key in normalized:
                normalized[list_key] = _ensure_criterion_ids(normalized[list_key])

        logger.info("LLM structure normalized successfully")
        return normalized