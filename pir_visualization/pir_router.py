import logging
from fastapi import APIRouter, HTTPException

from pir_visualization.db_mysql import fetch_patient_match_rows
from pir_visualization.utils_pir_normalizer import (
    normalize_patient_rows,
    ensure_graph_fields,
    MAX_GRAPH_PATIENTS,
)
from pir_visualization.db_neo4j import expand_labels_get_props

logger = logging.getLogger("pir-api")

router = APIRouter()


def _build_graph_from_norm(norm, ie: str, edge_type: str):
    """Build cytoscape nodes/edges from normalized rows (tolerates empty pred_list)."""
    nodes = []
    edges = []
    seen_patients = set()
    seen_concepts = set()
    patient_count = 0
    truncated = False

    for r in norm:
        if r.get("ie") != ie:
            continue

        r = ensure_graph_fields(dict(r))
        mid = r.get("matched_node_id")
        lab = r.get("display_label") or r.get("criteria_text") or r.get("matched_label")
        neo4j_label = r.get("matched_label") or "Condition"
        pid = r.get("patient_id")
        if not pid:
            continue

        cid = f"concept_{mid}"
        pid_id = f"pat__{pid}"

        if cid not in seen_concepts:
            seen_concepts.add(cid)
            nodes.append({
                "id": cid,
                "type": "label",
                "label": lab,
                "props": {
                    "matched_node_id": mid,
                    "matched_label": neo4j_label,
                    "display_label": lab,
                    "criteria_index": r["criteria_index"],
                },
            })

        if pid_id not in seen_patients:
            if patient_count >= MAX_GRAPH_PATIENTS:
                truncated = True
                continue
            seen_patients.add(pid_id)
            patient_count += 1
            nodes.append({
                "id": pid_id,
                "type": "patient",
                "label": str(pid),
                "props": r,
            })

        if pid_id in seen_patients:
            edges.append({
                "source": cid,
                "target": pid_id,
                "type": edge_type,
                "criteria_index": r["criteria_index"],
            })

    return nodes, edges, truncated


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
    if not norm:
        return {"nct_id": nct_id, "records": []}

    pcts = [r["match_percent"] for r in norm if r.get("match_percent") is not None]
    if not pcts:
        # Rows exist but no % bucket (e.g. criterion-level model_pred only) — return all for UI criteria list
        return {"nct_id": nct_id, "bucket": None, "records": norm}

    max_pct = max(pcts)
    top = [r for r in norm if r.get("match_percent") == max_pct]

    return {
        "nct_id": nct_id,
        "bucket": f"{max_pct}%",
        "records": top,
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
    display_name = ""
    for r in filtered:
        r = ensure_graph_fields(dict(r))
        display_name = display_name or r.get("display_label") or r.get("criteria_text") or ""
        mid = r.get("matched_node_id")
        neo4j_label = r.get("matched_label") or "Condition"
        if mid and not str(mid).startswith("criterion_"):
            label_map[mid] = neo4j_label

    if not label_map:
        cid = f"criterion_{criteria_index}"
        label_map[cid] = "Condition"
        for r in filtered:
            r["matched_node_id"] = cid

    labels = [
        {
            "id": k,
            "label": v,
            "display": (display_name or v)[:120],
        }
        for k, v in label_map.items()
    ]
    expansions = expand_labels_get_props(labels) if labels else []

    return {
        "nct_id": nct_id,
        "criteria_index": criteria_index,
        "labels": labels,
        "label_expansions": expansions,
        "patients": filtered,
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
    nodes, edges, truncated = _build_graph_from_norm(norm, "E", "excluded")
    out = {"nct_id": nct_id, "nodes": nodes, "edges": edges}
    if truncated:
        out["truncated"] = True
        out["max_patients"] = MAX_GRAPH_PATIENTS
    return out

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
