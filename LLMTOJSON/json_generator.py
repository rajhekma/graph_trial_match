import os
import re
import json
import time
import logging
from typing import Optional, Dict, Any, Tuple, Union, List
from dotenv import load_dotenv
from openai import OpenAI

# ---------- Setup ----------
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LLMTOJSON_v3")

# environment-based configuration (sensible defaults)
MAX_TOKENS_CATEGORY = int(os.getenv("MAX_TOKENS_CATEGORY", "6000"))
MAX_CODES_PER_SYSTEM = int(os.getenv("MAX_CODES_PER_SYSTEM", "200"))
CATEGORY_EXPAND_RETRIES = int(os.getenv("CATEGORY_EXPAND_RETRIES", "2"))
CLASSIFY_RETRIES = int(os.getenv("CLASSIFY_RETRIES", "2"))
CLASSIFY_MODEL = os.getenv("CLASSIFY_MODEL", "gpt-4o")
EXPAND_MODEL = os.getenv("EXPAND_MODEL", "gpt-4o")
CLASSIFY_MAX_TOKENS = int(os.getenv("CLASSIFY_MAX_TOKENS", "1500"))

# OpenAI client
_api_key = os.getenv("OPENAI_API_KEY")
_client_instance = None


def _get_openai_client():
    global _client_instance
    if _client_instance is not None:
        return _client_instance
    try:
        if _api_key:
            _client_instance = OpenAI(api_key=_api_key)
        else:
            _client_instance = OpenAI()
    except Exception as e:
        logger.warning("OpenAI client init warning: %s", e)
        _client_instance = OpenAI()
    return _client_instance


# ---------- Safe JSON parsing ----------
def safe_parse_json(content: str) -> Dict[str, Any]:
    """
    Robust JSON parser for LLM outputs.
    Attempts:
      1) direct json.loads
      2) mild repairs: strip code fences, remove leading/trailing garbage,
         fix trailing commas, balanced braces, replace smart quotes,
         conservative single->double quote replacement heuristic
      3) final fallback raises ValueError
    Returns parsed dict/list.
    """
    if content is None:
        raise ValueError("No content provided to parse")

    text = content.strip()

    # strip triple backticks and language fences
    text = re.sub(r"^```[\w]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)

    # Normalize smart quotes and weird unicode
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)

    # Direct parse attempt
    try:
        return json.loads(text)
    except Exception as e_first:
        # mild repairs
        repaired = text

        # find the first { or [ and last } or ] and slice
        sidx = repaired.find("{")
        bidx = repaired.find("[")
        if sidx == -1 and bidx == -1:
            raise ValueError(f"Could not find JSON object in content: {e_first}")
        # choose earliest opening bracket
        start = min([i for i in (sidx, bidx) if i != -1])

        # find corresponding closing bracket from end
        ridx = repaired.rfind("}")
        eidx = repaired.rfind("]")
        # choose latest
        end = max([i for i in (ridx, eidx) if i != -1])
        if start != -1 and end != -1 and end > start:
            repaired = repaired[start : end + 1]

        # remove trailing commas inside objects/arrays
        repaired = re.sub(r",\s*(\}|\])", r"\1", repaired)

        # if there are more single quotes than double quotes, replace single->double cautiously
        if repaired.count('"') < repaired.count("'"):
            # only replace when we detect typical JSON-like patterns
            # but avoid touching apostrophes inside words with letters on both sides
            repaired = re.sub(r"(?<!\w)'(?!\w)", '"', repaired)
            # fallback stronger replacement if still likely broken
            if repaired.count('"') < 2:
                repaired = repaired.replace("'", '"')

        # ensure balanced braces/brackets by appending missing closers
        opens = repaired.count("{") - repaired.count("}")
        if opens > 0:
            repaired = repaired + ("}" * opens)
        opens_b = repaired.count("[") - repaired.count("]")
        if opens_b > 0:
            repaired = repaired + ("]" * opens_b)

        # final parse attempt
        try:
            return json.loads(repaired)
        except Exception as e_second:
            logger.debug("safe_parse_json failed: first=%s, second=%s", e_first, e_second)
            raise ValueError(f"Could not parse JSON. first={e_first}; second={e_second}")


# ---------- Unit / numeric normalization helpers ----------
def normalize_unit(u: Optional[str]) -> Optional[str]:
    """
    Normalize common unit tokens. Preserve exactness but perform safe canonicalization.
    Examples normalized into forms safe to store in JSON output and consistent with Neo4j storage.
    """
    if not u:
        return None
    unit = str(u).strip()
    # unify fancy unicode
    unit = unit.replace("¹⁷³", "1.73")
    unit = unit.replace("μ", "u")
    unit = re.sub(r"\s+", " ", unit)
    unit = unit.strip()
    # common replacements
    unit = re.sub(r"(?i)per", "/", unit)
    unit = re.sub(r"(?i)mg/dl|mgdl", "mg/dL", unit)
    unit = re.sub(r"(?i)mmol/l|mmol", "mmol/L", unit)
    unit = re.sub(r"(?i)mmhg", "mmHg", unit)
    unit = re.sub(r"(?i)ml/min/1\.73\s*m2", "mL/min/1.73 m2", unit)
    unit = re.sub(r"\s*$", "", unit)
    return unit


def _extract_number_from_string(s: str) -> Optional[float]:
    if not s or not isinstance(s, str):
        return None
    m = re.search(r"([-+]?\d+(?:\.\d+)?)", s.replace(",", ""))
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


# ---------- Visit/time anchor helpers ----------
_VISIT_TO_DAYS = {
    "visit 1": -28,
    "visit 2": -14,
    "visit 3": 0,
    "visit 4": 14,
    "visit 5": 28
}


def _visit_to_days(token: str) -> Optional[int]:
    if not token:
        return None
    t = token.strip().lower()
    if t in _VISIT_TO_DAYS:
        return abs(_VISIT_TO_DAYS[t])
    m = re.search(r"week\s*-?(\d+)", t, re.I)
    if m:
        return int(m.group(1)) * 7
    m2 = re.search(r"visit\s*(\d+)", t, re.I)
    if m2:
        v = int(m2.group(1))
        return _VISIT_TO_DAYS.get(f"visit {v}", None)
    return None


def _extract_visit_window(text: str) -> Optional[str]:
    if not text:
        return None
    text_clean = text.replace("-", " ")
    range_match = re.search(r"Visit\s*(\d+)\D{0,30}Visit\s*(\d+)", text_clean, re.I)
    if range_match:
        v1, v2 = range_match.groups()
        return f"Visit_{v1}_to_Visit_{v2}"
    single_match = re.search(r"Visit\s*(\d+)", text_clean, re.I)
    if single_match:
        v = single_match.group(1)
        return f"Visit_{v}"
    return None


# ---------- heuristic numeric & change extractor ----------
def _extract_numeric_and_change(term_text: str, cat_obj: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[int], Optional[int], Optional[str]]:
    """
    Extract numeric value objects, 'change' objects, and temporal anchors (daysBefore/daysAfter/referenceEvent)
    from a single term phrase. Conservative: only set fields when patterns are clear.
    """
    if not term_text or not isinstance(term_text, str):
        return None, None, None, None, None

    txt = term_text.strip()
    value_obj = None
    change_obj = None
    days_before = None
    days_after = None
    reference_event = None

    # between patterns: 'between 30 and 90 mg/dL' or '30-90 mg/dL'
    m_between = re.search(r"(?:between|from)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:to|and|-)\s*([0-9]+(?:\.[0-9]+)?)\s*([^\s,;()]+)?", txt, re.I)
    if m_between:
        lo = float(m_between.group(1))
        hi = float(m_between.group(2))
        unit = m_between.group(3) or cat_obj.get("unit")
        if unit:
            unit = normalize_unit(unit)
        value_obj = {"operator": "between", "lower": lo, "upper": hi}
        if unit:
            value_obj["unit"] = unit
        reference_event = "sampling"

    # single comparison patterns: >=, <=, <, >, =, between forms with symbols
    if value_obj is None:
        m_num = re.search(r"(<=|>=|<|>|=|≤|≥)\s*([0-9]+(?:\.[0-9]+)?)\s*([^\s,;()]+)?", txt)
        if m_num:
            op = m_num.group(1)
            raw = float(m_num.group(2))
            unit = m_num.group(3) or cat_obj.get("unit")
            if unit:
                unit = normalize_unit(unit)
            op_map = {">=": ">=", "=>": ">=", "≤": "<=", "≥": ">=", "<=": "<=", ">": ">", "<": "<", "=": "="}
            opn = op_map.get(op, op)
            value_obj = {"operator": opn, "value": raw}
            if unit:
                value_obj["unit"] = unit
            reference_event = "sampling"

    # textual thresholds 'HbA1c 6.5% or higher' or 'A1c >= 6.5%'
    if value_obj is None:
        m_pct = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%(\s*or\s*higher|\s*or\s*more)?", txt, re.I)
        if m_pct:
            raw = float(m_pct.group(1))
            value_obj = {"operator": ">=", "value": raw, "unit": "%"}
            reference_event = "sampling"

    # change/delta detection 'increase by 2' / 'rise of 2'
    m_change = re.search(r"(increase|increased|rise|increasing|decrease|decreased|drop)\s*(?:by)?\s*([0-9]+(?:\.[0-9]+)?)\s*([^\s,;()]+)?", txt, re.I)
    if m_change:
        delta_word = m_change.group(1).lower()
        val = float(m_change.group(2))
        unit = m_change.group(3)
        op = ">=" if delta_word in ("increase", "increased", "rise", "increasing") else "<="
        change_obj = {"operator": op, "value": val}
        if unit:
            change_obj["unit"] = normalize_unit(unit)
        reference_event = reference_event or "sampling"

    # visit/time anchor detection
    m_visit = re.search(r"(visit\s*\d+|week\s*-?\d+|month[s]?\s*\d+|last\s*\d+\s*months?)", txt, re.I)
    if m_visit:
        vd = m_visit.group(1)
        days = _visit_to_days(vd)
        if days:
            # Heuristic: if pattern says 'within' or 'in last', treat as daysBefore
            if re.search(r"within|last|previous|prior|in the past", txt, re.I):
                days_before = days
            else:
                # ambiguous: set daysAfter for forward-looking windows
                days_after = days
            reference_event = reference_event or "sampling"

    # default referenceEvent for labs/observations
    cat_name_guess = cat_obj.get("categoryName") if isinstance(cat_obj, dict) else None
    if not reference_event and cat_name_guess in ("lab", "observation"):
        reference_event = "sampling"

    return value_obj, change_obj, days_before, days_after, reference_event


# ---------- Antonym filter helper ----------
def _filter_antonym_codes(term_text: str, codes_by_system: Dict[str, list]) -> Dict[str, list]:
    """
    Conservative antonym removal: if term contains 'hyper' and a code display contains 'hypo', drop it, etc.
    Keeps majority of codes; only remove clear antonyms when they conflict.
    """
    if not term_text or not codes_by_system:
        return codes_by_system
    t = term_text.lower()
    out = {}
    for sys, entries in (codes_by_system or {}).items():
        filtered = []
        for e in entries:
            if not isinstance(e, dict):
                filtered.append(e)
                continue
            disp = (e.get("display") or "").lower()
            skip = False
            if "hyper" in t and "hypo" in disp:
                skip = True
            if "hypo" in t and "hyper" in disp:
                skip = True
            if "positive" in t and "negative" in disp:
                skip = True
            if "negative" in t and "positive" in disp:
                skip = True
            if not skip:
                filtered.append(e)
        out[sys] = filtered
    return out


# ---------- dedupe codes_by_system ----------
def dedupe_codes_by_system(codes_by_system: Dict[str, list], max_per_system: int = MAX_CODES_PER_SYSTEM) -> Dict[str, list]:
    out = {}
    for system, entries in (codes_by_system or {}).items():
        seen = set()
        new_entries = []
        if not isinstance(entries, (list, tuple)):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            code = str(e.get("code", "")).strip()
            if not code:
                continue
            key = (system, code)
            if key in seen:
                # prefer the one with more descriptive display if candidate has display and previous did not
                continue
            seen.add(key)
            new_entries.append(e)
            if len(new_entries) >= max_per_system:
                break
        if new_entries:
            out[system] = new_entries
    return out


# ---------- Stage-1: Categorization Prompt (refined & preserved rules) ----------
CATEGORIZATION_PROMPT_TEMPLATE = r"""
You are an expert in clinical trial eligibility criteria and HL7 FHIR R4 modelling.

TASK: For a SINGLE input eligibility criterion, extract only the categorization structure (no codes).
Follow strict rules (see below). Output ONLY valid JSON: no commentary.

OUTPUT FORMAT:
{
  "id": <integer or null>,
  "description": "<original criterion text>",
  "criterion_logic": "<AND|OR>",
  "categories": {
    "<category_name>": {
      "logic": "<AND|OR>",
      "terms": ["<verbatim phrases from the criterion>"],
      "daysBefore": <int|null>,
      "daysAfter": <int|null>,
      "referenceEvent": "<sampling|procedure|diagnosis|medication|observation|now|null>",
      "value": <object|null>,
      "statusField": "<clinicalStatus|status|verificationStatus|null>",
      "statusValues": [ "<string>", ... ] | null,
      "negation": <true|false|null>
    }
  }
}

ALLOWED CATEGORIES: demographics, condition, medication, lab, allergy
- Map ONLY to these categories. Do NOT create new categories.

KEY RULES (must be followed):
- TERMS: verbatim phrase snippets that belong to that category. Include comparisons and units within the term.
- DEMOGRAPHICS: only static attributes (age, gender, birthDate, ethnicity, race, language, marital). Pregnancy is NOT demographics.
- LAB/OBSERVATION: includes labs, vitals, tests, qualitative test results, and social/behavioral observations (pregnancy test, HIV positive, homelessness, smoking). Use "lab" category for these.
- CONDITION: diagnoses, procedures; detect status words (history/past/resolved vs active).
- MEDICATION: exposures, prescriptions; detect status words (taking/stopped).
- ALLERGY: allergic reactions and intolerances.

TIMEFRAMES:
- Convert months -> days (1 month = 30 days), years -> days (1 year = 365 days).
- If timeframe anchored to an event, set referenceEvent accordingly.

NEGATION:
- Detect "no history of", "without", "never", "not on", set negation=true.

VALUE RULES:
- Extract numeric comparisons when present but keep them also inside the term (terms are verbatim).
- When possible, set category-level daysBefore/daysAfter to null if multiple terms; term-level handling will provide specifics later.

LOGIC DEFAULTS:
- inclusion default logic -> "AND"
- exclusion default logic -> "OR"
- demographics.logic = "AND"
- condition/medication/lab/allergy.logic = "OR"

Return strict JSON only.
USER_CRITERION: __USER_INPUT__
"""


def validate_classification(obj: Dict[str, Any]) -> bool:
    if not isinstance(obj, dict):
        return False
    if "description" not in obj or "criterion_logic" not in obj or "categories" not in obj:
        return False
    if not isinstance(obj.get("categories"), dict):
        return False
    return True


def _parse_line_fallback_to_classification(text: str, idx: int = None) -> Dict[str, Any]:
    """
    Deterministic fallback when LLM fails classification.
    Creates a single 'text_based' category with verbatim term = full input.
    """
    out = {
        "id": idx if idx is not None else None,
        "description": text,
        "criterion_logic": "AND",
        "categories": {
            "text_based": {
                "logic": "AND",
                "terms": [text],
                "daysBefore": None,
                "daysAfter": None,
                "referenceEvent": None,
                "value": None,
                "statusField": None,
                "statusValues": None,
                "negation": False
            }
        }
    }
    return out


def classify_criterion_with_logic(criterion_text: str, idx: int = None) -> Dict[str, Any]:
    """
    Stage 1: try LLM classification; fallback to heuristic deterministic output when LLM fails.
    Also attaches per-term initial term_details for further expansion.
    """
    client = _get_openai_client()
    prompt = CATEGORIZATION_PROMPT_TEMPLATE.replace("__USER_INPUT__", criterion_text)
    if "<integer|null>" in prompt:
        prompt = prompt.replace("<integer|null>", str(idx if idx is not None else "null"))

    last_err = None
    for attempt in range(max(1, CLASSIFY_RETRIES + 1)):
        try:
            resp = client.chat.completions.create(
                model=CLASSIFY_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=CLASSIFY_MAX_TOKENS
            )
            content = resp.choices[0].message.content.strip()
            parsed = safe_parse_json(content)

            if validate_classification(parsed):
                # Initialize term_details per term for expansion stage
                for cat_name, cat_obj in parsed.get("categories", {}).items():
                    terms = cat_obj.get("terms", []) or []
                    term_details = {}
                    for term in terms:
                        term_text = str(term).strip()
                        # basic heuristics for daysBefore/daysAfter from common phrases
                        days_before = None
                        days_after = None
                        if re.search(r"within\s+(\d+)\s+months?", term_text, re.I):
                            m = re.search(r"within\s+(\d+)\s+months?", term_text, re.I)
                            days_before = int(m.group(1)) * 30
                        if re.search(r"within\s+(\d+)\s+days?", term_text, re.I):
                            m = re.search(r"within\s+(\d+)\s+days?", term_text, re.I)
                            days_before = int(m.group(1))
                        if re.search(r"after\s+(\d+)\s+days?", term_text, re.I):
                            m = re.search(r"after\s+(\d+)\s+days?", term_text, re.I)
                            days_after = int(m.group(1))

                        # use the heuristic extractor to set possible value/change/ref_event
                        try:
                            ext_val, ext_change, ext_dbefore, ext_dafter, ext_ref = _extract_numeric_and_change(term_text, {"categoryName": cat_name})
                            if ext_dbefore:
                                days_before = ext_dbefore
                            if ext_dafter:
                                days_after = ext_dafter
                            ref_evt = ext_ref or parsed.get("categories", {}).get(cat_name, {}).get("referenceEvent") or None
                        except Exception:
                            ext_val, ext_change, ref_evt = None, None, None

                    term_details[term_text] = {
                        "daysBefore": days_before,
                        "daysAfter": days_after,
                        "referenceEvent": (
                            ref_evt if (ref_evt is not None)
                            else parsed.get("categories", {}).get(cat_name, {}).get("referenceEvent")
                        ),
                        "value": None,       # expansion will fill this
                        "change": None,      # must NOT be nested inside value
                        "occurrence": None,
                        "statusField": parsed.get("categories", {}).get(cat_name, {}).get("statusField"),
                        "statusValues": parsed.get("categories", {}).get(cat_name, {}).get("statusValues"),
                        "negation": bool(parsed.get("categories", {}).get(cat_name, {}).get("negation", False)),
                        "codes_by_system": {}   # required for expansion stage
                    }
                    # attach
                    parsed["categories"][cat_name]["term_details"] = term_details
                return parsed
            else:
                last_err = ValueError("Validation failed for classification output")
                logger.warning("Classification validation failed on attempt %d", attempt + 1)
        except Exception as e:
            logger.warning("Classification attempt %d failed: %s", attempt + 1, e)
            last_err = e
            time.sleep(0.2 * (attempt + 1))

    # fallback deterministic
    logger.error("Classification LLM failed after retries; using fallback. last_err=%s", last_err)
    fallback = _parse_line_fallback_to_classification(criterion_text, idx)
    # attach term_details if not present
    for cname, cobj in fallback["categories"].items():
        t = cobj.get("terms", [criterion_text])[0]
        fallback["categories"][cname]["term_details"] = {
            t: {
                "daysBefore": None,
                "daysAfter": None,
                "referenceEvent": None,
                "value": None,
                "change": None,
                "statusField": None,
                "statusValues": None,
                "negation": False,
                "codes_by_system": {}
            }
        }
    return fallback

MAIN_CATEGORY_PROMPT_TEMPLATE = r"""
You are a clinical terminology and HL7 FHIR R4 expert.

TASK:
Expand a SINGLE category (condition, medication, lab, or allergy, or demographics)
into detailed query-ready JSON for use in a Neo4j FHIR-like graph.
Each category is processed independently for each term.
Output MUST be strictly valid JSON only.

============================================================
 REQUIRED OUTPUT FORMAT
============================================================

{
  "<category_name>": {
    "logic": "<AND|OR>",
    "terms": ["<verbatim terms from Stage-1>"],

    "codes_by_system": {
      "<system>": [
        { "code": "<string>", "display": "<string>" }
      ]
    },

    "daysBefore": <int|null>,
    "daysAfter": <int|null>,
    "referenceEvent": "<sampling|procedure|diagnosis|medication|observation|now|null>",
    "statusField": "<clinicalStatus|status|verificationStatus|null>",
    "statusValues": ["<string>", ...] | null,
    "negation": <true|false|null>,
    "value": <object|null>,

    "term_details": {
      "<verbatim_term>": {
        "daysBefore": <int|null>,
        "daysAfter": <int|null>,
        "referenceEvent": "<sampling|procedure|diagnosis|medication|observation|now|null>",
        "value": <object|array|null>,
        "occurrence": <object|null>,
        "change": <object|null>,
        "statusField": "<clinicalStatus|status|verificationStatus|null>",
        "statusValues": ["<string>", ...] | null,
        "negation": <true|false|null>,
        "visit_window": "<Visit_1_to_Visit_5|null>",
        "codes_by_system": {
          "<system>": [
            { "code": "<string>", "display": "<string>" }
          ]
        }
      }
    }
  }
}

============================================================
 CATEGORY RULES (STRICT, MEDIUM-COVERAGE)
============================================================

DEMOGRAPHICS:
- NO codes_by_system (must be {}).
- Keep only demographic fields: age, gender, race, ethnicity, language, marital status, birthDate.

LAB (includes measurable labs + social/behavioral observations):
- Use LOINC for measurable tests (HbA1c, creatinine, cholesterol, BP, BMI, etc.).
- Include medium coverage: 20–40 relevant LOINC codes.
- STRICT FILTER:
  * No deprecated LOINC codes.
  * No ambiguous high-level categories.
  * Include panels + important variants (serum/plasma/whole blood).
- FOR SOCIAL OBSERVATIONS (pregnancy status, HIV-positive, homelessness, smoking, alcohol):
  → Codes are allowed here (LOINC/SNOMED etc.) and should be provided when available.

CONDITION:
- Use SNOMED CT + ICD-10-CM.
- STRICT FILTER:
  * Exclude deprecated SNOMED codes.
  * Exclude vague parents (e.g., “Disease (disorder)”).
  * Include medium coverage: 30–50 high-yield child conditions, common synonyms, clinically relevant variants.
- ICD-10-CM:
  * Include useful subcodes (E11.0, E11.9, etc.).
  * Exclude irrelevant administrative codes.

MEDICATION:
- Use RxNorm.
- STRICT FILTER:
  * Include ingredients + clinical drug forms + branded drug variants.
  * Exclude deprecated or obscure preparations.
  * Medium coverage: 20–40 RxNorm entries.
- Maintain negation (not taking → negation:true).

ALLERGY:
- Use SNOMED CT primarily; ICD-10-CM only if meaningful.
- STRICT FILTER:
  * No vague or deprecated allergy codes.
  * Keep clinically relevant reaction and intolerance concepts only.
- Negation: 
  * “no known drug allergies” → negation:true and codes_by_system:{}.

============================================================
 VALUE / OCCURRENCE RULES
============================================================

You MUST extract:
- Numeric comparisons: >=, <=, <, >, = with units.
- Ranges: “between A and B”.
- Qualitative values: “positive”, “negative”, “abnormal”.
- Occurrence counts: “2 or more episodes”.
- Change values: “increase of 2 mg”.
- Units must match exactly the text (“mL/min/1.73 m2”, etc.)

============================================================
 CODING SYSTEMS ALLOWED ONLY
============================================================

- SNOMED CT: "http://snomed.info/sct"
- ICD-10-CM: "http://hl7.org/fhir/sid/icd-10-cm"
- RxNorm: "http://www.nlm.nih.gov/research/umls/rxnorm"
- LOINC: "http://loinc.org"

Do NOT output any other systems.

============================================================
 STRICT FILTERING RULES
============================================================

- Remove ALL deprecated codes.
- Remove ALL duplicates (system + code must be unique).
- Remove vague, ambiguous, or overly broad parents.
- Include ONLY clinically meaningful child/subtype codes.
- Keep medium coverage: **20–50 codes maximum** per category.
- Never hallucinate unrelated diseases or tests.

============================================================
 OUTPUT RULES
============================================================

- Return strictly valid JSON.
- No markdown, comments, explanations, or text outside JSON.
- Preserve verbatim terms from Stage-1.
- Never create new categories.
- term_details MUST include codes_by_system for every term.

STRICT OUTPUT ENFORCEMENT:
- The "value" field MUST be an object when present. Use:
  { "operator": "< >= <= > < =", "value": number|string|null, "unit": string|null }
  For ranges:
  { "operator": "between", "lower": num, "upper": num, "unit": string|null }
- NEVER output "comparison"; ALWAYS "operator".
- NEVER put "change" inside "value"; use:
  "change": { "operator": "...", "value": num }
- codes_by_system MUST use one of:
  - http://loinc.org
  - http://snomed.info/sct
  - http://hl7.org/fhir/sid/icd-10-cm
  - http://www.nlm.nih.gov/research/umls/rxnorm
- Each code entry MUST be:
  { "code": "<string>", "display": "<string>" }
- term_details MUST include:
  "codes_by_system", "value", "change", "negation",
  "daysBefore", "daysAfter", "referenceEvent"
- Ranges MUST use lower/upper — NEVER arrays.
- Output MUST be STRICT JSON only.

============================================================
 INPUT CONTEXT
============================================================

Criterion text: __USER_INPUT__
Category: __CATEGORY_NAME__
Terms: __CATEGORY_TERMS__
Logic: __CATEGORY_LOGIC__
ReferenceEvent: __REFERENCE_EVENT__
DaysBefore: __DAYS_BEFORE__
DaysAfter: __DAYS_AFTER__

============================================================
 Produce the JSON now.
============================================================

Output only valid JSON.
"""


# ---------- small fallback parser for raw 'system|code|display' lines ----------
def _parse_line_fallback_to_codes(content: str) -> Dict[str, list]:
    """
    If LLM returns raw lines like "system|code|display", parse them into codes_by_system dict.
    """
    out = {}
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" not in line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sys_uri, code, display = parts[0].strip(), parts[1].strip(), parts[2].strip()
        out.setdefault(sys_uri, []).append({"code": code, "display": display})
    return out


# ---------- internal expansion cache ----------
_expansion_cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
# key: (category_name, term_text, normalized_criterion_hash) -> expansion dict


def expand_category_codes(category_name: str, category_data: Dict[str, Any], criterion_text: str) -> Dict[str, Any]:
    """
    Expand codes for a single category and attach per-term codes into category_data["term_details"].
    Returns {category_name: merged_block}
    """
    client = _get_openai_client()
    terms = category_data.get("terms", []) or []
    term_details = category_data.get("term_details", {}) or {}

    # compute a cheap normalized hash for caching (criterion text length + category + terms)
    normalized_key = f"{len(criterion_text)}::{category_name}::" + "|".join(sorted([str(t) for t in terms]))
    result_block: Dict[str, Any] = {**category_data}  # will be mutated and returned

    expanded_term_details: Dict[str, Any] = {}

    for term in terms:
        term_text = str(term).strip()
        cache_key = (category_name, term_text, normalized_key)
        if cache_key in _expansion_cache:
            # copy to avoid accidental mutation
            cached = _expansion_cache[cache_key].copy()
            # make sure term-level codes_by_system exists and is deduped
            cb = cached.get("codes_by_system", {}) or {}
            cached["codes_by_system"] = dedupe_codes_by_system(_filter_antonym_codes(term_text, cb), MAX_CODES_PER_SYSTEM)
            expanded_term_details[term_text] = cached
            continue

        # prepare prompt
        prompt = MAIN_CATEGORY_PROMPT_TEMPLATE \
            .replace("__USER_INPUT__", criterion_text) \
            .replace("__CATEGORY_NAME__", category_name) \
            .replace("__CATEGORY_TERMS__", json.dumps([term_text])) \
            .replace("__CATEGORY_LOGIC__", category_data.get("logic", "OR")) \
            .replace("__REFERENCE_EVENT__", str(term_details.get(term_text, {}).get("referenceEvent", category_data.get("referenceEvent", "now")) or "null")) \
            .replace("__DAYS_BEFORE__", str(term_details.get(term_text, {}).get("daysBefore", category_data.get("daysBefore", "null")) or "null")) \
            .replace("__DAYS_AFTER__", str(term_details.get(term_text, {}).get("daysAfter", category_data.get("daysAfter", "null")) or "null"))

        last_err = None
        parsed_block: Dict[str, Any] = {}
        for attempt in range(max(1, CATEGORY_EXPAND_RETRIES + 1)):
            try:
                resp = client.chat.completions.create(
                    model=EXPAND_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=MAX_TOKENS_CATEGORY
                )
                content = resp.choices[0].message.content.strip()

                # try JSON parse
                try:
                    parsed = safe_parse_json(content)
                except Exception as e:
                    # fallback parse line-style
                    parsed = {"codes_by_system": _parse_line_fallback_to_codes(content)}

                # Accept either {category: {...}} or direct block
                block = parsed.get(category_name) if (isinstance(parsed, dict) and category_name in parsed) else parsed
                if not isinstance(block, dict):
                    raise ValueError("Expansion returned invalid block")

                # Validate that expansion contains either codes_by_system or term_details with codes
                has_codes = bool(block.get("codes_by_system")) or bool(block.get("term_details"))
                # Also accept blocks which contain value/days/temporal info even without codes
                if not has_codes and not any(k in block for k in ("daysBefore", "daysAfter", "value", "statusField", "negation")):
                    raise ValueError("Expansion validation failed: no codes or temporal/value fields")

                # Filter antonymic codes and dedupe at category block level
                raw_codes = block.get("codes_by_system", {}) or {}
                raw_codes = _filter_antonym_codes(term_text, raw_codes)
                deduped = dedupe_codes_by_system(raw_codes, MAX_CODES_PER_SYSTEM)
                block["codes_by_system"] = deduped

                # If LLM gave per-term term_details, prefer it (and sanitize)
                llm_term_details = block.get("term_details", {}) or {}
                if isinstance(llm_term_details, dict) and term_text in llm_term_details:
                    tdet = llm_term_details[term_text]
                    # sanitize codes_by_system inside
                    t_codes = tdet.get("codes_by_system", {}) or {}
                    t_codes = _filter_antonym_codes(term_text, t_codes)
                    t_codes = dedupe_codes_by_system(t_codes, MAX_CODES_PER_SYSTEM)
                    tdet["codes_by_system"] = t_codes
                    parsed_block = tdet
                else:
                    # create a term-level detail from category-level expansion
                    parsed_block = {
                        "daysBefore": block.get("daysBefore", term_details.get(term_text, {}).get("daysBefore")),
                        "daysAfter": block.get("daysAfter", term_details.get(term_text, {}).get("daysAfter")),
                        "referenceEvent": block.get("referenceEvent", term_details.get(term_text, {}).get("referenceEvent")),
                        "value": block.get("value", term_details.get(term_text, {}).get("value")),
                        "occurrence": term_details.get(term_text, {}).get("occurrence"),
                        "codes_by_system": block.get("codes_by_system", {}),
                        "statusField": block.get("statusField", term_details.get(term_text, {}).get("statusField")),
                        "statusValues": block.get("statusValues", term_details.get(term_text, {}).get("statusValues")),
                        "negation": bool(block.get("negation", term_details.get(term_text, {}).get("negation", False)))
                    }

                # Merge category-level fields into parsed_block where missing
                if parsed_block.get("daysBefore") is None and category_data.get("daysBefore") is not None:
                    parsed_block["daysBefore"] = category_data.get("daysBefore")
                if parsed_block.get("daysAfter") is None and category_data.get("daysAfter") is not None:
                    parsed_block["daysAfter"] = category_data.get("daysAfter")
                if parsed_block.get("referenceEvent") in (None, "", "observation") and category_data.get("referenceEvent"):
                    parsed_block["referenceEvent"] = category_data.get("referenceEvent")
                if parsed_block.get("statusField") is None and category_data.get("statusField") is not None:
                    parsed_block["statusField"] = category_data.get("statusField")
                if parsed_block.get("statusValues") in (None, []) and category_data.get("statusValues") is not None:
                    parsed_block["statusValues"] = category_data.get("statusValues")
                if "negation" not in parsed_block:
                    parsed_block["negation"] = bool(category_data.get("negation", False))

                # ensure codes_by_system present and deduped in parsed_block
                p_codes = parsed_block.get("codes_by_system") or {}
                p_codes = _filter_antonym_codes(term_text, p_codes)
                parsed_block["codes_by_system"] = dedupe_codes_by_system(p_codes, MAX_CODES_PER_SYSTEM)

                # compute expanded_terms_count
                parsed_block["expanded_terms_count"] = sum(len(v) for v in parsed_block.get("codes_by_system", {}).values()) if isinstance(parsed_block.get("codes_by_system", {}), dict) else 0

                # store in cache and prepared term details
                _expansion_cache[cache_key] = parsed_block.copy()
                expanded_term_details[term_text] = parsed_block.copy()
                break  # successful expansion

            except Exception as e:
                last_err = e
                logger.warning("expand_category_codes attempt %d failed for %s - term '%s': %s", attempt + 1, category_name, term_text, e)
                time.sleep(0.2 * (attempt + 1))
                continue
        else:
            # retries exhausted
            logger.error("Expansion failed for %s term '%s' after retries. error=%s", category_name, term_text, last_err)
            # attach an empty detail with error
            expanded_term_details[term_text] = {
                **(term_details.get(term_text, {})),
                "codes_by_system": {},
                "error": str(last_err) if last_err else "unknown"
            }

    # attach expanded term_details back to result block
    # ensure each term_detail has a codes_by_system and it's deduped
    combined_term_details: Dict[str, Any] = {}
    for tname, tdet in {**(term_details or {}), **expanded_term_details}.items():
        tdet = dict(tdet or {})
        if "codes_by_system" not in tdet or tdet.get("codes_by_system") is None:
            tdet["codes_by_system"] = {}
        else:
            # sanitize and dedupe
            tdet["codes_by_system"] = dedupe_codes_by_system(_filter_antonym_codes(tname, tdet.get("codes_by_system", {})), MAX_CODES_PER_SYSTEM)
        # ensure canonical keys
        tdet.setdefault("daysBefore", None)
        tdet.setdefault("daysAfter", None)
        tdet.setdefault("referenceEvent", None)
        tdet.setdefault("value", None)
        tdet.setdefault("occurrence", None)
        tdet.setdefault("change", None)
        tdet.setdefault("statusField", None)
        tdet.setdefault("statusValues", None)
        tdet.setdefault("negation", False)
        combined_term_details[tname] = tdet

    result_block["term_details"] = combined_term_details

    # Build merged category-level codes_by_system from term-level codes
    merged_codes = {}
    for tdet in result_block["term_details"].values():
        for sys, entries in (tdet.get("codes_by_system") or {}).items():
            merged_codes.setdefault(sys, []).extend(entries if isinstance(entries, list) else [entries])

    merged_codes = dedupe_codes_by_system(merged_codes, MAX_CODES_PER_SYSTEM)
    #result_block["codes_by_system"] = merged_codes

    # ensure logic, terms, negation exist
    result_block.setdefault("logic", category_data.get("logic", "OR"))
    result_block.setdefault("terms", category_data.get("terms", []))
    result_block.setdefault("negation", bool(category_data.get("negation", False)))

    logger.debug("Completed expansion for category %s: %d terms", category_name, len(result_block.get("term_details", {})))
    return {category_name: result_block}

def _normalize_term_values(term_det: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize certain fields in a term_detail: ensure value is None if empty dict,
    negation is boolean, referenceEvent is either canonical string or None.
    """
    td = dict(term_det or {})
    if isinstance(td.get("value"), dict) and not td.get("value"):
        td["value"] = None
    if "negation" in td:
        td["negation"] = bool(td.get("negation"))
    else:
        td["negation"] = False
    ref = td.get("referenceEvent")
    if ref is not None and isinstance(ref, str) and not ref.strip():
        td["referenceEvent"] = None
    return td


def generate_json(criterion_text: Union[str, dict], idx: int = 0) -> Dict[str, Any]:
    """
    Orchestrator: given a single criterion (string or dict), produce the structured expanded JSON.
    Output schema is preserved:
    {
      "id": <int>,
      "description": "...",
      "logic": "<AND|OR>",
      "categories": { ... } 
    }
    """
    # Normalize input
    if isinstance(criterion_text, dict):
        # prefer description or text fields if user passed in rich object
        criterion_text = criterion_text.get("description") or criterion_text.get("text") or json.dumps(criterion_text)
    elif not isinstance(criterion_text, str):
        criterion_text = str(criterion_text)

    # Stage 1: classification
    structure = classify_criterion_with_logic(criterion_text, idx)
    if not validate_classification(structure):
        logger.error("Classification invalid for criterion #%s: %s", idx, (criterion_text[:200] if criterion_text else ""))
        return {
            "id": idx,
            "description": criterion_text,
            "logic": "AND",
            "categories": {},
            "error": "classification_failed"
        }

    categories = structure.get("categories", {}) or {}
    overall_logic = structure.get("criterion_logic") or "AND"

    expanded_categories: Dict[str, Any] = {}

    # Stage 2: expand each category
    for cat_name, cat_data in categories.items():
        try:
            expanded = expand_category_codes(cat_name, cat_data, criterion_text)
            candidate = expanded.get(cat_name, expanded)
            if not isinstance(candidate, dict):
                candidate = {"error": "invalid_expansion_block"}

            # Merge pre-classifier cat_data and expansion candidate
            # We will recompute category-level codes_by_system below to avoid duplication
            merged = {**cat_data, **candidate}
            merged.setdefault("logic", cat_data.get("logic", "OR"))
            merged.setdefault("terms", cat_data.get("terms", []))
            merged.setdefault("referenceEvent", cat_data.get("referenceEvent"))
            merged.setdefault("negation", bool(cat_data.get("negation", False)))

            # Deep merge of term_details: start from classifier details and overlay expansions
            term_details_cat = cat_data.get("term_details", {}) or {}
            term_details_exp = candidate.get("term_details", {}) or {}
            combined_term_details: Dict[str, Any] = {}

            # ensure we include all terms from classifier
            for term, det in term_details_cat.items():
                det_expanded = term_details_exp.get(term, {})
                # Merge carefully, making sure codes_by_system is deduped and present
                merged_det = {**det, **det_expanded}
                # prefer expanded codes if present, else classifier codes
                combined_codes = det_expanded.get("codes_by_system", det.get("codes_by_system", {})) or {}
                combined_codes = _filter_antonym_codes(term, combined_codes)
                merged_det["codes_by_system"] = dedupe_codes_by_system(combined_codes, MAX_CODES_PER_SYSTEM)
                merged_det = _normalize_term_values(merged_det)
                combined_term_details[term] = merged_det

            # also include new terms introduced during expansion
            for term, det in term_details_exp.items():
                if term not in combined_term_details:
                    detn = dict(det or {})
                    # sanitize codes_by_system
                    detn["codes_by_system"] = dedupe_codes_by_system(_filter_antonym_codes(term, detn.get("codes_by_system", {})), MAX_CODES_PER_SYSTEM)
                    detn = _normalize_term_values(detn)
                    combined_term_details[term] = detn

            merged["term_details"] = combined_term_details

            # Category-level codes_by_system should ALWAYS be recomputed
            # from term_details (to avoid duplicates and inconsistencies)
            cat_level_codes = {}

            for tdet in merged["term_details"].values():
                for sys, entries in (tdet.get("codes_by_system") or {}).items():
                    cat_level_codes.setdefault(sys, []).extend(entries)

            #merged["codes_by_system"] = dedupe_codes_by_system(cat_level_codes, MAX_CODES_PER_SYSTEM)

            # ensure daysBefore/daysAfter values preserved (prefer candidate then classifier)
            if "daysBefore" not in merged:
                merged["daysBefore"] = cat_data.get("daysBefore")
            if "daysAfter" not in merged:
                merged["daysAfter"] = cat_data.get("daysAfter")

            # final coercions
            merged["negation"] = bool(merged.get("negation", False))
            # ensure term_details have canonical keys
            for tname, tdet in merged["term_details"].items():
                merged["term_details"][tname] = _normalize_term_values(tdet)

            # --- UNIVERSAL VALUE + UNIT NORMALIZER ---
            # Ensure every term_detail.value uses canonical form:
            # { "operator": <str>, "value": <number|null>, "unit": <str|null> }
            for tname, tdet in merged["term_details"].items():
                v = tdet.get("value")

                # Case A — value is a verbatim string like "< 30 kg/m2" or "30 kg/m2"
                if isinstance(v, str):
                    op = None
                    if ">=" in v:
                        op = ">="
                    elif "<=" in v:
                        op = "<="
                    elif ">" in v:
                        # ensure we don't mis-detect '>=' as '>'
                        if ">=" not in v:
                            op = ">"
                        else:
                            op = ">="
                    elif "<" in v:
                        if "<=" not in v:
                            op = "<"
                        else:
                            op = "<="
                    else:
                        op = "="

                    num = _extract_number_from_string(v)
                    m = re.search(r"([-+]?\d+(?:\.\d+)?)(?:\s*([A-Za-z/%\.\-μμ²³]+))?", v)
                    unit = None
                    if m and m.group(2):
                        unit = normalize_unit(m.group(2))
                    tdet["value"] = {"operator": op, "value": (float(num) if num is not None else None), "unit": unit}

                # Case B — value is a dict but its 'value' is a string that contains unit ("27 kg/m2")
                elif isinstance(v, dict):
                    raw = v.get("value")
                    # If raw is string, extract number and unit
                    if isinstance(raw, str):
                        num = _extract_number_from_string(raw)
                        m = re.search(r"([-+]?\d+(?:\.\d+)?)(?:\s*([A-Za-z/%\.\-μμ²³]+))?", raw)
                        unit = None
                        if m and m.group(2):
                            unit = normalize_unit(m.group(2))
                        v["value"] = (float(num) if num is not None else None)
                        v["unit"] = unit if unit is not None else v.get("unit")
                    else:
                        # Ensure numeric coercion if possible
                        if isinstance(raw, (int, float)):
                            v["value"] = float(raw)
                        elif raw is None:
                            v["value"] = None
                        else:
                            num = _extract_number_from_string(str(raw))
                            v["value"] = (float(num) if num is not None else None)
                        # Ensure unit key exists
                        if "unit" not in v:
                            v["unit"] = None

                    # put back normalized dict
                    tdet["value"] = v

                # Otherwise leave as-is (None or other types) but coerce to canonical dict if possible
                if isinstance(tdet.get("value"), dict):
                    if "operator" not in tdet["value"]:
                        tdet["value"].setdefault("operator", "=")
                    if "unit" not in tdet["value"]:
                        tdet["value"].setdefault("unit", None)

                merged["term_details"][tname] = tdet

            expanded_categories[cat_name] = merged

        except Exception as e:
            logger.exception("Failed expanding category %s: %s", cat_name, e)
            # keep original classifier block but mark error
            expanded_categories[cat_name] = {**cat_data, "error": str(e)}

    final_output = {
        "id": idx,
        "description": criterion_text,
        "logic": overall_logic,
        "categories": expanded_categories
    }

    logger.info("Generated structured JSON for criterion id=%s with %d categories", idx, len(expanded_categories))
    # ----------------------------
    # Canonical runner-compatible finalizer
    # ----------------------------
    def _canonicalize_runner(out_obj: Dict[str, Any]):
        SYS_MAP = {
            "LOINC": "http://loinc.org", "loinc": "http://loinc.org",
            "SNOMED": "http://snomed.info/sct", "snomed": "http://snomed.info/sct",
            "ICD10": "http://hl7.org/fhir/sid/icd-10-cm",
            "ICD-10-CM": "http://hl7.org/fhir/sid/icd-10-cm",
            "RXNORM": "http://www.nlm.nih.gov/research/umls/rxnorm",
            "RxNorm": "http://www.nlm.nih.gov/research/umls/rxnorm",
        }

        def canon_sys(s):
            if not s:
                return s
            s2 = str(s).strip()
            return SYS_MAP.get(s2) or SYS_MAP.get(s2.upper()) or s2

        def fix_value(val):
            if val is None:
                return None
            if isinstance(val, str):
                return {"operator": "=", "value": val, "unit": None}
            if isinstance(val, dict):
                if "comparison" in val and "operator" not in val:
                    val["operator"] = val.pop("comparison")
                val.setdefault("operator", "=")
                val.setdefault("unit", None)
                return val
            return val

        out = dict(out_obj)
        categories = out.get("categories") or {}

        for cname, cobj in categories.items():
            if not isinstance(cobj, dict):
                continue

            # ---------------- category-level codes ----------------
            cb = cobj.get("codes_by_system") or {}
            new_cb = {}
            for sys, entries in cb.items():
                sysc = canon_sys(sys)
                fixed_entries = []
                for e in entries or []:
                    if isinstance(e, dict) and e.get("code"):
                        fixed_entries.append({
                            "code": str(e["code"]),
                            "display": str(e.get("display") or "")
                        })
                if fixed_entries:
                    new_cb[sysc] = fixed_entries

            # enforce 2 systems (lab/condition only)
            # All runner branches (lab, observation, diagnosticreport, condition)
            # ALWAYS require system_0/system_1 and codes_0/codes_1
            if cname in ("lab", "observation", "diagnosticreport", "condition") and len(new_cb.keys()) == 1:
                new_cb.setdefault("", [])

            cobj["codes_by_system"] = new_cb

            # ---------------- category-level value ----------------
            if "value" in cobj:
                cobj["value"] = fix_value(cobj.get("value"))

            # ---------------- term_details ----------------
            td = cobj.get("term_details") or {}
            for term, tdet in td.items():
                if not isinstance(tdet, dict):
                    continue

                tdet.setdefault("daysBefore", None)
                tdet.setdefault("daysAfter", None)
                tdet.setdefault("referenceEvent", None)
                tdet.setdefault("negation", bool(tdet.get("negation", False)))
                tdet.setdefault("occurrence", None)

                tdet["value"] = fix_value(tdet.get("value"))

                if isinstance(tdet.get("change"), dict):
                    ch = tdet["change"]
                    if "comparison" in ch and "operator" not in ch:
                        ch["operator"] = ch.pop("comparison")
                    ch.setdefault("value", None)
                    tdet["change"] = ch

                tc = tdet.get("codes_by_system") or {}
                new_tc = {}
                for sys, entries in tc.items():
                    sysc = canon_sys(sys)
                    fixed_entries = []
                    for e in entries or []:
                        if isinstance(e, dict) and e.get("code"):
                            fixed_entries.append({
                                "code": str(e["code"]),
                                "display": str(e.get("display") or "")
                            })
                    if fixed_entries:
                        new_tc[sysc] = fixed_entries

                    #if cname in ("lab", "observation", "diagnosticreport", "condition") and len(new_tc.keys()) == 1:
                        #new_tc.setdefault("", [])

                tdet["codes_by_system"] = new_tc
                td[term] = tdet

            cobj["term_details"] = td
            categories[cname] = cobj

        out["categories"] = categories
        return out

    return _canonicalize_runner(final_output)


def wrap_for_cypher(criterion_json: Dict[str, Any], limit: int = 1000) -> Dict[str, Any]:
    """
    Keep existing wrapper for cypher generator compatibility.
    Returns a dict with inclusion/exclusion and constraints.
    """
    return {
        "inclusion": criterion_json,
        "exclusion": {},
        "constraints": {"initialEvent": None, "limit": limit}
    }


def generate_json_from_criteria_v2(input_body: Union[str, dict, list], idx: int = 0) -> Dict[str, Any]:
    """
    Entry point for API / FastAPI compatibility.
    - dict with inclusion/exclusion arrays
    - list of criteria
    - single string criterion
    Return: {"nct_id":..., "inclusion_criteria":[...], "exclusion_criteria":[...]}
    """
    if isinstance(input_body, dict):
        inclusion = input_body.get("inclusion") or []
        exclusion = input_body.get("exclusion") or []
        nct_id = input_body.get("nct_id", "NCT_UNKNOWN")
        inclusion_results = [generate_json(c, i) for i, c in enumerate(inclusion)]
        exclusion_results = [generate_json(c, i) for i, c in enumerate(exclusion)]
        output = {
            "nct_id": nct_id or "NCT_UNKNOWN",
            "inclusion_criteria": inclusion_results,
            "exclusion_criteria": exclusion_results
        }
        return output
    elif isinstance(input_body, list):
        return {
            "nct_id": "NCT_UNKNOWN",
            "inclusion_criteria": [generate_json(c, i) for i, c in enumerate(input_body)],
            "exclusion_criteria": []
        }
    elif isinstance(input_body, str):
        return {
            "nct_id": "NCT_UNKNOWN",
            "inclusion_criteria": [generate_json(input_body, idx)],
            "exclusion_criteria": []
        }
    else:
        raise TypeError(f"Unsupported input type for generate_json_from_criteria_v2: {type(input_body)}")
