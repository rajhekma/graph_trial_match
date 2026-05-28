# backend/app/db_neo4j.py
import os
import logging
import re
from neo4j import GraphDatabase
from dotenv import load_dotenv
from neo4j.time import Date as NeoDate, DateTime as NeoDateTime

load_dotenv()
logger = logging.getLogger("db_neo4j")
logger.setLevel(logging.INFO)

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASS", "password")

_driver = None


def get_driver():
    """Return Neo4j driver (singleton)."""
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USER, NEO4J_PASS),
            max_connection_lifetime=3600,
        )
    return _driver


def safe_value(v):
    """Safely convert Neo4j values to JSON-friendly format."""
    if isinstance(v, (NeoDate, NeoDateTime)):
        try:
            return v.iso_format()
        except Exception:
            return str(v)
    if isinstance(v, list):
        return [safe_value(x) for x in v]
    if isinstance(v, dict):
        return {k: safe_value(val) for k, val in v.items()}
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", errors="ignore")
        except Exception:
            return str(v)
    return v


_LABEL_SAFE = re.compile(r"^[A-Za-z0-9_]+$")


def expand_labels_get_props(label_requests, chunk_size: int = 500):
    logger.info("expand_labels_get_props() called with %d requests", len(label_requests))

    if not label_requests:
        logger.warning("No label_requests provided — returning empty list")
        return []

    driver = get_driver()

    original_uuids = [lr.get("id") for lr in label_requests]
    logger.info("Original UUIDs extracted: %d", len(original_uuids))

    # dedupe while preserving order
    seen = {}
    deduped_uuids = []
    for u in original_uuids:
        if u and u not in seen:
            seen[u] = True
            deduped_uuids.append(u)

    logger.info("Deduped UUID count: %d", len(deduped_uuids))

    if not deduped_uuids:
        logger.warning("No usable UUIDs — returning all None centers")
        return [{"center": None} for _ in label_requests]

    # group by label
    label_to_uuids = {}
    for lr in label_requests:
        u = lr.get("id")
        lbl = lr.get("label")
        if not u or not lbl:
            logger.debug("Skipping entry (id=%s, label=%s)", u, lbl)
            continue
        if not _LABEL_SAFE.match(lbl):
            logger.error("Unsafe label name detected! label=%s  Skipping.", lbl)
            continue
        label_to_uuids.setdefault(lbl, [])
        if u not in label_to_uuids[lbl]:
            label_to_uuids[lbl].append(u)

    logger.info("Grouped by label: %s", {k: len(v) for k, v in label_to_uuids.items()})

    found = {}

    try:
        with driver.session(default_access_mode="READ") as session:
            for label, uuids in label_to_uuids.items():
                logger.info("Processing label=%s  UUID count=%d", label, len(uuids))
                for i in range(0, len(uuids), chunk_size):
                    batch = uuids[i:i + chunk_size]
                    logger.info(" → Querying label=%s  batch=%d..%d  size=%d", label, i, i + len(batch) - 1, len(batch))
                    cypher = f"""
                    UNWIND $batch AS u
                    MATCH (n:{label} {{id: u}})
                    RETURN u AS uuid, apoc.convert.toMap(n) AS node
                    """
                    rows = session.run(cypher, {"batch": batch}).data()
                    logger.info(" ← Returned rows for label=%s: %d", label, len(rows))
                    # log a small sample of what apoc returned (first 3)
                    for r in rows[:3]:
                        logger.debug("NEO4J ROW SAMPLE for label=%s: %s", label, r.get("node"))

                    for r in rows:
                        uuid = r.get("uuid")
                        node = r.get("node")
                        if node is not None:
                            found[uuid] = node
                        else:
                            logger.warning("Row with NULL node returned: %s", r)
    except Exception as exc:
        logger.exception("Neo4j expansion failed during execution: %s", exc)
        return [{"center": None} for _ in label_requests]

    logger.info("Total UUIDs found in DB: %d", len(found))

    # Build final output preserving original order
    final = []
    missing_count = 0

    for lr in label_requests:
        uuid_val = lr.get("id")
        required_label = lr.get("label")

        nd = found.get(uuid_val)

        if nd is None:
            missing_count += 1
            logger.debug("UUID NOT FOUND: %s (label=%s)", uuid_val, required_label)
            final.append({"center": None})
            continue

        labs = nd.get("labels", [])
        # Extract real properties: apoc returns properties at top-level of `nd` (not inside `props`) in many exports.
        # We'll collect all keys except labels + id as the node properties.
        node_props = {}
        for k, v in nd.items():
            if k in ("labels", "id"):
                continue
            node_props[k] = safe_value(v)

        # Debug: show keys discovered for the node (first few)
        logger.debug("Node %s keys: %s", uuid_val, list(node_props.keys())[:10])

        # If node_props is empty, attempt to fallback to CSV (PIR_DF) to enrich (useful if properties were flattened in CSV)
        if not node_props:
            try:
                # find first CSV row that references this matched_node_id
                # note: PIR_DF contains many patient rows; we find a row where matched_node_id==uuid_val
                rows = PIR_DF[PIR_DF["matched_node_id"].astype(str) == str(uuid_val)]
                if not rows.empty:
                    # convert first matching row to a dict (exclude patient-specific fields if you want)
                    row0 = rows.iloc[0].to_dict()
                    # choose a subset of useful columns from CSV to present (you can adjust list)
                    # we'll include match_percent, matched_label, criteria_index and other non-empty columns
                    fallback = {}
                    for k, vv in row0.items():
                        if vv is None or vv == "" or k in ("patient_id",):
                            continue
                        fallback[k] = vv
                    if fallback:
                        node_props = fallback
                        logger.debug("Used CSV fallback props for %s: keys=%s", uuid_val, list(fallback.keys())[:10])
            except Exception:
                logger.exception("CSV fallback failed for uuid=%s", uuid_val)

        final.append({
            "center": {
                "id": nd.get("id"),
                "labels": labs,
                "props": node_props
            }
        })

    logger.info("expand_labels_get_props() completed. Returned=%d  missing=%d", len(final), missing_count)

    # Also log a small sample of final centers for inspection
    try:
        sample = []
        for f in final[:8]:
            center = f.get("center") or {}
            props = center.get("props") or {}
            sample.append({"id": center.get("id"), "props_keys": list(props.keys())[:8]})
        logger.info("expand_labels_get_props() sample: %s", sample)
    except Exception:
        logger.exception("Failed to build expansion sample log")

    return final
