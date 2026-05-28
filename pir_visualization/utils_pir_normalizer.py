import json

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

    return norm
