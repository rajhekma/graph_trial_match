# # app/db_mysql.py
# import os
# import csv
# from typing import List, Dict, Any, Optional

# # Path to demo CSV
# CSV_PATH = os.getenv("PIR_CSV", "./pir_real_patients.csv")

# def _read_csv_rows(limit: Optional[int] = None) -> List[Dict[str, Any]]:
#     rows = []
#     if not os.path.exists(CSV_PATH):
#         return []
#     with open(CSV_PATH, newline="", encoding="utf-8") as fh:
#         reader = csv.DictReader(fh)
#         for i, r in enumerate(reader):
#             rows.append(r)
#             if limit and i + 1 >= limit:
#                 break
#     return rows

# def fetch_top_bucket_records_csv_fallback(nct_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
#     """
#     Simple CSV-based function used as demo MySQL fallback.
#     Strategy:
#       - read rows for nct_id
#       - determine numeric match_percent and pick rows in the highest bucket
#       - return up to `limit` rows
#     """
#     rows = _read_csv_rows(limit=None)
#     if not rows:
#         return []

#     # filter by nct_id
#     filtered = [r for r in rows if str(r.get("nct_id", "")).strip() == str(nct_id)]
#     if not filtered:
#         return []

#     # parse match_percent numeric
#     def to_num(x):
#         try:
#             if x is None:
#                 return None
#             s = str(x).strip().replace("%", "")
#             if s == "":
#                 return None
#             return int(float(s))
#         except Exception:
#             return None

#     numeric_vals = [to_num(r.get("match_percent")) for r in filtered]
#     numeric_vals_clean = [v for v in numeric_vals if v is not None]
#     if not numeric_vals_clean:
#         # return first `limit` rows
#         return filtered[:limit]

#     max_pct = max(numeric_vals_clean)
#     top_rows = [r for r, v in zip(filtered, numeric_vals) if v == max_pct]
#     return top_rows[:limit]

# app/db_mysql.py
import os
import json
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASS = os.getenv("MYSQL_PASS")
MYSQL_DB   = os.getenv("MYSQL_DB")

def get_connection():
    return mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        database=MYSQL_DB,
        autocommit=True
    )

def fetch_patient_match_rows(nct_id: str):
    """
    Fetch raw prediction rows for graph visualization.

    IMPORTANT:
    - One DB row == one patient + one criteria
    - pred_list contains multiple matched concepts
    - Row explosion happens in normalize_patient_rows()
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT
                id,
                claim_id,
                nct_id,
                ie,
                criteria_index,
                true_label,
                model_pred,
                pred_list,
                criteria_text
            FROM model_prediction_pir
            WHERE nct_id = %s
        """, (nct_id,))

        rows = cursor.fetchall()

        # JSON-decode pred_list
        for r in rows:
            if isinstance(r.get("pred_list"), str):
                try:
                    r["pred_list"] = json.loads(r["pred_list"])
                except Exception:
                    r["pred_list"] = []

        return rows

    finally:
        cursor.close()
        conn.close()
