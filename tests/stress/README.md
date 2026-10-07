# Stress suite — black-box triage testing

Independent black-box suite. Expectations come from `resources/*.pdf`, the two schemas,
`notes.txt` and Chitransh's decisions (N1–N12). `app/` was never opened, and **this suite never
changes anything under `app/`** — failures are reported, fixed in the build session.

| File | What it is |
|---|---|
| `cases-draft.md` | The reviewed case list: expectations, extra checks, the source for each, manual-review notes, and what is deliberately not covered. Read this first. |
| `cases.json` | The same cases as machine-readable data: payloads, expected statuses, typed checks. |
| `run-stress.mjs` | The runner. Sends cases, evaluates checks, writes one results file. |
| `results/` | Dry-run listings and run results. One file per run, named by run suffix. |

**50 cases · 52 HTTP deliveries · 44 expected model calls · hard ceiling 45.**

## Before a real run

1. **Container on the latest code with the model cache OFF.** The point is to exercise real
   model calls; a cache would replay old answers and invalidate most of the suite.
2. **`NORTHSTAR_ACCESS_TOKEN` exported in your shell** — the runner uses it read-only for the
   work-order snapshots. Without it, snapshots are skipped and work-order checks report
   "unverified" rather than passing or failing.
3. **Baseline sanity:** the four pre-seeded rows (WO-9281, WO-9285, WO-9290, WO-9294) should be
   present. D-07 and S-12 are skipped automatically if WO-9290 is no longer open.

## Running

```bash
# Dry run — prints the run order and totals, sends nothing
node tests/stress/run-stress.mjs --dry-run

# Main suite only (skips the restart leg)
node tests/stress/run-stress.mjs --phase main

# Everything, pausing for each restart
node tests/stress/run-stress.mjs

# Everything, with restarts automated via a hook that takes the phase name as $1
node tests/stress/run-stress.mjs --restart-cmd ./scripts/stress-restart.sh
```

Useful flags: `--base <url>` (default `http://localhost:8080`), `--model-ceiling <n>`
(default 45), `--run-suffix <s>` to pin event ids, `--out <path>`, `--timeout <ms>`, `-y`
to skip the interactive restart prompts.

## How it protects the run

- **Event-id suffixing.** Every `X-Event-ID` gets a run suffix (`stress-s01-20261006213742`),
  so a rerun is never answered from a previous run's stored results. The three deliberate
  reuses (D-01r, D-02, X-13) share the suffixed value of the id they reuse, so the collision
  being tested is preserved. X-01 sends no header at all.
- **Model-call ceiling.** Calls are counted from *new* trace ids as the run proceeds. The
  guard is checked *before* each case that expects a call, so the run aborts rather than
  exceeding the ceiling. Headroom is 1 call, so a retry will trip it — raise `--model-ceiling`
  deliberately if you need to rerun a failure in the same pass.
- **Chains run in order.** C1 (safety clarification), C2 (duplicates, six legs off D-01),
  C3 (response window, D-09a → D-08 → D-09b), C4 (restart idempotency). Chained cases are
  emitted as contiguous blocks, never interleaved.
- **Restart leg last.** Three phases in order: real config (X-13), bad model key (X-10), bad
  Northstar token (X-11). The runner reminds you to restore the real config at the end.
- **Work-order accounting.** Snapshots before, after each case, and at the end. Only the seven
  `dispatch_ready` creators may add a row (plus X-06 if it dispatches legitimately); the three
  replay cases must add none. Anything else is flagged as a G8 failure.

## Reading the results

`results/stress-run-<suffix>.json` holds a `summary` plus one record per delivery with the
request sent, the expected status, the full response, and every check with its verdict.

Verdicts: `PASS`, `FAIL`, `PASS*` (passed, but some check could not be verified — usually a
missing work-order snapshot), `ERROR` (transport failure).

Two things in the summary need a human:

- **`manualReviewQueue`** — cases whose real requirement is about meaning, not pattern
  matching. R-01/R-02/R-03 (reply content), S-11 (is the attachment's own wording preserved?)
  and C-08 (does the draft avoid account-review phrasing?) are the ones where the regex is the
  weakest proxy.
- **`failures[].requirementsGap`** — set on S-14, C-08 and D-07. A failure there may mean the
  rule has no written home yet rather than that the code is wrong. Read the response before
  filing anything.

The regex checks are deliberately blunt: `draftExcludesAdvice` and `draftNoPromisedDate` catch
obvious violations and will miss a politely phrased instruction or an implied date. A clean
automated pass on those is necessary, not sufficient.
