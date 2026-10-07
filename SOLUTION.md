# Solution

## How I framed the problem

The brief could be read as "triage requests faster". I read it as something narrower and
harder: **get to the correct first action**. Those are not the same target, and optimising
for the first one damages the second.

A wrong first action is not a small error that a later step tidies up. It is a message
already sent to a customer. If we reply "an engineer has been assigned" and no work order
exists, the customer stops worrying about a problem nobody is working on. If we book a
routine service against a report that mentions a gas smell, we have filed a hazard as
paperwork. Both are worse than taking an extra few seconds, and both are worse than saying
"a coordinator is looking at this".

So the service is built around three commitments:

1. **A hazard outranks everything**, including resolving who the customer is.
2. **Uncertainty is an outcome, not a failure.** Eight of the nine statuses in the schema
   are ways of being useful without dispatching. Reaching for a human is a correct answer.
3. **Never claim something the records do not support.** No technician is "confirmed"
   before the work-order write is reconciled; no response window is quoted from an
   agreement that was not in force.

I deliberately did not chase automation rate. The measure I used instead was: would a
coordinator on a busy shift trust this and be able to act on it without re-reading the
original message? That is why every response carries a `decisionTrace` naming each check
and the policy behind it.

One structural decision follows from all of this, and it is the most important one in the
codebase:

> **The model reads. The code decides.**

The model is given the message and the records for one asset, and asked only to report
facts: which equipment is mentioned, whether a hazard is signalled and in whose exact
words, what the intent is, whether this looks like a repeat. It never chooses a status.
Every status comes from ordered policy checks in `app/decide.py`, which a reviewer can
read against the client's PDFs. This keeps the behaviour explainable, makes the rules
testable without spending model budget, and means a model misreading degrades one fact
rather than inventing an outcome.

## The flow

```
intake → read → evidence → decide → act → respond
```

1. **intake** — validate against `case-input.schema.json`. A known `X-Event-ID` returns
   the stored result. An unknown one is first checked against the work-order record, in
   case a restart lost the in-memory store.
2. **read** — exact identifiers (`AST-1202`) are pulled out by regex, not the model: a
   fixed pattern is cheaper and more reliable than a model call. Then **one** model call
   extracts the language-level facts.
3. **evidence** — asset → customer → site → agreement in force at `receivedAt` →
   qualified and available technicians → open work orders and recent requests for that
   asset. Reports what it found and what it could not; decides nothing.
4. **decide** — ordered checks, first match wins:
   safety → lookup failure → identity → coverage → previous work → duplicate →
   planned vs breakdown → technician.
5. **act** — only `dispatch_ready` writes anything. Availability is re-checked
   immediately before the write, and the write is keyed on the event id.
6. **respond** — the structured result, a customer-ready draft chosen by status, the
   audit, and the decision trace.

### Why the checks are in that order

The order *is* the policy, so it is worth justifying each position.

**Safety first, before even the lookups.** POL-SAFETY-001 says not to wait on resolving
customer/site/asset before escalating. So a hazard signal escalates even when Northstar is
unreachable. There is no sequence of record failures that can bury a hazard.

**Lookup failure before identity.** If we could not read the records, we do not know
whether the asset exists. Saying "we don't recognise that equipment" would be a claim we
cannot support; `failed` is honest.

**Coverage and previous work before duplicates.** This order was wrong initially and the
stress suite caught it. A lapsed agreement linked to an existing job disappears behind
"we've linked your message to it" — an acknowledgement that implies cover we cannot
evidence. And a customer who writes "I'm not sure whether this is the same fault coming
back" has asked a question only a human can answer; auto-linking answers it for them, in
the wrong direction. An account question and an explicit "I can't tell" both outrank
housekeeping.

**Planned vs breakdown before technician selection.** OPS-DISPATCH-004 is explicit that
planned work is not dispatched at intake. Only a breakdown can reach `dispatch_ready`.

## Design decisions, and why

### Safety

**The model can never clear a safety signal.** If the model reports "denied" — the
customer ruled a hazard out — that only stands when the quoted words are actually in the
message. A model cannot clear a hazard with words nobody wrote. If the model output is
missing, malformed or timed out, the signal degrades to ambiguous, never to absent.

*Trade-off:* this produces false escalations on messages like "the alarm is sounding but
there's no smoke", where a keyword backstop sees "smoke". I accepted that. The asymmetry
is not close: a needless escalation costs a coordinator a minute, a missed hazard costs
something else entirely.

**An ambiguous hazard gets one plain question, and nothing progresses.** Meera's
constraint was that the question be answerable by whoever is standing next to the
equipment, not a form. It is one sentence covering all four triggers. "Not sure" or no
reply stays with a human and never drifts back to normal triage.

**Shock, electrocution and exposed wiring count as hazards.** These are *not* in
POL-SAFETY-001's list, which names fuel/gas, smoke, water near electrical equipment and
anyone unwell. I added them as a deliberate extension, commented as mine in the code and
written into CLAUDE.md, because the cost of treating live electrical exposure as routine is
not symmetric with holding a case. Flagged for Rohan rather than hidden.

**The customer's exact words are preserved**, never a generic label, and they appear in the
reply and the trace. POL-SAFETY-001 requires the reporter's own wording on the record.

**Attachment summaries are part of the message.** A hazard is often only in the photo
description while the body says "nothing urgent". They feed both the model prompt and the
keyword backstop. They deliberately do *not* feed identity: a summary is written by
whoever processed the file and may name other equipment in passing, which must not
redirect the case.

### Coverage

**Coverage comes from agreement records only.** Customer claims, payment receipts and
account-manager assurances are context, never evidence. One stress case had a customer
saying finance had paid, with a bank transfer attached — and the attachment itself said it
did not confirm Northstar had received or allocated the payment. That is account review,
not cover.

**Where the asset's own coverage and the agreement's `serviceMode` disagree, the stricter
one controls.** The data allows them to conflict and nothing says which wins, so I chose
the cautious reading.

**Remote-only never produces a work order, however the request is worded.** This started
as a coverage *fault* gated on whether the customer seemed to want someone on site — which
meant a customer who wrote "please do not send anyone" cleared the gate, and the breakdown
branch dispatched a technician against a remote-only contract. A contract limit does not
depend on the customer's phrasing. It is now read from the record alone, and the wording
only picks which of two permitted outcomes applies: remote help wanted is `covered_action`
(nothing is wrong with the cover, so there is nothing for an account reviewer to decide);
onsite genuinely needed is `account_review_required`.

**A response commitment from an agreement that was not in force is withheld**, with a
reason, rather than reported as a number a coordinator might quote.

### Duplicates

**Duplicates turn on the fault, not the asset.** Equipment can develop a second, unrelated
fault while a job is open. Being the same machine does not make it the same fault.

**The response window decides a repeat, not an invented interval.** A follow-up arriving
while we are still inside the window we promised is the same incident chased up, so it is
linked. One arriving after the window has passed is a different conversation and goes to a
coordinator. I first used a fixed 12 hours; using the contract's own number is defensible
from the record instead of made up.

**The window is measured from the earliest related request, not the nearest.** Measuring
from the nearest would restart the clock on every chase-up, so a customer emailing every
couple of hours would stay inside the window forever and never reach a human — the
opposite of what the escalation is for.

**A duplicate links only to what it actually matched.** Other open jobs on the same
equipment go into the audit as context, never as the linked reference. Citing the wrong job
tells the customer we have attached them to work that is not theirs.

**A referenced request id only counts if the customer wrote it.** The model is shown the
asset's open jobs and recent requests, and was returning those ids as things "the message
points to" when the message pointed at nothing. Same principle as verifying a safety
quote: the model reports, the text decides.

**Unclear is a human's decision**, never a guess in either direction.

### Dispatch

**D1 — breakdowns use same-city technicians only.** Qualified, available, and
`technician.city == site.city`; none leaves `resource_escalation_required`. Rohan's actual
practice is softer — can they reach it inside the response window, with travel as a
tie-breaker — but that needs travel-time and route data the API does not expose. Guessing
at reachability would be inventing the key fact. **This is my decision, not Rohan's, and
should be revisited with him.** Not applied to planned work, where a route can be planned
around travel.

**D2 — planned acknowledgements promise no date.** Name the service and the asset, say it
is being scheduled against the contract, commit to no slot. Rohan would like a date by
which we would propose a slot; we have no scheduling-lead-time data, so the coordinator
supplies it rather than the service inventing it.

**A surfaced technician is a suggestion until the write is reconciled.** Availability is
operational, not static — someone can take another emergency between triage and write. We
re-check immediately before writing, replace them if they are gone, and escalate if nobody
qualified is left. We never write a placeholder against an unavailable technician, and
never substitute someone closer who lacks the skill or certification.

### Writes and repeats

There are three different kinds of "we've seen this before", and conflating them is how a
job gets created twice:

| Kind | Handling |
|---|---|
| Same `X-Event-ID` redelivered | Transport repeat: return the stored result |
| Same fault, different request | `duplicate_detected`, linked to the existing job |
| Different fault, same asset | Its own work order |

**The write is keyed on `X-Event-ID` as `externalEventId`**, so a redelivered event cannot
create a second job, and `duplicate: true` from Northstar is a success, not an error.

**A timeout after a write leaves the outcome uncertain**, so work orders are read back by
request and event reference before any retry, and the retry reuses the same id. If the
outcome still cannot be established the case fails visibly rather than risking a second
job.

**After a restart, an unrecognised event is checked against the work-order record before
being triaged.** The in-memory store is lost on restart; the records are not. Re-deciding
would spend a model call and could reach a *different* status than the one the customer was
already given. Recovery is an optimisation, never a gate: if the read fails or finds
nothing, the case is triaged normally.

**Work orders we create carry a summary.** Northstar's own jobs do, and without one the
model had a blank line to compare a repeat against.

**Requests we have handled are remembered per asset.** Requests delivered to *us* never
appear in Northstar's queue, so without this each repeat of a fault saw "no earlier
requests" and became another work order.

### Identifiers

**Matching is tolerant of spelling.** Customers copy ids off a sticker (`AST 1202`), out
of a portal (`ast-1202`) or from memory. All normalise to `AST-1202`. A hyphen or
underscore is an explicit identifier whatever the case; a space or no separator needs a
known equipment prefix, so "Unit 4 stopped" and "AHU 2" stay prose — otherwise a bogus
first match would hijack the whole lookup.

**The model's asset id is a fallback**, used only when that id genuinely appears in the
message, so it cannot invent equipment or pull one from the records it was shown.

### Secrets and the model gateway

Keys live in environment variables only — never in source, logs, model prompts, the trace
or any written file. The config object's `repr` is `Secret(***redacted***)` so it cannot
leak through an exception or a debug print. The token goes in a header, never a query
string. `/health` reports whether each credential is present, never its value. The two
credentials are distinct and not interchangeable.

**One model call per case**, `max_tokens` kept tight because output bills at `max_tokens`
rather than actual usage, `temperature: 0`, and a JSON object response. **No retries by
design**: a model failure takes the cautious path instead, because retrying a timed-out
call doubles the spend for a fact the policy can do without. A dev cache exists behind an
env flag, off by default, so repeated local runs do not re-bill identical prompts; it is
keyed on the user prompt only, so editing the system prompt does not silently reuse stale
answers.

## Trade-offs I accepted

**Cautious over automated.** The service escalates in cases a human would wave through —
"no smoke, just the alarm" can still produce a safety question. Deliberate, per the
asymmetry above, and Meera confirmed she wants it kept even at the cost of automation rate.

**One model call per case, no retries.** Cheaper and more predictable, and the policy
degrades to a cautious path when the model is unavailable. Costs some accuracy on genuinely
ambiguous messages where a second opinion might help.

**Regex for identifiers, model for language.** Fast and deterministic where the pattern is
fixed, but it needed real care: loosening the pattern to accept `AST 1202` immediately
started reading "Unit 4" as equipment, which the known-prefix rule now prevents.

**In-process state.** The event-id store and the handled-request store are in memory and
reset on restart. The restart recovery closes the dangerous half of this — a redelivered
event will not be re-decided or double-written. What is lost is duplicate detection against
requests handled before the restart, until they appear in Northstar's own records. A
submitted runtime would persist both.

**Statuses over a confidence score.** The schema has a `confidence` field and it is
populated, but nothing branches on it. A threshold would be a number I invented; the
ordered checks are auditable against the policies instead.

**`processing` is never emitted.** The schema allows it, but the service is synchronous and
always reaches a terminal decision within the request. Returning `processing` would imply
background work that does not exist.

**Trusting the records over the customer.** Payment receipts and account-manager
assurances do not establish cover. This will occasionally hold a case for a customer who
really has paid. POL-CONTRACT-002 is explicit, and account review is the designed path for
exactly that.

## Open questions for Northstar

- **Scheduling lead time** (Rohan). Needed before a planned acknowledgement can name a
  date; D2 exists because we do not have it.
- **D1's same-city rule** (Rohan). My cautious substitute for reachability; revisit if
  travel-time data becomes available.
- **Matching sender domains to customers.** The data has no email field on customers, and
  matching domains to company names is too shaky to rely on. Identity resolves asset →
  customer instead; a request naming no equipment is asked for one. Left as a known limit.
- **No reply to a safety question.** A request-driven service cannot see silence. Chasing
  an unanswered safety question needs a timer somewhere outside this service.
