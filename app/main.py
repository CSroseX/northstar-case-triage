"""FastAPI entrypoint.

GET  /health         -> 200
POST /cases/process  -> case-input.schema.json in, case-result.schema.json out
"""

from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .config import load_settings
from .model_client import ModelClient
from .models import CaseInput, CaseResult
from .northstar_client import NorthstarClient
from .pipeline import process_case

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("northstar.triage")

class Utf8JSONResponse(JSONResponse):
    """JSON with an explicit charset.

    Starlette's default omits it, so a client that does not assume UTF-8 — a Windows
    terminal, or a mail client pasting a draft — renders non-ASCII characters as
    mojibake. The drafts themselves stay plain ASCII, but customer names and equipment
    nicknames come from the records and may not be.
    """

    media_type = "application/json; charset=utf-8"


settings = load_settings()
app = FastAPI(
    title="Northstar case triage",
    version="0.2.0",
    default_response_class=Utf8JSONResponse,
)

# Transport repeats: the same X-Event-ID delivered again must return the original result,
# never a second decision. In-process for now; a submitted runtime would persist this.
_RESULTS_BY_EVENT_ID: dict[str, CaseResult] = {}


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness. Reports whether credentials are present, never their values."""
    return {
        "status": "ok",
        "northstarConfigured": settings.northstar_configured,
        "modelConfigured": settings.model_configured,
    }


@app.post("/cases/process", response_model=CaseResult, response_model_exclude_none=False)
async def process(
    case: CaseInput,
    x_event_id: str | None = Header(default=None, alias="X-Event-ID"),
) -> CaseResult:
    """Return the correct first action for one service request."""
    # Log identifiers only: request bodies carry customer detail, and nothing secret
    # is ever logged.
    logger.info("processing request=%s channel=%s event=%s", case.requestId, case.channel, x_event_id)

    if x_event_id and x_event_id in _RESULTS_BY_EVENT_ID:
        logger.info("event=%s already processed; returning the stored result", x_event_id)
        return _RESULTS_BY_EVENT_ID[x_event_id]

    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as http:
        northstar = NorthstarClient(settings, client=http)
        model = ModelClient(settings, client=http)
        result = await process_case(case, northstar, model, x_event_id)

    if x_event_id:
        _RESULTS_BY_EVENT_ID[x_event_id] = result

    logger.info("request=%s -> status=%s", case.requestId, result.status)
    return result


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never leak a traceback to the caller; an unexpected fault is still a coordinator's problem."""
    logger.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal_error"})
