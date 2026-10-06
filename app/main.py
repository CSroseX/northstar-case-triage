"""FastAPI entrypoint.

GET  /health         -> 200
POST /cases/process  -> case-input.schema.json in, case-result.schema.json out
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .config import load_settings
from .models import CaseInput, CaseResult
from .triage import triage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("northstar.triage")

settings = load_settings()
app = FastAPI(title="Northstar case triage", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness. Reports whether credentials are present, never their values."""
    return {
        "status": "ok",
        "northstarConfigured": settings.northstar_configured,
        "modelConfigured": settings.model_configured,
    }


@app.post("/cases/process", response_model=CaseResult, response_model_exclude_none=False)
async def process_case(
    case: CaseInput,
    x_event_id: str | None = Header(default=None, alias="X-Event-ID"),
) -> CaseResult:
    """Return the correct first action for one service request."""
    # Log identifiers only. Request bodies may contain customer detail, and nothing
    # secret is ever logged.
    logger.info("processing request=%s channel=%s event=%s", case.requestId, case.channel, x_event_id)
    result = triage(case, x_event_id)
    logger.info("request=%s -> status=%s", case.requestId, result.status)
    return result


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never leak a traceback to the caller; an unexpected fault is still a coordinator's problem."""
    logger.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal_error"})
