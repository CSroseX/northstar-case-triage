# Northstar case triage

A service that takes one Northstar Field Services service request and returns the correct
**first action** for it, as a structured case result.

The goal is not speed on its own. A fast wrong acknowledgement costs more than a slightly
slower right one: it tells a customer a technician is coming when nobody is, or treats a
gas smell as a routine service booking. So the service is built to reach the *correct*
first action, and to hand a case to a human whenever the records cannot settle it.

## What it does

`POST /cases/process` takes one request and returns one of eight statuses:

| Status | When |
|---|---|
| `human_escalation_required` | A possible hazard, or a repeat that a coordinator must judge |
| `clarification_required` | One question is needed before anything can progress |
| `account_review_required` | Coverage cannot be confirmed from the agreement record |
| `duplicate_detected` | The same fault is already raised; linked, not raised again |
| `covered_action` | Covered planned work, a coverage answer, or covered remote support |
| `dispatch_ready` | A covered breakdown with a technician; the only case that writes a work order |
| `resource_escalation_required` | No qualified, available technician for the site |
| `failed` | A dependency could not be read, or a write could not be confirmed |

Each response carries the resolved customer/site/asset, the entitlement evidence, the next
actions, a customer-ready reply draft, and a `decisionTrace` recording every check that
ran and the policy behind it.

Two extra endpoints:

- `GET /health` — liveness, and whether each credential is present (never its value)
- `GET /cases/recent` — the last few decision traces, newest first, for a coordinator

## Setup

Python 3.12. Two credentials are needed, supplied as environment variables only — nothing
secret is in the source or baked into the image.

```bash
cp .env.example .env
# then fill in both values in .env
```

`.env.example` lists them:

```
NORTHSTAR_ACCESS_TOKEN=     # Northstar business APIs
CELECO_MODEL_GATEWAY_KEY=   # Celeco model gateway
```

These are two different credentials and are not interchangeable — the Northstar token does
not authenticate to the model gateway. `.env` is gitignored and excluded from the Docker
image.

The loader also accepts the alternative names `API_Access_Key` and `Model_Access_key`, and
reads both `KEY=value` and `Key: value` lines, because the supplied credentials file uses
the latter.

### Optional settings

Two more bound how long a case can spend waiting on Northstar. The defaults are what the
service ships with and neither needs setting to run it.

| Variable | Default | What it does |
|---|---|---|
| `NORTHSTAR_TIMEOUT_SECONDS` | `5` | Per-request timeout for Northstar business reads. Separate from the model call, which legitimately takes longer. |
| `EVIDENCE_BUDGET_SECONDS` | `15` | Wall-clock ceiling for gathering all the evidence for one case. |

A safety case must never wait behind a slow dependency, and the provided runner allows
120s for a whole case. The independent reads run concurrently, and anything not back when
the budget expires counts as a failed lookup: the case is then decided on what did arrive,
so a hazard still escalates immediately and everything else shows as a visible failure for
a coordinator rather than a silent default. Raise them if Northstar is slow but working;
lowering them makes the service give up on records sooner, not decide worse.

## Run in Docker

```bash
docker build -t northstar-triage .
docker run --rm -p 8080:8080 --env-file .env northstar-triage
```

The service listens on **8080**. Keys are passed at `docker run` time, never built into the
image, and the container runs as an unprivileged user.

Check it came up with both credentials loaded:

```bash
curl -s localhost:8080/health
# {"status":"ok","northstarConfigured":true,"modelConfigured":true}
```

If either field is `false` the service is running but will take the cautious path on every
request. The usual cause is `--env-file` silently parsing nothing: Docker's `--env-file`
requires `KEY=value` lines and ignores `Key: value`, so convert the file first if yours
uses colons.

Run it locally without Docker if you prefer:

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8080
```

## Send a request

```bash
curl -s -X POST localhost:8080/cases/process \
  -H 'Content-Type: application/json' \
  -H 'X-Event-ID: evt-0001' \
  -d '{
    "requestId": "REQ-1001",
    "receivedAt": "2026-09-20T09:31:00Z",
    "channel": "portal",
    "sender": {"name": "Deepa Kulkarni", "email": "ops@example.in"},
    "subject": "Freezer plant 2 stopped",
    "body": "AST-302 at SITE-021 has stopped cooling. There is no smoke, water or unusual smell.",
    "attachments": []
  }'
```

`X-Event-ID` is the idempotency key. Redelivering the same event returns the original
result and never creates a second work order; after a restart the event is recovered from
the work-order record rather than triaged again. A request without the header is still
decided, but will not write a work order, since the key is what makes the write safe to
retry.

The status you get back for any given message depends on the live records, not just the
text. The example above is a covered breakdown, but if the shared sandbox already holds an
open job or a recent request for `AST-302`, the same message is correctly treated as a
repeat and routed to a coordinator instead. `decisionTrace` in the response shows which
check decided it and the policy behind it, and `GET /cases/recent` shows the last few.

## Tests

```bash
pytest -q
```

210 tests, about 10 seconds, **no network and no model calls** — Northstar is served from
saved responses in `exploration/raw/` and the model is stubbed. The suite covers the
decision rules, the evidence lookups, booking and reconciliation, reply drafting, the
trace, and the schema contract in both directions.

The provided runner needs the service running:

```bash
node tests/celeco-visible-runner.mjs http://localhost:8080
```

This makes 8 real model calls, one per case. Current result: **8/8**.

There is also an offline walkthrough that needs no service and no credentials:

```bash
python scripts/demo.py     # 12/12, writes exploration/demo-output.md
```

An independent black-box stress suite lives in `tests/stress/` with its own README; it is
not part of `pytest` and makes real model calls.

## Layout

```
app/          service code
tests/        our tests, the provided runner and schemas, and tests/stress/
exploration/  API survey, saved responses, FINDINGS.md, demo output
resources/    the client policy PDFs and the submission schema
scripts/      demo.py, the offline walkthrough
agent-sessions/  full exported logs of the two AI sessions
```

`SOLUTION.md` explains the design and the reason behind each decision. `QUALITY.md` is the
testing history, including what was wrong along the way, and the known limits.
