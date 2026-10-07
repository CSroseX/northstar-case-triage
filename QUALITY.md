# Quality: what was tested, what was wrong, and what is still missing

This is the honest history, not a summary of the final state. Most of what is worth knowing
about this service is in the things that were wrong at some point and how they were found.

## Where it ended up

| Check | Result |
|---|---|
| Unit and integration tests (`pytest -q`) | **210 passed**, ~10s, no network, no model calls |
| Provided visible runner | **8/8** |
| Offline walkthrough (`scripts/demo.py`) | **12/12** |
| Independent stress suite, run 2 | **46/49** main phase, 1 genuine defect (since fixed) |
| Model budget | **~115 of 300** |

## The visible runner: 6/8 → 7/8 → 8/8

The runner score moved three times, and the middle step is the interesting one.

**6/8.** The first end-to-end run with the model wired in. Two failures, both from the
model misreading rather than the rules:

- A planned-service request was read as a breakdown because the sender asked for a
  particular kind of engineer, so it tried to dispatch.
- A coverage question was read as a fault report.

Both were fixed in the system prompt by making the instruction general — decide intent
from the *state of the equipment*, not from what the sender asks for — rather than by
adding wording that matched the specific test cases. Tuning to the eight visible cases
would have scored better and taught the service nothing.

**7/8, and then 6/8 again on a re-run with no code change.** VIS-005 is a follow-up email
about a service request raised minutes earlier from the portal. On one run the model
answered `sameFaultAsExisting: "unclear"`, on another `"different"` — same prompt, same
case. That instability was the useful finding: a decision that depends on which way the
model leans on a borderline reading is not a decision, it is a coin flip. The fix was to
let the *records* settle it: an earlier request for the same equipment inside the
contract's response window is one incident reported twice. The model's opinion stopped
being load-bearing.

**8/8.** With the window rule in place and the prompt generalised.

VIS-005 then regressed a fourth time, much later, during the stress-defect fixes — and for
a different reason worth recording. Tightening `referencedRequests` to only count ids the
customer actually wrote had a side effect: a guard meant to stop open *work orders* making
an admin request a duplicate was also blocking a service request from matching an earlier
service *request*, which is exactly the case it was for. Separately, the instruction for
`sameFaultAsExisting` was written around faults and symptoms, and VIS-005 describes no
fault at all — it is the same *work* requested twice, so the model answered "different",
correctly by the letter of the instruction. Both were fixed and the run returned to 8/8
with fresh model calls.

The evidence for this sequence is kept: `exploration/run-7of8-vis005-unclear.json` is the
actual 7/8 run.

## Bugs that hand testing caught

These were found by reading real output against a running container, not by the test suite.
Several were passing their tests at the time.

**A duplicate linked every open job on the asset.** The reply cited whichever readable
`WO-` reference came first, which could be an unrelated job. The customer would be told
we had attached their report to work that was not theirs. Worse: **the bug was baked into a
passing test** that asserted the wrong work order as correct. Fixing the code meant fixing
the test's expectation too.

**A database UUID reached a customer reply.** Work orders created through the API come back
with a UUID and no `WO-` reference. The draft quoted it. Now only references a customer
would recognise on their paperwork are shown.

**Mojibake in the reply.** An em dash in the issue line rendered as garbage in a Windows
terminal and in mail clients. Replaced with a plain hyphen, and responses now send an
explicit `charset=utf-8`.

**`safetyRisk` derived from the status.** "Please review the June visit" reached
`human_escalation_required` for a non-safety reason and was flagged as an immediate hazard.
Now only genuine safety reason codes set the flag.

**A predicate that passed its tests and never fired live.** The "this isn't a service
request" check required an empty `symptomSummary`. In tests, hand-written facts had one
empty, so it passed. The real model writes a summary for anything — including "request for
contact number" — so live it never matched. Caught only by re-testing the original phrasing
against the running container instead of trusting the green suite. The test was then
strengthened to use a realistic non-empty summary so it cannot pass for the wrong reason.

**Docker `--env-file` silently set nothing.** The supplied credentials file uses
`Key: value`; `--env-file` requires `KEY=value` and ignores the rest. The service started
healthy and took the cautious path on everything. Caught because `/health` reports whether
each credential is present.

**The test suite was making live API and model calls.** The contract test ran against the
real service: 96 seconds and real budget per run. Stubbed to saved responses, now 0.96s.
A related mock was routing the work-order POST to the model because it matched on
`request.method == "POST"`.

**`TraceLog` crashed on an unusable log path.** Only `OSError` was caught; a null byte in
the path raises `ValueError`. That would have killed the service at startup, for logging —
which is never allowed to affect a response.

## The stress suite

A second AI session acted as an independent black-box tester. It read only the client PDFs,
the two schemas, `notes.txt` and the decision list — it never opened `app/` — and built 50
cases from the policies. It lives in `tests/stress/` with its own README, is not part of
`pytest`, and makes real model calls.

**Run 1 (49 deliveries, 43 model calls): 21 passed as first measured.** The tester then
re-scored its own failures and classified 12 of 28 as bugs in its harness or its
expectations rather than service defects, leaving 15 genuine. It grouped them into four
root causes:

- duplicate matching keyed on customer + asset instead of the fault
- attachment summaries not read at all
- coverage confirmed without checking the agreement's customer, mode or dates
- identifiers matched by exact string only

That re-scoring is the tester grading its own homework, so I treated its pass counts as
softer evidence than the traces and verified each finding against the recorded
`decisionTrace` and the code before fixing anything. Two of the 15 turned out to be rule
decisions for me rather than defects, and two were requirements that had never been
written down — which is why CLAUDE.md gained the shock/exposed-wiring extension and the
remote-only clarification.

The worst single finding was **C-08**: a dialysis centre on a remote-only agreement asked
us *not* to send anyone, and the service booked a technician and told them an engineer was
coming. The remote-only check was gated on the model's reading of whether the customer
wanted someone on site, so "please do not send anyone" cleared the gate.

**Run 2 (49 deliveries + 3 restart legs, 45 model calls): 46 passed in the main phase, 11
of the 15 defects confirmed fixed, 0 still failing from run 1.** Of the three remaining
main-phase failures, the tester attributed two to its own expectations and one (`I-04`) to
the known identity limit below.

Run 2 added a restart leg that run 1 had skipped, and found **one genuine new defect
(`X-13`)** with a real container restart: the in-memory event store was lost, so a
redelivered event was triaged from scratch and came back `human_escalation_required` where
the customer had already been told `dispatch_ready`. The write itself was always safe —
Northstar keys on `externalEventId` — but the *answer* changed, which is the worse failure.

## The restart fix

An unrecognised event id is now checked against Northstar's work orders first, filtered for
that event id in our own code since `q=` is ignored on that route. A match returns
`dispatch_ready` for that existing work order, with a warning that it was recovered, no
model call and no re-decision. A miss or a failed read falls through to normal triage, so
recovery is an optimisation and never a gate.

Verified with a real container restart: dispatched a request, killed and restarted the
container, confirmed `/cases/recent` showed an empty store, redelivered the same event id,
and got the same work order id back with `modelTraceIds: []`. Northstar showed exactly one
work order for that event.

Wiring it exposed a latent bug underneath: `_normalise_work_order` never produced
`externalEventId`, so booking's existing "reconcile by request **or event** reference" step
— the documented 504 recovery path — could only ever match on `requestId`. The offline test
stub had the same blind spot in reverse, returning a camelCase field the real route does
not use, so it would have made the new test pass while the live service failed.

## The lookup timing fix

Found by reading the code rather than by a failing test, which is why it is worth
recording: nothing in the suite or the runner was red.

The six Northstar reads ran one after another, each with three attempts at a 20s timeout.
That is roughly **365 seconds** in the worst case, against the 120s the provided runner
allows for a whole case. The consequence was specific and bad: because the reads happened
before the decision, a Northstar that was timing out could hold a **hazard** for minutes
before the safety check ran at all. POL-SAFETY-001 says an escalation must not wait on a
record, and this was a path where it would have.

Nothing was observably broken. The sandbox answers quickly, so every test and every runner
pass had been measuring a healthy dependency. The failure needed Northstar to be slow,
which it never was while we were watching.

The identity read still goes first, since it resolves the asset and the other reads have no
subject without it. The remaining five now run **concurrently under one 15s wall-clock
budget**, and the identity read carries the same budget on its own — without that, a hang
on just that one read would still have held the case open before anything else started.
Worst case is now about **30 seconds**. Anything not back when the budget expires becomes a
failed lookup, and the case is decided on what did arrive: safety escalates immediately,
and every other outcome surfaces as a visible failure for a coordinator instead of a silent
default.

The read timeout dropped to 5s, which needed a new setting rather than a smaller number.
The single `REQUEST_TIMEOUT_SECONDS` also covered the model call, which legitimately takes
5–7 seconds, so lowering it globally would have failed every model call. Northstar reads
now use `NORTHSTAR_TIMEOUT_SECONDS`, applied per request so it holds even for the shared
HTTP client the endpoint passes in.

**The test that proves it.** `test_a_hazard_escalates_even_when_northstar_hangs` points
every single route — including identity — at a transport that never answers, and asserts
the hazard still reaches `human_escalation_required` inside the budget with the customer's
own words preserved. Two more go with it: one that checks the budget expiring keeps the
identity it had already resolved and reports all five outstanding reads as failed, and one
that pins the concurrency itself. That last one matters for the future — reverted to
sequential awaits it fails with *"suggests the reads ran sequentially"*, so the property
cannot quietly regress.

Measured after the change: 8/8 on the runner in 53 seconds, and 7–9s per case live, nearly
all of which is the model call. Evidence gathering is now about 1.5–2s.

## Known limits

**Identity by sender is not attempted.** Customers have no email field in the data, and
matching sender domains to company names is too unreliable to act on. Identity resolves
asset → customer; a request naming no equipment is asked for it. This is the one run-1
finding (`I-04`) left unfixed on purpose: a shaky heuristic here would make confident wrong
identifications, which is worse than asking.

**In-process state resets on restart.** The event-id store and the handled-request store
are in memory. The restart recovery closes the dangerous half; what remains is that
duplicate detection against requests handled before a restart is lost until they surface in
Northstar's own records. A submitted runtime would persist both.

**The decision log is in memory and in a file inside the container.** `GET /cases/recent`
is lost on restart. Acceptable for a coordinator looking at the last few requests;
acknowledged rather than solved.

**Silence cannot be detected.** A request-driven service cannot see that a safety question
went unanswered. Chasing one needs a timer outside this service.

**Planned work names no date.** No scheduling-lead-time data exists, so the service
commits to no slot and a coordinator supplies it (D2).

**Same-city dispatch is stricter than Northstar's practice.** D1 is my substitute for
reachability-within-the-window, which needs travel data the API does not expose. It will
refuse dispatches a human would allow.

**A tight evidence budget trades completeness for promptness.** Under a slow Northstar the
service now decides on partial records rather than waiting. That is the right trade for
safety, but it means a case can reach `failed` or `account_review_required` where a patient
read would have resolved it. The failed lookups are named in the response and the trace so
a coordinator can see exactly what was missing and retry.

**Model instability is bounded, not eliminated.** The records settle duplicates now, but
intent and the safety signal still rest on one model call at `temperature: 0` with no
retry. The cautious paths catch a missing or malformed answer; they cannot catch a
confidently wrong one. The keyword backstop is the second line for hazards specifically.

**The stress suite's regex checks are blunt by its author's own admission.**
`draftExcludesAdvice` and `draftNoPromisedDate` catch obvious violations and will miss a
politely phrased instruction or an implied date. A clean pass there is necessary, not
sufficient — 30 of its cases are flagged for human review for exactly this reason.

**Test coverage is uneven.** The decision rules, booking and the trace are well covered.
Reply wording is covered for shape and for the specific mistakes found so far, but "does
this read well to a customer" is not something the suite can assert.
