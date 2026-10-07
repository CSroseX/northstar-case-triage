# AI work log

Two AI sessions were used for this project. Both are exported in full under
`agent-sessions/` — complete transcripts, not summaries, so the actual prompts, the tool
calls, the mistakes and the corrections can all be read directly.

**Redaction:** the only things removed are the live Northstar token, the live model gateway
key, and my personal email address. Each is replaced with a visible marker
(`[REDACTED_NORTHSTAR_TOKEN]`, `[REDACTED_MODEL_GATEWAY_KEY]`, `[REDACTED_EMAIL]`).
Deliberately invalid test credentials (`cel_northstar_INVALID_FOR_TESTING_...`) are left
intact, because a reader needs to see that the bad-key failure cases were exercised with
obviously fake keys. Nothing else was altered, shortened or tidied.

## Index

| # | File | Tool | Model | Records | Period (UTC) |
|---|---|---|---|---|---|
| 1 | [`agent-sessions/01-builder-claude-code.jsonl`](agent-sessions/01-builder-claude-code.jsonl) | Claude Code 2.1.289 (VS Code extension) | `claude-opus-5` | 5,485 | 2026-10-06 07:20 → 2026-10-07 08:28 |
| 2 | [`agent-sessions/02-tester-claude-code.jsonl`](agent-sessions/02-tester-claude-code.jsonl) | Claude Code 2.1.289 (VS Code extension) | `claude-opus-5` (222 msgs) + `claude-sonnet-5` (94 msgs) | 934 | 2026-10-06 20:55 → 2026-10-07 08:27 |

### 1. Builder session — `01-builder-claude-code.jsonl`

**Tool:** Claude Code 2.1.289, VS Code extension. **Model:** `claude-opus-5` throughout.
**Size:** 13.7 MB, 5,485 records, ~42 instructed turns.

**Why this session was needed.** This is the session that built the service. I ran it as a
sequence of small, separately approved steps rather than one "build me a service" prompt,
because I wanted to review each layer before the next one depended on it. Every step ended
without a commit until I had read the result.

The order it ran in:

1. **Read-only exploration** of the Northstar APIs — one script calling each read endpoint
   once, saving raw responses to `exploration/raw/`, plus exactly one model call to confirm
   the gateway contract. No POSTs, and no service code yet. This produced
   `exploration/FINDINGS.md` and settled several questions the PDFs left open (there are no
   agreement "families"; `q=` is ignored on two routes; identity has to resolve
   asset → customer because customers have no email field).
2. **Walking skeleton** — FastAPI, the two Pydantic schema models, a Dockerfile with keys
   supplied at run time, and a contract test.
3. **Doc updates** after Rohan's email and two further policy PDFs arrived, including
   checking whether the new material contradicted anything already written down.
4. **Evidence lookups** — one function, asset id in, facts and gaps out, deciding nothing.
5. **Decision logic** — the ordered policy checks, with no model involved, so the rules
   could be tested without spending budget.
6. **An offline walkthrough** (`scripts/demo.py`) so the whole thing could be demonstrated
   with no credentials and no network.
7. **Wiring the model in** — one call per case. This is where the runner first ran
   end-to-end at 6/8, and where the prompt was generalised rather than tuned to the visible
   cases.
8. **Work-order booking, customer replies, the previous-work rule and safety answers.**
9. **The response-window duplicate rule**, replacing an invented 12-hour interval.
10. **Bug fixes from my own hand testing** against a running container.
11. **Logging** — `decisionTrace` on every response, a log file, and `GET /cases/recent`.
12. **Fixing the defects the tester session found**, then the restart recovery.

**Sub-agents.** I asked for a sub-agent twice inside this session, both times because I
wanted work done without the main thread's assumptions carried into it: once when I was
uncomfortable with keyword-based safety detection and wanted it reworked, and once to
re-run a set of cases with a cleared model cache and report what the model actually
returned. The transcript contains the instructions given to each and the results that came
back, but Claude Code does not export a sub-agent's own internal turns, so those two are
visible only through their briefs and their reported findings.

**Worth reading for:** the 6/8 → 8/8 sequence and the reasoning about VIS-005's unstable
model answer; the moment a predicate that passed its tests turned out never to fire live;
and the point where I challenged a claim about which earlier request the window rule had
matched and it turned out to be wrong.

### 2. Tester session — `02-tester-claude-code.jsonl`

**Tool:** Claude Code 2.1.289, VS Code extension. **Models:** `claude-opus-5` and
`claude-sonnet-5` (mixed in the main thread). **Size:** 3.3 MB, 934 records, ~10 instructed
turns.

**Why this session was needed.** The builder session could not test its own work
honestly — it knew where the shortcuts were, and anything it wrote as a test would share
its blind spots. So I started a separate session with an explicit instruction to act as an
independent black-box tester: derive expectations from the client PDFs, the two schemas,
`notes.txt` and my decision list, and **never open `app/`**. It built 50 cases from the
policies alone and only ever talked to the running service over HTTP.

What it produced is in `tests/stress/` — the case list with the source for each
expectation, a runner with a model-call ceiling, and two full result sets.

**Run 1** found 15 genuine defects across four root causes, including the worst single bug
in the project: a dialysis centre on a remote-only agreement asked us not to send anyone,
and the service booked a technician anyway.

**Run 2**, after the fixes, passed 46 of 49 in the main phase with none of run 1's failures
remaining, and added a restart leg that run 1 had skipped. That leg found the last genuine
defect — a redelivered event being re-decided after a container restart and coming back
with a different status than the customer had already been given.

**The tester corrected its own expectations after seeing results.** This matters for how
much weight to give its numbers. In run 1 it re-scored its own failures and reclassified 12
of 28 as bugs in its harness or its expectations rather than service defects; in run 2 it
marked four more that way, including one restart case where its expectation had been too
narrow. Some of those corrections were clearly right — it had been asserting on a field
Northstar does not echo, and flagging a customer's own quoted "2am" as a promised
appointment. But it is still a tester grading its own homework, so I treated the recorded
`decisionTrace` for each case as the evidence and verified every finding against the code
before changing anything. Two of its findings turned out to be rule decisions for me rather
than defects, and two were requirements that had never been written down anywhere.

**Worth reading for:** how the expectations were derived from the policies before any code
was run, and the re-scoring passes where it argues with its own earlier verdicts.

## Model budget

Roughly **115 of the 300** permitted gateway requests. The two stress runs account for 88
of those (43 + 45). The rest went on the visible runner — re-run whenever the system prompt
changed, since cached answers would have been invalid — plus a small number of single-call
probes to check what the model actually returned for a specific message. A development
cache (off by default, behind `MODEL_CACHE_DIR`) kept repeated local runs from re-billing
identical prompts.

## Decisions I made myself

1. Made the model only read the message and let code make the decision, so every outcome can be explained, tested and traced back to a policy.
2. limited the system to one model call per request, because the budget was 300 calls and had to cover Celeco's evaluation as well as my testing. Perhaps better approach would be to not limit the model calls for clarification - implemente loops, decision nodes and other complex architectures. 
3. I decided that an unclear safety signal gets one plain question and that the model can never clear a hazard on its own, because Meera described a pilot that nearly dispatched on a possible fuel smell.
4. "not sure" fallback answers to the safety question to a person, because Meera said those must never drift back into normal handling.
5. let the system move to the next qualified technician if the first one becomes busy just before booking, so a valid dispatch doesn't stall on a timing change.
6. Duplicate requests are flagged duplicated only from the FIRST original request rather than LAST request. Ex: window is 4 hours -> 1st req arrives at 9 am, 2nd at  12 noon -> 2nd req is duplicate. But if 3rd req arrives at 3 pm (out of 4 hour window) then not duplicate. 
7. 