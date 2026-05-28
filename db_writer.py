import os
import json
import mysql.connector
from dotenv import load_dotenv
import logging
from typing import Any, Dict, List
from datetime import date, datetime
import re

load_dotenv()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASS = os.getenv("MYSQL_PASS")
MYSQL_DB   = os.getenv("MYSQL_DB", "operation_v1")


def json_safe(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (set, tuple)):
        return list(obj)
    return str(obj)


def get_connection():
    return mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        database=MYSQL_DB,
        autocommit=False,          
        connection_timeout=15
    )


def extract_fhir_address_from_string(address_raw: str) -> dict:
    if not address_raw or not isinstance(address_raw, str):
        return {}

    def grab(field):
        m = re.search(rf"{field}='([^']*)'", address_raw)
        return m.group(1) if m else ""

    def grab_line():
        m = re.search(r"line=\[([^\]]+)\]", address_raw)
        if not m:
            return ""
        return m.group(1).replace("'", "").strip()

    return {
        "line": grab_line(),
        "state": grab("state"),
        "city": grab("city"),
        "postalCode": grab("postalCode"),
        "country": grab("country"),
    }


def build_address_text(addr: dict) -> str:
    return ", ".join([
        addr.get("line", ""),
        addr.get("state", ""),
        addr.get("city", ""),
        addr.get("postalCode", ""),
        addr.get("country", ""),
    ])


def normalize_address_for_frontend(details: dict):
    if not details or not isinstance(details, dict):
        return details

    text = details.get("addressText", "")
    if text and "None" not in text and "[" not in text:
        return details

    # Try structured address first, fallback to addressText
    addr = details.get("address") or details.get("addressText")

    if isinstance(addr, str):
        addr_dict = extract_fhir_address_from_string(addr)
    elif isinstance(addr, list) and addr:
        addr_dict = addr[0]
    elif isinstance(addr, dict):
        addr_dict = addr
    else:
        addr_dict = {}

    details["addressText"] = build_address_text(addr_dict)
    return details


# ---------------------- INSERT INTO patient_match_pir ----------------------
def insert_patient_matches(result_data: Dict[str, Any]):
    match_results = result_data.get("match_results", [])
    if not match_results:
        logger.info("No patient match results found — skipping insert.")
        return

    nct_id = result_data.get("nct_id", "NCT_UNKNOWN")

    delete_query = "DELETE FROM patient_match_pir WHERE nct_id = %s"

    insert_query = """
        INSERT INTO patient_match_pir
        (nct_id, patientId, siteId, criteria_type, criteria_text, percentage_match, match_details)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """

    batch_data = []
    for match in match_results:
        site_id = match.get("site_id") or None
        criteria_type = "inclusion" if match.get("exclusion_hits", 0) == 0 else "exclusion"
        batch_data.append((
            nct_id,
            match.get("claim_id"),
            site_id,
            criteria_type,
            match.get("criteria_text") or "",
            match.get("match_percent", 0),
            json.dumps(match.get("details", {}), default=json_safe)
        ))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(delete_query, (nct_id,))
        cursor.executemany(insert_query, batch_data)
        conn.commit()
        logger.info(f"Inserted {len(batch_data)} rows into patient_match_pir.")
    except Exception as e:
        conn.rollback()
        logger.error(f"Error inserting into patient_match_pir: {e}", exc_info=True)
    finally:
        cursor.close()
        conn.close()


# ---------------------- INSERT INTO model_prediction_pir ----------------------
def insert_model_predictions(result_data: Dict[str, Any]):
    pred_records = result_data.get("patient_match_records", [])
    if not pred_records:
        logger.info("No model prediction records found — skipping insert.")
        return

    nct_id = result_data.get("nct_id", "NCT_UNKNOWN")

    delete_query = "DELETE FROM model_prediction_pir WHERE nct_id = %s"

    insert_query = """
        INSERT INTO model_prediction_pir
        (claim_id, nct_id, ie, criteria_index, model_pred, pred_list, criteria_text)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """

    batch_data = []
    for rec in pred_records:
        batch_data.append((
            rec.get("claim_id"),
            nct_id,
            rec.get("ie", "i"),
            rec.get("criteria_index", 0),
            rec.get("model_pred", 0),
            json.dumps(rec.get("pred_list", []), default=json_safe),
            rec.get("criteria_text", "")
        ))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(delete_query, (nct_id,))
        cursor.executemany(insert_query, batch_data)
        conn.commit()
        logger.info(f"Inserted {len(batch_data)} rows into model_prediction_pir.")
    except Exception as e:
        conn.rollback()
        logger.error(f"Error inserting into model_prediction_pir: {e}", exc_info=True)
    finally:
        cursor.close()
        conn.close()


# ---------------------- FETCH PAGINATED PATIENTS ----------------------
def fetch_paginated_patients(nct_id: str, page: int = 1, page_size: int = 10):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM patient_match_pir
            WHERE nct_id = %s
        """, (nct_id,))
        total_count = cursor.fetchone()["total"]

        offset = (page - 1) * page_size

        cursor.execute("""
            SELECT patientId, siteId, percentage_match, match_details
            FROM patient_match_pir
            WHERE nct_id = %s
            ORDER BY percentage_match DESC, patientId
            LIMIT %s OFFSET %s
        """, (nct_id, page_size, offset))

        patients = cursor.fetchall()

        for p in patients:
            if isinstance(p.get("match_details"), str):
                try:
                    p["match_details"] = json.loads(p["match_details"])
                except Exception:
                    p["match_details"] = {}

            p["match_details"] = normalize_address_for_frontend(p["match_details"])

        return {
            "patients": patients,
            "count": len(patients),
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
        }

    finally:
        cursor.close()
        conn.close()
