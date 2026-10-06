# Northstar case triage — working rules

Takes one Northstar service request, returns the correct **first action**. A fast wrong
acknowledgement costs more than a slightly slower right one. Never optimise for automation rate.

## Hard rules

1. **Safety first, and it wins.** Hazard signal (fuel/gas leak, smoke, water near electrical
   equipment, anyone dizzy/unwell in an equipment room) → `human_escalation_required`. No
   troubleshooting, no work order, no dispatch. Escalate before resolving customer/site/asset.
   **Our extension, beyond POL-SAFETY-001's list:** electric shock, electrocution and exposed
   wiring are hazards too. Not in the policy; my call, because the cost of treating one as
   routine is not symmetric with holding a case for a human. Flagged for Rohan.
2. **The model can never clear a safety signal.** Hazard wording, or model output
   missing/malformed/timed out → cautious path. Ambiguous → ask ONE plain safety question
   (`clarification_required`), progress nothing. "Not sure"/no reply stays with a human.
   Only a human duty owner releases an escalated case.
3. **Preserve the customer's exact trigger words.** Never a generic label.
4. **No work order unless fully evidenced.** Customer, site, asset, entitlement AND technician
   all supported by records, nothing consequential uncertain. Else → coordinator, with reason
   and evidence. **Planned work is never dispatched at intake** (OPS-DISPATCH-004): a covered
   planned service → acknowledge + schedule, no WO; the WO is raised when the visit is placed
   on the schedule. Only a breakdown reaches `dispatch_ready`.
5. **Coverage comes from agreement records only.** Customer claims, payment receipts and
   account-manager messages are context, never evidence. Remote-only does not authorise onsite
   dispatch — but remote-only where the customer only wants remote help is **`covered_action`,
   not account review**: nothing is wrong with the cover, so there is nothing for an account
   reviewer to decide. Account review is for cover that is in doubt. Either way, a remote-only
   agreement never produces a work order. Expired/suspended/missing/conflicting → `account_review_required`. Asset-level
   `coverage` and agreement `serviceMode`/`status` can disagree — the stricter controls.
   Received before the agreement's start date → account review; don't infer earlier terms.
   Take the agreement from the asset's `contractRef`; check its customer matches the asset's.
6. **Never guess identity.** Two assets could match → ask, don't pick. Asset ID → customer is
   the reliable path; sender domain is verifiable against no record. **Match `aliases` as well
   as `name`** — customers appear under legal *and* trading names, and a job was once recreated
   because two requests used different ones for one site.
7. **Duplicates turn on the fault, not the asset.** Same fault again (or more detail on an open
   one) → `duplicate_detected`, linked to the existing WO, never a second one. A genuinely
   *different* fault on the same asset → its own WO; skills, parts and coverage can differ.
   **Unclear which → a human decides.** Keep the original request IDs and channel on the link
   even when no WO is created. Watch for: two channels, forwarded mail with a changed subject,
   a second person reporting the same thing.
8. **Re-check availability immediately before creating the WO** (OPS-DISPATCH-004). It is
   operational, not static — a technician can take another emergency between triage and write.
   A surfaced technician is a *suggestion* re-verified at commit, never a booking. Gone at
   re-check → reopen selection; none left → resource escalation. Never write a placeholder WO
   against an unavailable or unqualified technician, and never substitute someone closer who
   lacks the skill or certification.
9. **Keys never** appear in model prompts, logs, source or any written file. Env vars only.
   The Northstar key does not authenticate to the model gateway.
10. **Model budget: 300 requests total** (1 used). One call per case, `max_tokens` tight —
    output bills at `max_tokens`, not actual. **Ask before any call outside request handling.**
11. **Write idempotently.** WO create keys on `externalEventId` = `X-Event-ID`; a retry reuses
    the same id. `duplicate: true` is success, never retried. 504 after a write = uncertain:
    reconcile by request/event ref first.
12. **Reads:** bounded retries, short backoff, never infinite, never a guess. Failed dependency
    we rely on → visible failure state for a coordinator.
13. **Acknowledgements** say what we understood, what happens next, what is still needed. Never
    expose internal prompts, confidence scores or raw errors. Never imply a technician is
    confirmed before the WO response is reconciled. 
14. When reporting results to me, explain them in plain language first, then the technical details.

## Our decisions (ours, not the client's — revisit with Rohan)

- **D1. Breakdowns: same-city only.** Only qualified, available technicians whose `city` matches
  the site's count; none → resource escalation. Rohan's practice is softer (reachability within
  the response window, travel as tie-breaker) but we have no travel-time or route data, and he
  says a distant technician is usually wrong for a tight response term. Not applied to planned
  work, where the route can be planned around travel.
- **D2. Planned acknowledgements promise no date.** Name the service and asset, say the visit is
  being scheduled against the contract, commit to no slot — we have no scheduling-lead-time data.
  Rohan wants a date by which we'd propose a slot; inventing it would be a guess, so the
  coordinator supplies it.

## Pipeline

`intake → read → evidence → decide → act → respond`

1. **intake** — validate; known `X-Event-ID` → return the stored result.
2. **read** — one model call → facts (asset/site mentions, safety signal
   affirmed/denied/ambiguous/absent + exact quote, intent, referenced requests). Validate.
3. **evidence** — customer (name *and* aliases) → site → asset → agreement in force at
   `receivedAt` → qualified & available technicians → open WOs on the asset.
4. **decide** — safety → identity → duplicate → coverage → **planned vs breakdown** →
   technician → status. **Model reads, code decides**, from the policies, not the test cases.
5. **act** — only a breakdown reaches `dispatch_ready`: re-check availability, then create the
   WO keyed on `X-Event-ID`. Planned work: acknowledge and schedule, no WO.
6. **respond** — result + customer draft by status (no advice on safety cases) + audit.

Three kinds of repeat: same `X-Event-ID` redelivered = transport repeat, return the stored
result. Same fault, different request = `duplicate_detected` linked to the existing WO.
Different fault, same asset = its own WO. An open WO on the asset is **not** on its own a
duplicate — compare customer, site, asset, symptoms and timing.

## Layout

`app/` service code · `tests/` ours + provided runner/schemas · `exploration/` API survey +
FINDINGS.md · `resources/` client PDFs, source of truth over any summary: POL-SAFETY-001,
POL-CONTRACT-002, OPS-INTAKE-003, OPS-DISPATCH-004, SYS-CATALOG-001, CELECO-MODEL-001.

## Run / test

```bash
uvicorn app.main:app --port 8080            # local
pytest -q                                    # tests
docker build -t northstar-triage . && \
  docker run --rm -p 8080:8080 --env-file .env northstar-triage
node tests/celeco-visible-runner.mjs http://localhost:8080
```

Config from environment variables only; `.env` is local dev and gitignored. Keys are passed at
`docker run` time, never baked into the image.
