import json
import re

# Cap patients drawn per graph response (large trials can match 10k+ patients)
MAX_GRAPH_PATIENTS = 200
_NEO4J_LABEL_SAFE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def ensure_graph_fields(row: dict) -> dict:
    """Fill graph fields when pred_list was stored empty in MySQL."""
    ci = row.get("criteria_index", 0)
    text = (row.get("criteria_text") or "").strip() or f"Criterion {ci}"
    row["display_label"] = text[:120]

    if not row.get("matched_node_id"):
        row["matched_node_id"] = f"criterion_{ci}"

    # matched_label must be a Neo4j node label (Condition), not criteria prose
    ml = str(row.get("matched_label") or "").strip()
    if not ml or not _NEO4J_LABEL_SAFE.match(ml):
        row["matched_label"] = "Condition"

    return row


def normalize_patient_rows(rows):
    norm = []

    for r in rows:
        # --- normalize IE ---
        ie = r.get("ie")
        if isinstance(ie, str):
            ie = ie.upper()
            if ie == "INCLUSION":
                ie = "I"
            elif ie == "EXCLUSION":
                ie = "E"

        # --- normalize match percent ---
        mp = r.get("match_percent", r.get("model_pred"))

        if isinstance(mp, str) and mp.endswith("%"):
            try:
                mp = int(mp.replace("%", ""))
            except ValueError:
                mp = None

        base = {
            "nct_id": r.get("nct_id"),
            "patient_id": r.get("patient_id") or r.get("claim_id"),
            "criteria_index": int(r.get("criteria_index")),
            "criteria_text": r.get("criteria_text"),
            "ie": ie,
            "match_percent": mp,
        }

        if r.get("matched_node_id"):
            norm.append({
                **base,
                "matched_label": r.get("matched_label"),
                "matched_node_id": r.get("matched_node_id"),
            })
            continue

        # CASE 2: model_prediction_pir (pred_list explosion)
        pred_list = r.get("pred_list") or []

        if isinstance(pred_list, str):
            try:
                pred_list = json.loads(pred_list)
            except Exception:
                pred_list = []

        added = False
        for p in pred_list:
            mid = p.get("matched_node_id")
            lab = p.get("matched_label")

            if not mid or not lab:
                continue

            norm.append({
                **base,
                "matched_label": lab,
                "matched_node_id": mid,
            })
            added = True

        # Keep criterion row for UI even when pred_list is empty or has no graph nodes
        if not added:
            norm.append({
                **base,
                "matched_label": None,
                "matched_node_id": None,
            })

    return norm
