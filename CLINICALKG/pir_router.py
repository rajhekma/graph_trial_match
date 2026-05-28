import logging
from fastapi import APIRouter, HTTPException

from CLINICALKG.db_mysql import fetch_patient_match_rows
from CLINICALKG.utils_pir_normalizer import normalize_patient_rows
from CLINICALKG.db_neo4j import expand_labels_get_props

logger = logging.getLogger("pir-api")

router = APIRouter()

# HEALTH
@router.get("/health")
def health():
    return {"status": "ok"}

# RESULTS (TOP MATCH BUCKET)
@router.get("/nct/{nct_id}/results")
def get_results(nct_id: str):
    rows = fetch_patient_match_rows(nct_id)
    if not rows:
        return {"nct_id": nct_id, "records": []}

    norm = normalize_patient_rows(rows)

    max_pct = max(
        r["match_percent"] for r in norm
        if r.get("match_percent") is not None
    )

    top = [r for r in norm if r["match_percent"] == max_pct]

    return {
        "nct_id": nct_id,
        "bucket": f"{max_pct}%",
        "records": top
    }

# SINGLE INCLUSION / EXCLUSION CLUSTER
@router.get("/nct/{nct_id}/{mode}/{criteria_index}")
def get_single_cluster(nct_id: str, mode: str, criteria_index: int):
    if mode not in ("inclusion", "exclusion"):
        raise HTTPException(status_code=400, detail="Invalid mode")

    ie = "I" if mode == "inclusion" else "E"

    rows = fetch_patient_match_rows(nct_id)
    norm = normalize_patient_rows(rows)

    filtered = [
        r for r in norm
        if r["criteria_index"] == criteria_index and r["ie"] == ie
    ]

    if not filtered:
        return {
            "nct_id": nct_id,
            "criteria_index": criteria_index,
            "labels": [],
            "label_expansions": [],
            "patients": []
        }

    label_map = {}
    for r in filtered:
        mid = r.get("matched_node_id")
        lab = r.get("matched_label")
        if mid and lab:
            label_map[mid] = lab

    labels = [{"id": k, "label": v} for k, v in label_map.items()]
    expansions = expand_labels_get_props(labels) if labels else []

    return {
        "nct_id": nct_id,
        "criteria_index": criteria_index,
        "labels": labels,
        "label_expansions": expansions,
        "patients": filtered
    }

# ALL INCLUSIONS (MULTI-RING)
@router.get("/nct/{nct_id}/all_inclusions")
def get_all_inclusions(nct_id: str):
    rows = fetch_patient_match_rows(nct_id)
    norm = normalize_patient_rows(rows)

    nodes = []
    edges = []
    seen_patients = set()

    for r in norm:
        if r["ie"] != "I":
            continue

        mid = r.get("matched_node_id")
        lab = r.get("matched_label")
        pid = r.get("patient_id")

        if not mid or not lab or not pid:
            continue

        cid = f"concept_{mid}"
        pid_id = f"pat__{pid}"

        nodes.append({
            "id": cid,
            "type": "label",
            "label": lab,
            "props": {
                "matched_node_id": mid,
                "matched_label": lab,
                "criteria_index": r["criteria_index"]
            }
        })

        if pid_id not in seen_patients:
            seen_patients.add(pid_id)
            nodes.append({
                "id": pid_id,
                "type": "patient",
                "label": pid,
                "props": r
            })

        edges.append({
            "source": cid,
            "target": pid_id,
            "type": "match",
            "criteria_index": r["criteria_index"]
        })

    return {"nct_id": nct_id, "nodes": nodes, "edges": edges}

# ALL EXCLUSIONS (MULTI-RING)
@router.get("/nct/{nct_id}/all_exclusions")
def get_all_exclusions(nct_id: str):
    rows = fetch_patient_match_rows(nct_id)
    norm = normalize_patient_rows(rows)

    nodes = []
    edges = []
    seen_patients = set()

    for r in norm:
        if r["ie"] != "E":
            continue

        mid = r.get("matched_node_id")
        lab = r.get("matched_label")
        pid = r.get("patient_id")

        if not mid or not lab or not pid:
            continue

        cid = f"concept_{mid}"
        pid_id = f"pat__{pid}"

        nodes.append({
            "id": cid,
            "type": "label",
            "label": lab,
            "props": {
                "matched_node_id": mid,
                "matched_label": lab,
                "criteria_index": r["criteria_index"]
            }
        })

        if pid_id not in seen_patients:
            seen_patients.add(pid_id)
            nodes.append({
                "id": pid_id,
                "type": "patient",
                "label": pid,
                "props": r
            })

        edges.append({
            "source": cid,
            "target": pid_id,
            "type": "excluded",
            "criteria_index": r["criteria_index"]
        })

    return {"nct_id": nct_id, "nodes": nodes, "edges": edges}

# NEO4J NODE EXPANSION
@router.post("/expand/nodes")
def expand_nodes(payload: dict):
    try:
        items = payload.get("items", [])
        lr = []

        for it in items:
            if not isinstance(it, dict):
                continue
            uid = it.get("id")
            lbl = it.get("label")
            if uid:
                lr.append({"id": str(uid), "label": lbl or ""})

        if not lr:
            return []

        return expand_labels_get_props(lr)

    except Exception as exc:
        logger.exception("expand_nodes failed")
        raise HTTPException(status_code=500, detail=str(exc))
