"""The single model call per case.

The model READS: it turns one customer message into the small set of facts in
`app/facts.py`. It never decides a status and never sees a credential.

Budget discipline (CELECO-MODEL-001): one call per case, `temperature 0`,
`response_format: json_object`, and `max_tokens` kept tight because the gateway bills
output at `max_tokens` rather than at the tokens actually produced.

Failure handling: no retries. A timeout, an HTTP error, unparseable JSON or a payload
that does not match the facts model all produce `modelUnavailable`, which forces the
cautious path in `decide()`. A model that cannot be read must never clear a hazard.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .facts import RequestFacts, parse_facts

logger = logging.getLogger("northstar.model")

MAX_OUTPUT_TOKENS = 500

SYSTEM_PROMPT = """You read one field-service request for Northstar Field Services and \
return facts about it. You do not decide what Northstar should do.

Reply with JSON only, using exactly these keys:

"assetMentions": array of equipment identifiers the message names, e.g. ["AST-101"]. [] if none.
"safetySignal": one of:
   "affirmed"  - the message reports a possible immediate hazard: a fuel or gas leak, smoke,
                 water near electrical equipment, or someone dizzy or unwell near equipment.
   "denied"    - the sender explicitly rules those out, e.g. "there is no smoke, water or smell".
   "ambiguous" - the wording might indicate a hazard but is not clear either way.
   "absent"    - the message does not touch on any hazard.
"safetyQuote": the sender's exact words that drove the signal, copied verbatim from the
   message, including for "denied". "" when the signal is "absent". Never paraphrase.
"intent": one of "planned_service" (routine or scheduled maintenance arranged in advance),
   "breakdown" (equipment has failed, or is failing, now), "coverage_question" (asking what
   the agreement covers), "follow_up" (about a job already raised), "other".
   Decide this from the STATE OF THE EQUIPMENT, not from what the sender asks for. If the
   message says equipment is stopped, offline, failed, not starting, not holding
   temperature, or otherwise not working at the moment, the intent is "breakdown" — even
   when the sender frames it as a request for a particular kind of engineer, for a
   particular day, or as a scheduling question. Use "planned_service" only when nothing is
   reported as currently faulty.
"symptomSummary": a SHORT PHRASE naming the problem, in the sender's own terms, that can be
   dropped into the sentence "Issue: ___". Six words or fewer. No equipment name, no asset
   id, no site, no full sentence and no trailing full stop — those are added separately.
   Good: "freezer has stopped cooling", "rattling noise from the unit", "quarterly service
   due", "weak airflow", "controller fault 118". Bad: "The freezer plant 2 (AST-302) at the
   Hoskote cold store has stopped cooling." or "Quarterly service requested for Main DG; no
   fault reported."
"referencedRequests": array of earlier request or job identifiers the message points to,
   e.g. ["REQ-V001", "WO-9294"]. [] if none.
"requiresOnsite": true if the sender asks for someone to attend in person, false if the
   request can be handled remotely or is purely administrative.
"sameFaultAsExisting": compare this message with the open jobs and earlier requests listed
   below. "" if nothing was listed. Otherwise:
   "same"      - only when the message points at one of them (by identifier, or by saying
                 this is about a job already raised), OR describes the same symptom that
                 one of them already describes.
   "different" - a symptom that is not the one already listed, and with no reference to the
                 existing job. Equipment can develop a second, unrelated fault while a job
                 is open; being the same machine does not make it the same fault.
   "unclear"   - the message could plausibly be either and you cannot tell from the words.
   Do not answer "same" merely because the equipment matches.
"sameFaultReference": the identifier it matches when the answer is "same", otherwise "".
"refersToPreviousWork": true ONLY when the message asks us to look at work Northstar has
   already carried out on this equipment — "review the June visit", "compare with the July
   work order", "check what was done last time", "this is happening again after your last
   visit". It must be a request to revisit completed work. Set it false for everything
   else, including: a reply to a question we asked, a chase-up on a request that has not
   been attended yet, and a message that merely says a fault is happening "again" without
   asking us to look at the earlier job. If "isSafetyAnswer" is true, this is always false.
"isSafetyAnswer": true if this message reads as a reply to a safety question somebody asked
   the sender — a short answer about whether there is smoke, a smell, water or anyone
   unwell — rather than a new request. false otherwise.
"safetyAnswerUncertain": only meaningful when "isSafetyAnswer" is true. true when the reply
   leaves the hazard unresolved — "not sure", "can't tell", "I don't know", "nobody has
   checked yet", or any answer that does not actually settle the question. false when the
   reply clearly settles it either way.

Report what the message says. Do not infer a hazard that is not described, and do not
explain away one that is."""


@dataclass
class ModelResult:
    """Facts plus the trace id for the audit record."""

    facts: RequestFacts
    traceId: str | None = None
    warnings: list[str] | None = None

    @property
    def traceIds(self) -> list[str]:
        return [self.traceId] if self.traceId else []


class _DevCache:
    """Dev-only cache of model answers, keyed by request content.

    Off unless MODEL_CACHE_DIR is set. Lets the same cases be re-run while developing
    without spending budget. Never enabled in a submitted run: the key is derived from
    the request text, so a changed message always misses.
    """

    def __init__(self, directory: str | None) -> None:
        self.path = Path(directory) if directory else None
        if self.path:
            self.path.mkdir(parents=True, exist_ok=True)
            logger.info("model dev cache enabled at %s", self.path)

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def _key(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32]

    def get(self, prompt: str) -> dict[str, Any] | None:
        if not self.path:
            return None
        candidate = self.path / f"{self._key(prompt)}.json"
        if not candidate.is_file():
            return None
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, prompt: str, payload: dict[str, Any]) -> None:
        if not self.path:
            return
        try:
            (self.path / f"{self._key(prompt)}.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover - cache failure must never break a case
            logger.warning("model cache write failed: %s", exc)


def build_user_prompt(
    subject: str,
    body: str,
    open_work_orders: list[dict[str, Any]] | None = None,
    recent_requests: list[dict[str, Any]] | None = None,
) -> str:
    """The message plus only the records needed to judge a repeat fault.

    Deliberately narrow: the request text and the jobs/requests for this one asset.
    No credentials, no customer list, no agreement terms — the model does not decide
    coverage, so it is not shown coverage.
    """
    parts = [f"REQUEST\nSubject: {subject}\nBody: {body}"]

    if open_work_orders:
        lines = [
            f"- {w.get('id')}: {w.get('summary')} (status {w.get('status')}, "
            f"raised by {w.get('requestId')})"
            for w in open_work_orders
        ]
        parts.append("OPEN JOBS ON THIS EQUIPMENT\n" + "\n".join(lines))

    if recent_requests:
        lines = [
            f"- {r.get('requestId') or r.get('id')} ({r.get('receivedAt')}): "
            f"{r.get('subject')} — {str(r.get('body') or '')[:200]}"
            for r in recent_requests
        ]
        parts.append("EARLIER REQUESTS FOR THIS EQUIPMENT\n" + "\n".join(lines))

    if not open_work_orders and not recent_requests:
        parts.append("No open jobs or earlier requests were found for this equipment.")

    return "\n\n".join(parts)


class ModelClient:
    """One call per case against the Celeco gateway. No retries by design."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        cache_dir: str | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._cache = _DevCache(
            cache_dir if cache_dir is not None else os.getenv("MODEL_CACHE_DIR")
        )

    async def __aenter__(self) -> ModelClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.request_timeout_seconds)
            self._owns_client = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def extract_facts(
        self,
        subject: str,
        body: str,
        open_work_orders: list[dict[str, Any]] | None = None,
        recent_requests: list[dict[str, Any]] | None = None,
    ) -> ModelResult:
        """One call. Any failure returns modelUnavailable facts rather than raising."""
        user_prompt = build_user_prompt(subject, body, open_work_orders, recent_requests)

        cached = self._cache.get(user_prompt)
        if cached is not None:
            logger.info("model answer served from dev cache")
            return ModelResult(
                facts=parse_facts(cached.get("payload")),
                traceId=cached.get("traceId"),
                warnings=["Model answer served from the development cache"],
            )

        if not self._settings.model_configured:
            logger.warning("model gateway key not configured")
            return ModelResult(
                facts=parse_facts(None),
                warnings=["Model gateway is not configured; cautious path applied"],
            )

        request_body = {
            "model": self._settings.model_alias,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "response_format": {"type": "json_object"},
        }

        if self._client is None:
            raise RuntimeError("ModelClient used outside its context manager")

        try:
            response = await self._client.post(
                self._settings.model_endpoint,
                headers={
                    "authorization": f"Bearer {self._settings.model_key.reveal()}",
                    "content-type": "application/json",
                },
                json=request_body,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # No retry: a second call costs budget and the cautious path is already correct.
            logger.warning("model call failed: %s", type(exc).__name__)
            return ModelResult(
                facts=parse_facts(None),
                warnings=[f"Model call failed ({type(exc).__name__}); cautious path applied"],
            )

        if response.status_code != 200:
            logger.warning("model call returned HTTP %s", response.status_code)
            return ModelResult(
                facts=parse_facts(None),
                warnings=[
                    f"Model call returned HTTP {response.status_code}; cautious path applied"
                ],
            )

        try:
            envelope = response.json()
        except ValueError:
            return ModelResult(
                facts=parse_facts(None),
                warnings=["Model response was not valid JSON; cautious path applied"],
            )

        trace_id = (envelope.get("celeco") or {}).get("traceId") or envelope.get("id")

        try:
            content = envelope["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ModelResult(
                facts=parse_facts(None),
                traceId=trace_id,
                warnings=["Model response had no message content; cautious path applied"],
            )

        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            return ModelResult(
                facts=parse_facts(None),
                traceId=trace_id,
                warnings=["Model returned malformed JSON; cautious path applied"],
            )

        if not isinstance(payload, dict):
            return ModelResult(
                facts=parse_facts(None),
                traceId=trace_id,
                warnings=["Model returned JSON that was not an object; cautious path applied"],
            )

        self._cache.put(user_prompt, {"payload": payload, "traceId": trace_id})

        facts = parse_facts(payload)
        warnings: list[str] = []
        # parse_facts degrades an unrecognised signal to "ambiguous"; say so in the audit.
        if payload.get("safetySignal") not in {"affirmed", "denied", "ambiguous", "absent"}:
            warnings.append(
                "Model returned an unrecognised safety signal; treated as ambiguous"
            )
        return ModelResult(facts=facts, traceId=trace_id, warnings=warnings)
