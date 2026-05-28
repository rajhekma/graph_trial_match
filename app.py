"""
Graph Trial Match API.

Trial criteria -> structured JSON -> Neo4j patient matching -> MySQL -> PIR visualization.
"""
from fastapi import FastAPI, HTTPException, Body, Request
from pydantic import BaseModel
from typing import Optional, Any
import os
import json
import logging

import httpx
from dotenv import load_dotenv
from starlette.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from trial_matching.json_generator import generate_json_from_criteria_v2
from trial_matching.cypher_engine_v2 import JsonToCypherRunnerV2
from db_writer import insert_patient_matches, insert_model_predictions, fetch_paginated_patients
from pir_visualization.pir_router import router as pir_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASS", "password")
EXTRACTOR_API_URL = os.getenv("EXTRACTOR_API_URL")

app = FastAPI(
    title="Graph Trial Match API",
    description="Clinical trial patient matching: LLM criteria parsing, Neo4j engine, PIR visualization",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

runner = None
try:
    runner = JsonToCypherRunnerV2(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASS)
    logger.info("JsonToCypherRunnerV2 initialized successfully.")
except Exception as e:
    logger.error(f"JsonToCypherRunnerV2 init failed: {e}")
    runner = None


class UserInput(BaseModel):
    user_input: Optional[str] = None
    inclusion: Optional[Any] = None
    exclusion: Optional[Any] = None


@app.get("/health")
def root_health():
    return {"status": "ok", "neo4j_runner": runner is not None}


@app.post("/generate_json")
async def generate_json_endpoint(body: dict = Body(...)):
    try:
        if isinstance(body.get("inclusion"), list) or isinstance(body.get("exclusion"), list):
            json_output = await run_in_threadpool(generate_json_from_criteria_v2, body)
            return json_output

        nct_code = body.get("nctCode") or body.get("nctId") or body.get("id") or body.get("nct_id")

        if not nct_code:
            if isinstance(body, str) and body.strip().upper().startswith("NCT"):
                nct_code = body.strip().upper()
            elif isinstance(body.get("input"), str) and body.get("input", "").strip().upper().startswith("NCT"):
                nct_code = body.get("input").strip().upper()

        if not nct_code and isinstance(body, str) and body.strip().isdigit():
            nct_code = "NCT" + body.strip()

        if nct_code:
            if not EXTRACTOR_API_URL:
                raise HTTPException(status_code=500, detail="EXTRACTOR_API_URL not configured")

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    EXTRACTOR_API_URL,
                    params={"id": nct_code},
                    headers={"Content-Type": "application/json"},
                )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Criteria extractor API failed: {resp.status_code}",
                )

            api_data = resp.json()
            refined = None
            try:
                refined = (
                    api_data.get("data", [{}])[0]
                    .get("FullStudy", {})
                    .get("RefinedCriteria")
                )
            except Exception:
                pass

            if not refined:
                refined = (
                    api_data.get("RefinedCriteria")
                    or api_data.get("PIR", {}).get("RefinedCriteria")
                )

            if not refined:
                raise HTTPException(
                    status_code=404,
                    detail="RefinedCriteria not found in extractor API response",
                )

            inclusion = refined.get("inclusion", [])
            exclusion = refined.get("exclusion", [])

            json_output = await run_in_threadpool(
                generate_json_from_criteria_v2,
                {"nct_id": nct_code, "inclusion": inclusion, "exclusion": exclusion},
            )
            json_output["nct_id"] = nct_code
            return json_output

        user_input_text = ""
        try:
            ui = UserInput(**body)
            if ui.user_input:
                user_input_text = ui.user_input
            else:
                parts = []
                if ui.inclusion:
                    parts.append(f"Include: {ui.inclusion}")
                if ui.exclusion:
                    parts.append(f"Exclude: {ui.exclusion}")
                user_input_text = " ".join(parts)
        except Exception:
            if isinstance(body.get("user_input"), str):
                user_input_text = body.get("user_input")

        if not user_input_text or not user_input_text.strip():
            raise HTTPException(
                status_code=400,
                detail="Provide nctCode or inclusion/exclusion arrays or user_input.",
            )

        json_output = await run_in_threadpool(generate_json_from_criteria_v2, body)
        return json_output

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"JSON generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/test_engine")
async def test_engine_endpoint(
    criteria_json: dict = Body(...),
    page: Optional[int] = 0,
    request: Request = None,
):
    if not runner:
        raise HTTPException(status_code=500, detail="Neo4j runner not initialized")

    try:
        nct_id = criteria_json.get("nct_id", "NCT_UNKNOWN")
        is_pagination_call = request is not None and "page" in request.query_params

        if not is_pagination_call:
            logger.info(f"Full engine run for {nct_id}")
            result = runner.run(criteria_json, nct_id=nct_id)
            insert_model_predictions(result)
            insert_patient_matches(result)
            page = 0

        db_page = page + 1
        page_data = fetch_paginated_patients(nct_id, db_page)

        return {
            "status": "success",
            "patients": page_data.get("patients", []),
            "final_count": page_data.get("total_count"),
            "mode": "pagination" if is_pagination_call else "initial_run",
            "nct_id": nct_id,
            "page": page,
        }

    except Exception as e:
        logger.error(f"Engine execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Engine failed: {str(e)}")


@app.post("/generate_and_run")
async def generate_and_run_endpoint(body: dict = Body(...)):
    if not runner:
        raise HTTPException(status_code=500, detail="Neo4j runner not initialized")

    try:
        json_criteria = await generate_json_endpoint(body)
        nct_id = body.get("nct_id") or json_criteria.get("nct_id", "NCT_UNKNOWN")
        result = runner.run(json_criteria, nct_id=nct_id)

        return {
            "nct_id": result.get("nct_id"),
            "included_count": result.get("included_count"),
            "excluded_count": result.get("excluded_count"),
            "final_count": result.get("final_count"),
            "match_groups": result.get("match_groups"),
            "final_patients": result.get("final_patients"),
        }

    except Exception as e:
        logger.error(f"Combined generate+run failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Combined execution failed: {str(e)}")


app.include_router(pir_router, prefix="/api", tags=["PIR_VISUALISATION"])


@app.on_event("shutdown")
async def shutdown_event():
    if runner:
        runner.close()
        logger.info("JsonToCypherRunnerV2 closed cleanly.")
