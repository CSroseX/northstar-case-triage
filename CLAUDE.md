# Northstar case triage — working rules

Service takes one Northstar service request and returns the correct **first action**.
A fast wrong acknowledgement costs more than a slightly slower right one. Never optimise
for automation rate.

## Hard rules

1. **Safety first, and it wins.** Hazard signal (fuel/gas leak, smoke, water near electrical
   equipment, anyone dizzy/unwell in an equipment room) → `human_escalation_required`.
   No troubleshooting advice, no work order, no dispatch. Escalate before resolving
   customer/site/asset — never delay escalation to finish a lookup.
2. **The model can never clear a safety signal.** Hazard wording present, or model output
   missing/malformed/timed out → cautious path. Ambiguous → ask ONE plain safety question
   (`clarification_required`) and progress nothing. "Not sure" or no reply stays with a human.
   Only a human duty owner releases an escalated case; the automation never does.
3. **Preserve the customer's exact trigger words** in the record. Never replace them with a
   generic label.
4. **No work order unless fully evidenced.** Customer, site, asset, entitlement AND technician
   match all supported by records, and nothing consequential uncertain. Anything else goes to
   a coordinator with the reason and evidence attached.
5. **Coverage comes from agreement records only.** Customer claims, payment receipts and
   account-manager messages are context, never evidence. Remote-only service mode does not
   authorise onsite dispatch. Expired/suspended/missing/conflicting → `account_review_required`.
   Asset-level `coverage` and agreement-level `serviceMode`/`status` can disagree — the
   stricter one controls.
   Request received before the agreement's start date → account review. Don't infer earlier terms.
   Take the agreement from the asset's own contractRef, and check the agreement's customer matches the asset's customer.
6. **Never guess identity.** Two assets could match → ask, don't pick. Asset ID → customer is
   the reliable path; sender domain is not verifiable against any record.
7. **Keys never** appear in model prompts, logs, source, or any written file. Environment
   variables only. The Northstar key does not authenticate to the model gateway.
8. **Model budget is 300 requests for the whole assessment** (1 used by exploration).
   One model call per case, `max_tokens` tight — output tokens bill at `max_tokens`, not actual.
   **Ask the user before spending any model call outside normal request handling.**
9. **Write idempotently.** Work-order create keys on `externalEventId` = `X-Event-ID`; a retry
   must reuse the same id. `duplicate: true` is a success, not an error — never retry it.
   504 after a write = uncertain: reconcile by request/event ref before retrying.
10. **Reads:** bounded retries with short backoff, never infinite, never substitute a guess.
    A failed dependency we depend on → visible failure state for a coordinator.

## Pipeline

`intake → read → evidence → decide → act → respond`

1. **intake** — validate input; known `X-Event-ID` → return the stored original result.
2. **read** — one model call → structured facts (asset/site mentions, safety signal
   affirmed/denied/ambiguous/absent + exact quote, intent, referenced requests). Validate it.
3. **evidence** — customer → site → asset → agreement in force at `receivedAt` → qualified &
   available technicians → open work orders.
4. **decide** — in order: safety → identity → duplicate → coverage → technician → status.
   **Model reads, code decides.** Rules come from the policies, not from test cases.
5. **act** — create a work order only for `dispatch_ready`, keyed on `X-Event-ID`.
6. **respond** — case result + customer draft by status (no advice on safety cases) + audit
   (policy ids, record ids, model trace ids, warnings).

Two kinds of duplicate: same `X-Event-ID` redelivered = transport repeat, return the original
result. Different request, same job = `duplicate_detected`. An open WO on the same asset is
**not** on its own a duplicate.

## Layout

`app/` service code · `tests/` ours + the provided runner/schemas · `exploration/` read-only
API survey + FINDINGS.md · `resources/` client PDFs (source of truth over any summary).

## Run / test

```bash
uvicorn app.main:app --port 8080            # local
pytest -q                                    # tests
docker build -t northstar-triage . && \
  docker run --rm -p 8080:8080 --env-file .env northstar-triage
node tests/celeco-visible-runner.mjs http://localhost:8080
```

Config comes from environment variables only; `.env` is local dev and is gitignored.
Keys are passed at `docker run` time, never baked into the image.
