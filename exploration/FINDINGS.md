# Exploration findings

Read-only pass over the six Northstar GET routes + one model-gateway probe.
No POST to Northstar. Model budget used: 1 / 300.
Raw responses in `raw/`. Re-run with `node exploration/explore.mjs`.

## Record shapes

**customer** (13, `?route=customers`) — nested, one call gets site + asset:
`{id, name, aliases[], status, sites[{id, customerId, name, city, assets[{id, siteId, type,
nickname, coverage, contractRef, requiredCertifications[]}]}]}`
`status`: active ×12, on_hold ×1 (CUS-074). **No email or domain field anywhere.**

**agreement** (13, `?route=agreements`) — flat:
`{id, customerId, status, serviceMode, responseHours, effectiveFrom, effectiveTo, supersedes?, note?}`
`status`: active ×12, suspended ×1. `serviceMode`: onsite ×12, remote_only ×1.
`responseHours`: 2, 4, 6, 8, 12.

**technician** (13) — `{id, name, city, skills[], certifications[], available, currentAssignment?}`
Cities: Bengaluru ×11, Mysuru ×1, Tumakuru ×1. 8 available / 5 not.

**request** (18, `?route=requests`) — `{id, requestId, receivedAt, channel, priority, sender{name,email},
subject, body, attachments[{id,name,type,summary}], workflow}`
Two fields the POST input schema does NOT carry: `priority` (low/normal/high/urgent) and
`attachments[].summary` (pre-summarised text — no OCR needed). `workflow` is `null` on all 18.

**work order, active** (4, `?route=work-orders`) — **snake_case, unlike every other route**:
`{id, request_id, customer_id, site_id, asset_id, technician_id, status, summary, created_at, source}`

**work-order history** (24) — camelCase:
`{id, requestId, customerId, siteId, assetId, category, summary, status, openedAt, closedAt}`
`status`: completed ×21, closed_after_review ×2, remote_resolution ×1.

## Answers to "open / to verify"

**1. covered_action vs dispatch_ready** — data supports your reading, with a sharper line.
The two `covered_action` runner cases (VIS-001 scheduled quarterly service, VIS-008 coverage
confirmation) are both *planned/administrative* work; `dispatch_ready` (VIS-006) is a
*breakdown* — "has stopped cooling". Discriminator is breakdown-vs-planned, not coverage.
Note VIS-001 is covered, has an available tech (TECH-01/08) and still expects `covered_action`
— so **availability alone must not promote planned work to dispatch**.

**2. How a business duplicate is found** — not via saved workflow state. `workflow` is `null`
on all 18 seeded requests, including VIS-001 which VIS-005 duplicates. So the only evidence
is: (a) `?route=requests` (full list) matched on asset + time proximity + body cross-reference,
and/or (b) the pre-existing open WO. Note the runner POSTs cases in order, so VIS-001 is
processed before VIS-005, but it arrives as a POST — your own store is the reliable record.
**`q=` is ignored on this route** (see traps), so filter client-side.

**3. Writing to request-workflow** — the API ref documents the workflow record and all 18 are
`null`, i.e. nothing has populated them. It is a documented POST and plausibly an observed
side effect, but it is a write and you said no POSTs yet. Fields line up with the case result
(`safetyOutcome`, `coverageOutcome`, `coordinatorNotes`, `customerReplyDraft`). Flagged for
your decision — untested.

**4. What CON-012 base says, does an A1 exist** — **neither exists.** See below; this is the
biggest correction to the notes.

**5. submission.json manifest schema** — not found. Not in the resources, not served by any
read route. Still open; likely in the workspace UI.

## Corrections / additions to the notes

**Agreement "families" do not exist in this data.** Exactly 13 agreements, one per customer,
no base contracts and no intermediate amendments. `q=CON-012` returns only `CON-012-A2`;
`q=CON-129` returns only `CON-129-A1`. CON-044-**A3** exists with no A1/A2 and no `supersedes`.
`supersedes` appears on only 2 of 13 records (CON-012-A2 → CON-012, CON-129-A1 → CON-129) and
points at a record that is **not retrievable**.
Consequence: there is no base-vs-amendment conflict to resolve at runtime — the one in-force
record per customer already carries the final `responseHours` and `serviceMode`. Build
"latest in force by date + status" as a general selector over whatever the family returns
(correct per policy, handles hidden cases), but do **not** make a resolvable base contract a
precondition — a missing `supersedes` target is normal here, not a reason for account review.
VIS-008 ("use the amendment, not the base contract") is satisfied by citing CON-012-A2 and its
4-hour window.

**Identity cannot be resolved by sender domain.** Customer records have no email field, and
`q=` on customers matches **name and aliases only** — `asterwarehousing.in`, `Whitefield`,
`AST-101` and `Main DG` all return 0 results. The Aster collision is real (CUS-012 Aster
Warehousing, CUS-019 Aster Clinical Services, both Bengaluru/Whitefield). In practice the
reliable path is **asset ID → customer**, by fetching customers and indexing assets locally;
asset IDs are globally unique across the dataset. Domain is a corroborating signal at best,
and you cannot verify it against any record.

**`q=` is silently ignored on `requests` and `work-orders`.** Both return the complete list
for any query (18 and 4 respectively) and return HTTP 200 — no error, no empty set. Only
`customers`, `agreements` and `work-order-history` actually filter. Anything that assumes
server-side filtering on those two routes will quietly operate on every record.

**Coverage is carried at asset level too, and can disagree with the agreement.**
`asset.coverage` ∈ premium / standard / suspended / remote_only. Two assets where it matters:
- `AST-801` — `coverage: "suspended"`, under `CON-074` (`status: suspended`, expired
  2026-08-31, `note: "Renewal payment awaiting allocation."`). Matching request REQ-8268 cites
  a bank transfer advice. Both layers agree: account review. This is the payment-receipt trap,
  fully instrumented in the data.
- `AST-901` — `coverage: "remote_only"` under `CON-083-A1` (`serviceMode: "remote_only"`).
  Matching request REQ-8271 explicitly asks for "someone on site". Remote-only must not
  authorise onsite dispatch, so this is the service-mode trap.
Treat the stricter of the two layers as controlling.

**`CON-019` expires 2026-12-31 and `CON-074` already expired** (2026-08-31, before the
2026-09-20 request dates). Date arithmetic must run against `receivedAt`, as you planned.

**Resource escalation has a single clean discriminator.** Computing skill ⊇ asset.type and
certifications ⊇ asset.requiredCertifications across all 22 assets: **AST-205 is the only
asset with zero available qualified technicians** — it needs `["electrical","high-voltage"]`
+ generator, which only TECH-04 satisfies, and TECH-04 is unavailable (on WO-9285). AST-205 is
exactly the runner's VIS-007 case. Everything else has ≥1 available match. Also: TECH-07
(Mysuru) and TECH-12 (Tumakuru) are out-of-city — if you filter on city, AST-701/AST-1301 drop
to a single available tech (TECH-13).

**Pre-existing open work orders overlap the runner's cases.** `WO-9294` is open on AST-101
(created 09:20, `diagnosis_in_progress`) and `WO-9290` is open on AST-302 (`assigned`).
These are the assets in VIS-001/005 (AST-101) and VIS-006 (AST-302) — and VIS-006 is expected
to be `dispatch_ready` **despite an already-open WO on the same asset**. So "open WO on this
asset" is NOT sufficient for `duplicate_detected`; it would break VIS-006. The duplicate
signal has to come from the request referring to the same job (VIS-005 names Kavya's portal
request explicitly), not from asset overlap alone.

**The seeded `requests` store is the runner's dataset, not visible-cases.json.** Seeded
records are VIS-001…VIS-008 with `REQ-V00n` IDs, matching the IDs embedded in
`celeco-visible-runner.mjs`. `visible-cases.json` (DEMO-100n / Orion / Saffron / Vistara) is a
third, different set. Bodies in the store are also *longer* than the runner's POST payloads
(VIS-001 adds "Tuesday morning is preferred"), so don't match requests by exact body text.
Ten further seeded requests (REQ-8259…REQ-8290) cover scenarios the runner doesn't: repeat
faults, remote_only onsite ask, suspended account + payment, water near a UPS (REQ-8279 —
safety), prior-work references. Good free test material; hidden cases likely resemble these.

## Model gateway

One call, `response_format: {type:"json_object"}`, HTTP 200 in 2.8s. Returned clean parseable
JSON in `choices[0].message.content` (a string — needs `JSON.parse`), honoured the key names
given in the system message, and correctly returned `safetySignal: "affirmed"` with a quote.

Standard chat-completions shape plus a **`celeco` envelope**:
`{traceId, allowance:{requests, inputTokens, outputTokens}, warning}`.
`traceId` matches top-level `id` — use it for `audit.modelTraceIds`. `allowance` reports
*this request's* consumption, not the remaining balance; `outputTokens` billed at `max_tokens`
(400) rather than actual completion (68), so **keep `max_tokens` tight** — the 150k output cap
is the binding constraint, and 300 × 2048 would be 4× over. At ~400 reserved per call you have
room for ~375 calls, so the request count binds first. No rate-limit headers. `warning` is
null; worth watching as the budget drains.

## Open / untested

- `submission.json` schema — still not found.
- POST `?route=request-workflow` and POST `?route=work-orders` — untouched by design.
- 504 / timeout behaviour — not observed; all reads returned 200 in <1s. Retry and
  reconciliation paths will need fault injection rather than live observation.
- Whether `?route=requests` reflects cases POSTed to your service during a runner pass.
