#!/usr/bin/env node
/**
 * Northstar triage — black-box stress runner.
 *
 * Reads tests/stress/cases.json, sends each case to a running service, checks the
 * documented expectations, and writes one results file.
 *
 * It never reads or writes app/. If something fails it is reported, not fixed.
 *
 *   node tests/stress/run-stress.mjs --dry-run            # list run order + totals, send nothing
 *   node tests/stress/run-stress.mjs --base http://localhost:8080
 *   node tests/stress/run-stress.mjs --phase main         # skip the restart leg
 *
 * Restart legs (X-13, X-10, X-11) need the container restarted between phases. The runner
 * pauses and prints the exact commands unless --restart-cmd is supplied.
 */

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execSync } from "node:child_process";
import { createInterface } from "node:readline";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "../..");

// ─── args ────────────────────────────────────────────────────────────────────
function parseArgs(argv) {
  const a = {
    base: "http://localhost:8080",
    dryRun: false,
    phases: null,
    modelCeiling: 45,
    out: null,
    runSuffix: null,
    restartCmd: null,
    timeoutMs: 180_000,
    yes: false,
  };
  for (let i = 2; i < argv.length; i += 1) {
    const k = argv[i];
    const next = () => argv[++i];
    if (k === "--dry-run" || k === "-n") a.dryRun = true;
    else if (k === "--base") a.base = next().replace(/\/$/, "");
    else if (k === "--phase") a.phases = next().split(",").map((s) => s.trim());
    else if (k === "--model-ceiling") a.modelCeiling = Number(next());
    else if (k === "--out") a.out = next();
    else if (k === "--run-suffix") a.runSuffix = next();
    else if (k === "--restart-cmd") a.restartCmd = next();
    else if (k === "--timeout") a.timeoutMs = Number(next());
    else if (k === "--yes" || k === "-y") a.yes = true;
    else throw new Error(`unknown argument: ${k}`);
  }
  return a;
}

const ARGS = parseArgs(process.argv);

// Run suffix keeps event ids unique across runs, so a rerun is never answered from
// the previous run's stored results. Cases that deliberately reuse an id share the
// same suffixed value, because the suffix is applied to the id itself, not per case.
const RUN_SUFFIX =
  ARGS.runSuffix ?? new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);

const CASES = JSON.parse(readFileSync(resolve(HERE, "cases.json"), "utf8"));

const PHASE_ORDER = ["main", "restart-clean", "restart-bad-model", "restart-bad-northstar"];
const PHASE_LABEL = {
  main: "Main suite (correct config)",
  "restart-clean": "Restart leg 1 — restart with the REAL config",
  "restart-bad-model": "Restart leg 2 — invalid CELECO_MODEL_GATEWAY_KEY",
  "restart-bad-northstar": "Restart leg 3 — invalid NORTHSTAR_ACCESS_TOKEN",
};

// ─── case ordering ───────────────────────────────────────────────────────────
// Chains run in their declared step order. Within a phase, chained cases are emitted
// as contiguous blocks (so a chain is never interleaved), then the unchained ones.
function orderCases(all, phases) {
  const wanted = phases ?? PHASE_ORDER;
  const out = [];
  for (const phase of PHASE_ORDER) {
    if (!wanted.includes(phase)) continue;
    const inPhase = all.filter((c) => c.phase === phase);
    const chains = new Map();
    const loose = [];
    for (const c of inPhase) {
      if (c.chain) {
        if (!chains.has(c.chain)) chains.set(c.chain, []);
        chains.get(c.chain).push(c);
      } else loose.push(c);
    }
    for (const [, members] of [...chains.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      members.sort((x, y) => (x.chainStep ?? 0) - (y.chainStep ?? 0));
      out.push(...members);
    }
    out.push(...loose);
  }
  return out;
}

// ─── payload building ────────────────────────────────────────────────────────
function buildLongBody(gen) {
  const filler = gen.fillerParagraph;
  const total = gen.targetChars;
  const hazard = gen.hazardSentence;
  const reps = Math.max(1, Math.ceil((total - hazard.length) / filler.length));
  const cut = Math.floor(reps * gen.hazardAtFraction);
  const head = filler.repeat(cut);
  const tail = filler.repeat(reps - cut);
  return head + hazard + tail;
}

function resolveRequest(kase, all) {
  if (kase.rawBody !== undefined) return { raw: kase.rawBody };
  const src = kase.sameRequestAs
    ? all.find((c) => c.id === kase.sameRequestAs)?.request
    : kase.request;
  if (!src) throw new Error(`${kase.id}: no request payload`);
  const body = { ...src };
  if (typeof body.sender === "string") {
    const s = CASES.senders[body.sender];
    if (!s) throw new Error(`${kase.id}: unknown sender ref ${body.sender}`);
    body.sender = { ...s };
  }
  if (body.body === "__GENERATED__") {
    if (kase.generate?.kind !== "longBody") throw new Error(`${kase.id}: __GENERATED__ without generate.longBody`);
    body.body = buildLongBody(kase.generate);
  }
  return { json: body };
}

function eventIdFor(kase) {
  if (kase.omitEventId) return null;
  return `${kase.eventId}-${RUN_SUFFIX}`;
}

// ─── work-order snapshots ────────────────────────────────────────────────────
// Read via the service's own upstream to avoid needing the Northstar key here:
// we shell out to the same base URL the service uses only if a helper is given.
// By default we snapshot through the Northstar API using NORTHSTAR_ACCESS_TOKEN
// from the environment, read-only.
const NS_BASE = process.env.NORTHSTAR_API_BASE || "https://hiring.celecolabs.com/api/assessment";

async function snapshotWorkOrders() {
  const token = process.env.NORTHSTAR_ACCESS_TOKEN;
  if (!token) {
    return { ok: false, reason: "NORTHSTAR_ACCESS_TOKEN not set in this shell — snapshot skipped", rows: [] };
  }
  try {
    const res = await fetch(`${NS_BASE}?route=work-orders`, {
      headers: { authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) return { ok: false, reason: `HTTP ${res.status}`, rows: [] };
    const body = await res.json();
    const rows = body?.results ?? body ?? [];
    return { ok: true, rows };
  } catch (err) {
    return { ok: false, reason: String(err?.message ?? err), rows: [] };
  }
}

// ─── checks ──────────────────────────────────────────────────────────────────
const ADVICE_PATTERNS = [
  /\bswitch(ing)? (it )?off\b/i, /\bturn (it )?off\b/i, /\bisolat(e|or|ing)\b/i,
  /\breset\b/i, /\brestart\b/i, /\bopen the doors?\b/i, /\bventilat(e|ing|ion)\b/i,
  /\bunplug\b/i, /\bbreaker\b/i, /\bmop\b/i, /\bcheck the valve\b/i,
  /\byou should (try|check|open|close|switch|turn|reset)\b/i, /\btry (the|to) \w+/i,
];
const DATE_PATTERNS = [
  /\b\d{4}-\d{2}-\d{2}\b/, /\b\d{1,2}\/\d{1,2}\/\d{2,4}\b/,
  /\b(mon|tues|wednes|thurs|fri|satur|sun)day\b/i,
  /\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b/i,
  /\bnext (week|month|monday|tuesday|wednesday|thursday|friday)\b/i,
  /\bwithin \d+\s*(hour|day|week)/i, /\bby (friday|monday|tomorrow|the end of)\b/i,
  /\b\d{1,2}\s*(am|pm)\b/i, /\btime slot\b/i, /\bslot (of|on|at)\b/i, /\btomorrow\b/i,
];
const INTERNAL_PATTERNS = [
  /\bconfidence\b/i, /\bPOL-[A-Z]+-\d+/, /\bOPS-[A-Z]+-\d+/, /\bSYS-[A-Z]+-\d+/,
  /\bCELECO-[A-Z]+-\d+/, /\bsystem prompt\b/i, /\bHTTP \d{3}\b/, /\bTraceback\b/i,
  /\bstack trace\b/i, /\bexception\b/i, /\b0\.\d+\b/,
];
const KEY_PATTERNS = [/cel_northstar_/i, /cel_model_/i, /\bBearer\s+\S{8,}/i, /sk-[A-Za-z0-9]{10,}/];
const RAW_ERROR_PATTERNS = [/\bTraceback\b/i, /\bstack trace\b/i, /\bHTTP \d{3}\b/, /\bECONNREFUSED\b/i, /\bgetaddrinfo\b/i, /\b5\d\d (Internal|Bad|Service)\b/i];

function deepString(obj) {
  try { return JSON.stringify(obj ?? null); } catch { return String(obj); }
}

function entitlementText(result) {
  return deepString(result?.entitlement);
}

function findResponseHours(result) {
  // look for a numeric response-window value anywhere in entitlement
  const ent = result?.entitlement ?? {};
  const seen = [];
  const walk = (v, key) => {
    if (v == null) return;
    if (typeof v === "number") {
      if (/hour|sla|response|window/i.test(key ?? "")) seen.push(v);
      return;
    }
    if (typeof v === "string") {
      const m = v.match(/(\d+)\s*(?:h\b|hour)/i);
      if (m) seen.push(Number(m[1]));
      return;
    }
    if (Array.isArray(v)) { v.forEach((x) => walk(x, key)); return; }
    if (typeof v === "object") { for (const [k, x] of Object.entries(v)) walk(x, k); }
  };
  walk(ent, "entitlement");
  return seen;
}

/** Each check returns {name, pass, detail}. refs carries recorded values across cases. */
function runChecks(kase, ctx) {
  const { result, http, elapsedMs, refs, woAfter, woBefore, eventId } = ctx;
  const out = [];
  const add = (name, pass, detail = "") => out.push({ name, pass, detail });
  const draft = String(result?.customerResponseDraft ?? "");
  const whole = deepString(result);

  for (const chk of kase.checks ?? []) {
    const label = chk.label ? `${chk.type} (${chk.label})` : chk.type;
    switch (chk.type) {
      case "safetyRisk":
        add(`${label} == ${chk.value}`, result?.classification?.safetyRisk === chk.value,
          `got ${JSON.stringify(result?.classification?.safetyRisk)}`);
        break;
      case "noWorkOrder":
        add("workOrder is null", result?.workOrder == null,
          result?.workOrder ? `got ${deepString(result.workOrder).slice(0, 200)}` : "");
        break;
      case "workOrderPresent":
        add("workOrder present", result?.workOrder != null);
        break;
      case "workOrderField": {
        const got = result?.workOrder?.[chk.field] ?? result?.workOrder?.[toSnake(chk.field)];
        add(`workOrder.${chk.field} == ${chk.equals}`, got === chk.equals, `got ${JSON.stringify(got)}`);
        break;
      }
      case "workOrderEventId": {
        // The service's workOrder object does not echo externalEventId (confirmed against the
        // live response shape), so assert it on the upstream row the snapshot returns instead.
        const wo = result?.workOrder ?? {};
        const direct = wo.externalEventId ?? wo.external_event_id;
        if (direct !== undefined) {
          add("workOrder.externalEventId == X-Event-ID sent", direct === eventId, `got ${JSON.stringify(direct)}, sent ${eventId}`);
          break;
        }
        if (!woAfter?.ok) { add("work order keyed on X-Event-ID", null, "no snapshot — verify manually"); break; }
        const woId = wo.id ?? wo.workOrderId ?? null;
        const row = woAfter.rows.find((r) => r.id === woId);
        if (!row) { add("work order keyed on X-Event-ID", null, `row ${woId} not in snapshot — verify manually`); break; }
        const rowEv = rowEvent(row);
        add("upstream work order keyed on X-Event-ID", rowEv === eventId, `upstream row carries ${JSON.stringify(rowEv)}, sent ${eventId}`);
        break;
      }
      case "technicianIn": {
        const t = techOf(result);
        add(`technician in [${chk.allowed.join(", ")}]`, t != null && chk.allowed.includes(t), `got ${JSON.stringify(t)}`);
        break;
      }
      case "technicianNot": {
        const t = techOf(result);
        add(`technician not in [${chk.disallowed.join(", ")}]${chk.label ? " — " + chk.label : ""}`,
          t == null || !chk.disallowed.includes(t), `got ${JSON.stringify(t)}`);
        break;
      }
      case "entity": {
        const got = result?.entities?.[chk.field] ?? null;
        add(`entities.${chk.field} == ${JSON.stringify(chk.equals)}`, got === chk.equals, `got ${JSON.stringify(got)}`);
        break;
      }
      case "verbatim":
        add(`verbatim preserved: "${truncate(chk.phrase, 60)}"`, whole.includes(chk.phrase),
          whole.includes(chk.phrase) ? "" : "phrase not found anywhere in the result");
        break;
      case "verbatimAnyOf": {
        const hit = chk.phrases.find((p) => whole.includes(p));
        add(`verbatim (any of ${chk.phrases.length}) preserved`, Boolean(hit), hit ? `matched "${truncate(hit, 50)}"` : `none of: ${chk.phrases.map((p) => truncate(p, 30)).join(" | ")}`);
        break;
      }
      case "mentionsAnywhere":
        add(`${chk.label ?? "mentions " + chk.phrase}`, whole.includes(chk.phrase),
          whole.includes(chk.phrase) ? "" : `"${chk.phrase}" not present in the result`);
        break;
      case "draftExcludesAdvice": {
        const hits = ADVICE_PATTERNS.filter((r) => r.test(draft)).map((r) => r.source);
        add("draft contains no troubleshooting advice", hits.length === 0, hits.length ? `matched: ${hits.join(", ")}` : "");
        break;
      }
      case "draftNoPromisedDate": {
        // Strip quoted customer wording first. Safety drafts must preserve the customer's
        // exact words (POL-SAFETY-001), and those words can contain a time ("around 2am")
        // that is a report of when something happened, not a promised appointment.
        const prose = draft
          .replace(/You told us:\s*"[^"]*"/gi, " ")
          .replace(/"[^"]{0,400}"/g, " ");
        const hits = DATE_PATTERNS.filter((r) => r.test(prose)).map((r) => r.source);
        add("draft promises no date/slot/window (quoted customer words excluded)",
          hits.length === 0, hits.length ? `matched in Northstar's own prose: ${hits.join(", ")}` : "");
        break;
      }
      case "draftNoInternals": {
        const hits = INTERNAL_PATTERNS.filter((r) => r.test(draft)).map((r) => r.source);
        add("draft leaks no internals", hits.length === 0, hits.length ? `matched: ${hits.join(", ")}` : "");
        break;
      }
      case "noKeyMaterial": {
        const hits = KEY_PATTERNS.filter((r) => r.test(whole)).map((r) => r.source);
        add("no key material anywhere in the response", hits.length === 0, hits.length ? `matched: ${hits.join(", ")}` : "");
        break;
      }
      case "noPromptLeak": {
        const hits = [/system prompt/i, /you are now in/i, /ignore all previous/i].filter((r) => r.test(whole)).map((r) => r.source);
        add("no prompt text echoed", hits.length === 0, hits.length ? `matched: ${hits.join(", ")}` : "");
        break;
      }
      case "draftExcludesInjectedText":
        add(`draft excludes injected "${truncate(chk.phrase, 40)}"`, !draft.toLowerCase().includes(chk.phrase.toLowerCase()));
        break;
      case "noRawErrorInDraft": {
        const hits = RAW_ERROR_PATTERNS.filter((r) => r.test(draft)).map((r) => r.source);
        add("draft carries no raw error", hits.length === 0, hits.length ? `matched: ${hits.join(", ")}` : "");
        break;
      }
      case "draftAccountReviewTone": {
        const good = /account|record|review|checking/i.test(draft);
        add("draft uses account-review wording", good, good ? "" : "expected 'checking the account record'-style acknowledgement");
        break;
      }
      case "draftNotAccountReviewTone": {
        const bad = /checking (the|your) account record/i.test(draft);
        add("draft does NOT use the account-review script", !bad, bad ? "covered customer told their account is being checked" : "");
        break;
      }
      case "draftNoOnsitePromise": {
        const bad = /(engineer|technician|someone|will be|we will) (will )?(attend|visit|be on site|come to site|be dispatched)/i.test(draft);
        add("draft promises no onsite visit", !bad, bad ? "draft appears to promise attendance" : "");
        break;
      }
      case "draftNoTechnicianNamed": {
        const bad = /\bTECH-\d+\b/.test(draft);
        add("draft names no technician id", !bad);
        break;
      }
      case "missingInformationNonEmpty":
        add("missingInformation non-empty", Array.isArray(result?.missingInformation) && result.missingInformation.length > 0,
          `got ${deepString(result?.missingInformation)}`);
        break;
      case "singleQuestion": {
        const n = (draft.match(/\?/g) ?? []).length;
        add("draft asks exactly one question", n === 1, `found ${n} question mark(s)`);
        break;
      }
      case "warningsNonEmpty":
        add(`audit.warnings non-empty${chk.label ? " — " + chk.label : ""}`,
          Array.isArray(result?.audit?.warnings) && result.audit.warnings.length > 0,
          `got ${deepString(result?.audit?.warnings)}`);
        break;
      case "auditNonEmpty": {
        const sr = result?.audit?.sourceReferences;
        add("audit.sourceReferences non-empty", Array.isArray(sr) && sr.length > 0, `got ${deepString(sr)}`);
        break;
      }
      case "modelTraceIdsEmpty": {
        const ids = result?.audit?.modelTraceIds ?? [];
        add("audit.modelTraceIds empty", Array.isArray(ids) && ids.length === 0, `got ${deepString(ids)}`);
        break;
      }
      case "entitlementMentions": {
        const txt = entitlementText(result) + whole;
        const hit = chk.any.find((x) => txt.includes(x));
        add(`entitlement cites one of [${chk.any.join(", ")}]`, Boolean(hit), hit ? `matched ${hit}` : "none found");
        break;
      }
      case "entitlementNonEmpty": {
        const e = result?.entitlement;
        const ok = e && Object.keys(e).length > 0 && deepString(e) !== "{}";
        add(`entitlement populated${chk.label ? " — " + chk.label : ""}`, Boolean(ok), `got ${truncate(entitlementText(result), 160)}`);
        break;
      }
      case "entitlementExcludesAttachment": {
        const txt = entitlementText(result).toLowerCase();
        const bad = /transfer advice|bank transfer|payment|receipt|1,84,000|184000/.test(txt);
        add("entitlement evidence excludes the payment attachment", !bad, bad ? "payment advice appears in entitlement evidence" : "");
        break;
      }
      case "responseHours": {
        const found = findResponseHours(result);
        add(`response hours == ${chk.value}`, found.includes(chk.value), `found ${JSON.stringify(found)}`);
        break;
      }
      case "responseHoursNot": {
        const found = findResponseHours(result);
        add(`response hours != ${chk.value}`, !found.includes(chk.value), `found ${JSON.stringify(found)}`);
        break;
      }
      case "statusNot":
        add(`status != ${chk.value}`, result?.status !== chk.value, `got ${result?.status}`);
        break;
      case "nothingProgressed": {
        const bad = ["dispatch_ready", "covered_action"].includes(result?.status) || result?.workOrder != null;
        add("nothing progressed (no dispatch, no covered commitment, no WO)", !bad, bad ? `status ${result?.status}` : "");
        break;
      }
      case "recordCaseId":
        refs[chk.as] = {
          caseId: result?.caseId,
          workOrderId: result?.workOrder?.id ?? result?.workOrder?.workOrderId ?? null,
          requestId: kase.request?.requestId,
          traceIds: result?.audit?.modelTraceIds ?? [],
        };
        add(`recorded reference "${chk.as}"`, true, deepString(refs[chk.as]));
        break;
      case "sameCaseIdAs": {
        const ref = refs[chk.ref];
        add(`caseId equals ${chk.ref}'s`, Boolean(ref) && result?.caseId === ref.caseId,
          `got ${result?.caseId}, expected ${ref?.caseId}`);
        break;
      }
      case "sameWorkOrderIdAs": {
        const ref = refs[chk.ref];
        const got = result?.workOrder?.id ?? result?.workOrder?.workOrderId ?? null;
        add(`workOrder id equals ${chk.ref}'s`, Boolean(ref) && got === ref.workOrderId,
          `got ${got}, expected ${ref?.workOrderId}`);
        break;
      }
      case "differentWorkOrderFrom": {
        const ref = refs[chk.ref];
        const got = result?.workOrder?.id ?? result?.workOrder?.workOrderId ?? null;
        add(`workOrder id differs from ${chk.ref}'s`, got != null && got !== ref?.workOrderId,
          `got ${got}, ${chk.ref} was ${ref?.workOrderId}`);
        break;
      }
      case "noNewModelTrace": {
        const ref = refs[chk.ref];
        const now = result?.audit?.modelTraceIds ?? [];
        const prior = new Set(ref?.traceIds ?? []);
        const fresh = now.filter((t) => !prior.has(t));
        add("no NEW model trace id (stored result replayed)", fresh.length === 0,
          fresh.length ? `new trace id(s): ${fresh.join(", ")} — a model call was spent on a known event` : "");
        break;
      }
      case "linksReference": {
        const ref = refs[chk.ref];
        const needles = [ref?.workOrderId, ref?.requestId, ref?.caseId].filter(Boolean);
        const hit = needles.find((n) => whole.includes(n));
        add(`links back to ${chk.ref} (WO/request/case id)`, Boolean(hit),
          hit ? `matched ${hit}` : `none of ${needles.join(", ")} present in the result`);
        break;
      }
      case "workOrderCountForEvent": {
        if (!woAfter?.ok) { add(`exactly ${chk.equals} work order for ${chk.eventKey}`, null, "snapshot unavailable — verify manually"); break; }
        const key = `${chk.eventKey}-${RUN_SUFFIX}`;
        const n = countWorkOrdersForEvent(woAfter.rows, key, woBefore?.rows ?? []);
        add(`exactly ${chk.equals} work order for ${key}`, n === chk.equals, `counted ${n}`);
        break;
      }
      case "noWorkOrderCreatedAnywhere": {
        if (!woAfter?.ok) { add("no work order created", null, "snapshot unavailable — verify manually"); break; }
        // Attribute by this case's own event id. Comparing against the run baseline would
        // blame a case for every row earlier cases legitimately created.
        const mine = eventId ? woAfter.rows.filter((r) => rowEvent(r) === eventId) : [];
        const returned = result?.workOrder != null;
        add("no work order created by this case", mine.length === 0 && !returned,
          mine.length ? `rows for ${eventId}: ${mine.map((r) => r.id).join(", ")}` : returned ? "a workOrder was returned in the result" : "");
        break;
      }
      case "noModelCall": {
        const ids = result?.audit?.modelTraceIds ?? [];
        const n = Array.isArray(ids) ? ids.length : 0;
        add("no model call made", n === 0, n ? `${n} trace id(s) present — validation ran after the model (budget leak)` : "");
        break;
      }
      case "notServerError":
        add("not a 5xx", !(http >= 500), `HTTP ${http}`);
        break;
      case "bounded":
        add("returned within timeout", elapsedMs < ARGS.timeoutMs, `${elapsedMs}ms`);
        break;
      default:
        add(`UNIMPLEMENTED CHECK: ${chk.type}`, null, "runner does not know this check type");
    }
  }
  return out;
}

const toSnake = (s) => s.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);
const truncate = (s, n) => (String(s).length > n ? String(s).slice(0, n) + "…" : String(s));
function techOf(result) {
  const wo = result?.workOrder ?? {};
  return wo.technicianId ?? wo.technician_id ?? null;
}
function rowEvent(r) {
  return r.externalEventId ?? r.external_event_id ?? r.event_id ?? null;
}
function countWorkOrdersForEvent(rows, key, beforeRows) {
  const byEvent = rows.filter((r) => rowEvent(r) === key);
  if (byEvent.length) return byEvent.length;
  // Fall back to counting rows that are new since the baseline, when the route does
  // not expose the event id. Reported as a count, caveated in the results file.
  const before = new Set(beforeRows.map((r) => r.id));
  return rows.filter((r) => !before.has(r.id)).length ? -1 : 0;
}
function newRows(before, after) {
  const seen = new Set(before.map((r) => r.id));
  return after.filter((r) => !seen.has(r.id));
}

// ─── status evaluation ───────────────────────────────────────────────────────
function evaluateStatus(kase, result, http) {
  const e = kase.expect ?? {};
  if (e.httpAnyOf) {
    if (e.httpAnyOf.includes(http)) return { pass: true, detail: `HTTP ${http} (accepted)` };
    if (e.orStatus && result?.status === e.orStatus) return { pass: true, detail: `HTTP ${http} with status ${result.status} (accepted alternative)` };
    if (e.orStatusAnyOf?.includes(result?.status)) return { pass: true, detail: `HTTP ${http} with status ${result.status} (accepted alternative)` };
    const alts = e.orStatus ? ` or status ${e.orStatus}` : e.orStatusAnyOf ? ` or status in ${e.orStatusAnyOf.join("/")}` : "";
    return { pass: false, detail: `expected HTTP ${e.httpAnyOf.join("/")}${alts}, got HTTP ${http} status ${result?.status ?? "none"}` };
  }
  if (e.statusAnyOf) {
    const ok = e.statusAnyOf.includes(result?.status);
    return { pass: ok, detail: ok ? `status ${result.status} (one of ${e.statusAnyOf.join("/")})` : `expected one of ${e.statusAnyOf.join("/")}, got ${result?.status ?? "none"}` };
  }
  const ok = result?.status === e.status;
  return { pass: ok, detail: ok ? `status ${result.status}` : `expected ${e.status}, got ${result?.status ?? "none"}` };
}

// Global assertions G1/G2/G3/G4/G6, applied to every successful case result.
const REQUIRED_KEYS = ["caseId", "status", "entities", "classification", "entitlement", "nextActions", "customerResponseDraft", "missingInformation", "workOrder", "audit"];
const STATUS_ENUM = ["covered_action", "clarification_required", "human_escalation_required", "account_review_required", "duplicate_detected", "dispatch_ready", "resource_escalation_required", "processing", "failed"];

function globalChecks(kase, result, http) {
  if (result == null || typeof result !== "object") return [];
  // A 4xx validation rejection legitimately returns an error body, not a case result.
  // G1/G2/G6 describe the case-result contract and do not apply to it.
  if (http >= 400 && !("status" in result)) {
    return [{ name: "G-n/a validation error body (case-result assertions not applicable)", pass: true, detail: `HTTP ${http}` }];
  }
  const out = [];
  const add = (name, pass, detail = "") => out.push({ name, pass, detail });
  const missing = REQUIRED_KEYS.filter((k) => !(k in result));
  add("G1 all 10 required keys present", missing.length === 0, missing.length ? `missing: ${missing.join(", ")}` : "");
  add("G1 status within enum", STATUS_ENUM.includes(result.status), `got ${result.status}`);
  add("G2 safetyRisk is boolean", typeof result?.classification?.safetyRisk === "boolean", `got ${typeof result?.classification?.safetyRisk}`);
  const conf = result?.classification?.confidence;
  add("G2 confidence in [0,1]", typeof conf === "number" && conf >= 0 && conf <= 1, `got ${JSON.stringify(conf)}`);
  const isDispatch = result.status === "dispatch_ready";
  add("G3 workOrder only when dispatch_ready", isDispatch ? result.workOrder != null : result.workOrder == null,
    `status ${result.status}, workOrder ${result.workOrder == null ? "null" : "present"}`);
  const draft = String(result.customerResponseDraft ?? "");
  const leak = KEY_PATTERNS.filter((r) => r.test(draft)).map((r) => r.source);
  add("G4 no key material in the draft", leak.length === 0, leak.join(", "));
  const ids = result?.audit?.modelTraceIds;
  add("G6 modelTraceIds is a string array", Array.isArray(ids) && ids.every((x) => typeof x === "string"), `got ${deepString(ids)}`);
  // A no-call case that REPLAYS a stored result correctly carries the original trace id;
  // "no new call" is asserted per-case by noNewModelTrace. Only cases rejected before the
  // read step should have an empty list.
  if (kase.expectsModelCall === false && !kase.reuseEventIdOf) {
    add("G6 modelTraceIds empty for a no-call case", Array.isArray(ids) && ids.length === 0, `got ${deepString(ids)}`);
  }
  return out;
}

// ─── dry run ─────────────────────────────────────────────────────────────────
function dryRun(ordered) {
  const lines = [];
  const say = (s = "") => { lines.push(s); console.log(s); };

  say("NORTHSTAR TRIAGE — STRESS SUITE DRY RUN");
  say("=".repeat(78));
  say(`Cases file     : tests/stress/cases.json (revision ${CASES.meta.revision})`);
  say(`Target         : ${ARGS.base}  (nothing sent — dry run)`);
  say(`Run suffix     : -${RUN_SUFFIX}   (appended to every X-Event-ID)`);
  say(`Model ceiling  : ${ARGS.modelCeiling} calls — the run aborts before exceeding this`);
  say("");

  let running = 0;
  let n = 0;
  const perPhase = {};

  for (const phase of PHASE_ORDER) {
    const inPhase = ordered.filter((c) => c.phase === phase);
    if (!inPhase.length) continue;
    say("");
    say(`── ${PHASE_LABEL[phase]} ${"─".repeat(Math.max(0, 55 - PHASE_LABEL[phase].length))}`);
    if (phase !== "main") {
      say(`   RESTART REQUIRED before this phase.`);
    }
    say("");
    say("   #   case     model  chain   event id                              expected status");
    say("   " + "-".repeat(94));
    let phaseCalls = 0;
    for (const k of inPhase) {
      n += 1;
      const call = k.expectsModelCall ? "yes" : "no ";
      if (k.expectsModelCall) { running += 1; phaseCalls += 1; }
      const chain = k.chain ? `${k.chain}.${k.chainStep}` : "—   ";
      const eid = k.omitEventId ? "(omitted)" : eventIdFor(k);
      const exp = k.expect.status ?? (k.expect.statusAnyOf ? k.expect.statusAnyOf.join(" | ") : `HTTP ${k.expect.httpAnyOf?.join("/")}${k.expect.orStatus ? ` | ${k.expect.orStatus}` : ""}`);
      const reuse = k.reuseEventIdOf ? ` (reuses ${k.reuseEventIdOf})` : "";
      say(`   ${String(n).padStart(2)}  ${k.id.padEnd(7)}  ${call}   ${String(chain).padEnd(6)}  ${String(eid).padEnd(36)}  ${exp}${reuse}`);
    }
    perPhase[phase] = { cases: inPhase.length, calls: phaseCalls };
    say(`   ${"-".repeat(94)}`);
    say(`   phase subtotal: ${inPhase.length} deliveries, ${phaseCalls} model calls (running total ${running})`);
  }

  const expectedCalls = ordered.filter((c) => c.expectsModelCall).length;
  const freeCalls = ordered.length - expectedCalls;

  say("");
  say("=".repeat(78));
  say("TOTALS");
  say("=".repeat(78));
  for (const phase of PHASE_ORDER) {
    if (!perPhase[phase]) continue;
    say(`  ${PHASE_LABEL[phase].padEnd(52)} ${String(perPhase[phase].cases).padStart(3)} deliveries  ${String(perPhase[phase].calls).padStart(3)} calls`);
  }
  say("  " + "-".repeat(74));
  say(`  ${"TOTAL".padEnd(52)} ${String(ordered.length).padStart(3)} deliveries  ${String(expectedCalls).padStart(3)} calls`);
  say("");
  // A redelivery (sameRequestAs + a reused event id) is the same case sent twice,
  // not a second case. D-02 and X-13 reuse an id but send their own payload, so they count.
  const redeliveries = ordered.filter((c) => c.reuseEventIdOf && c.sameRequestAs).length;
  say(`  Distinct cases          : ${ordered.length - redeliveries}  (${ordered.length} deliveries − ${redeliveries} redelivery of an identical payload)`);
  say(`  Deliveries that should cost NOTHING : ${freeCalls}`);
  const free = ordered.filter((c) => !c.expectsModelCall).map((c) => c.id);
  say(`    ${free.join(", ")}`);
  say("");
  say(`  Expected model calls    : ${expectedCalls}`);
  say(`  Ceiling                 : ${ARGS.modelCeiling}`);
  const headroom = ARGS.modelCeiling - expectedCalls;
  say(`  Headroom                : ${headroom} ${headroom < 0 ? "*** OVER CEILING — the run would abort ***" : headroom <= 2 ? "(tight — a single retry could trip the ceiling)" : ""}`);
  say("");

  say("DISPATCH CASES — the only ones that may CREATE a work order");
  const creators = ordered
    .filter((c) => (c.expect.status ?? "") === "dispatch_ready" && !c.reuseEventIdOf)
    .map((c) => c.id);
  const replayers = ordered.filter((c) => c.reuseEventIdOf).map((c) => c.id);
  say(`  create a WO  : ${creators.join(", ")}  (${creators.length} rows expected)`);
  say(`  conditional  : X-06 (only if its genuine content dispatches legitimately)`);
  say(`  replay only  : ${replayers.join(", ")} — these RETURN an existing work order and must`);
  say(`                 not add a row; that is exactly what they are testing.`);
  say(`  Every other new work-order row is a G8 failure.`);
  say("");

  say("CHAINS — must run in this order");
  const chains = {};
  for (const c of ordered) if (c.chain) (chains[c.chain] ??= []).push(c);
  for (const [name, members] of Object.entries(chains)) {
    say(`  ${name}: ${members.sort((a, b) => a.chainStep - b.chainStep).map((m) => `${m.id}(${m.chainStep})`).join(" → ")}`);
  }
  say("");

  say("EVENT-ID REUSE — deliberate, suffix shared so the collision is preserved");
  for (const c of ordered.filter((x) => x.reuseEventIdOf)) {
    say(`  ${c.id} reuses ${c.reuseEventIdOf}'s id → ${eventIdFor(c)}`);
  }
  say(`  X-01 sends NO X-Event-ID header at all.`);
  say("");

  say("PRE-FLIGHT REQUIREMENTS (checked at the start of a real run)");
  say("  1. Service healthy at GET /health");
  say("  2. Container running the latest code with the MODEL CACHE DISABLED");
  say("     (so these are real model calls, not replays)");
  say("  3. NORTHSTAR_ACCESS_TOKEN set in this shell for the work-order snapshots");
  say("  4. Work-order baseline captured — expected 4 pre-seeded rows:");
  say(`     ${CASES.meta.preSeededWorkOrders.join(", ")}`);
  say("  5. WO-9290 still open/assigned, else D-07 and S-12 are skipped");
  say("");
  say("RESTART LEG — run last, in this order, restoring the real config afterwards");
  say("  a. restart with the REAL config      → X-13 (idempotency across a restart)");
  say("  b. restart with a bad model key      → X-10");
  say("  c. restart with a bad Northstar token → X-11");
  say("  d. restore the real config and verify /health");
  say("");
  say("Nothing was sent. Re-run without --dry-run to execute.");

  const outPath = ARGS.out ?? resolve(HERE, "results", `dry-run-${RUN_SUFFIX}.txt`);
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, lines.join("\n") + "\n", "utf8");
  console.log(`\nDry-run listing also written to ${outPath}`);
}

// ─── live run ────────────────────────────────────────────────────────────────
function ask(question) {
  if (ARGS.yes) return Promise.resolve("");
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((res) => rl.question(question, (a) => { rl.close(); res(a); }));
}

async function sendCase(kase, all) {
  const payload = resolveRequest(kase, all);
  const eventId = eventIdFor(kase);
  const headers = { "content-type": "application/json" };
  if (eventId) headers["x-event-id"] = eventId;
  const body = payload.raw !== undefined ? payload.raw : JSON.stringify(payload.json);
  const t0 = Date.now();
  let http = 0; let result = null; let transportError = null; let rawText = "";
  try {
    const res = await fetch(`${ARGS.base}/cases/process`, {
      method: "POST", headers, body,
      signal: AbortSignal.timeout(ARGS.timeoutMs),
    });
    http = res.status;
    rawText = await res.text();
    try { result = JSON.parse(rawText); } catch { result = null; }
  } catch (err) {
    transportError = String(err?.message ?? err);
  }
  return { eventId, http, result, rawText, transportError, elapsedMs: Date.now() - t0, sent: payload.raw !== undefined ? { raw: truncate(payload.raw, 400) } : payload.json };
}

async function liveRun(ordered, all) {
  const started = new Date().toISOString();
  console.log(`Stress run ${RUN_SUFFIX} → ${ARGS.base}`);

  // pre-flight
  let health = null;
  try {
    const r = await fetch(`${ARGS.base}/health`, { signal: AbortSignal.timeout(15_000) });
    health = { ok: r.ok, status: r.status };
  } catch (err) { health = { ok: false, error: String(err?.message ?? err) }; }
  if (!health.ok) {
    console.error(`GET /health failed (${deepString(health)}). Start the container first; aborting.`);
    process.exitCode = 1;
    return;
  }
  console.log(`/health OK`);

  const baseline = await snapshotWorkOrders();
  console.log(baseline.ok ? `Work-order baseline: ${baseline.rows.length} rows` : `Work-order baseline unavailable: ${baseline.reason}`);
  const openIds = new Set(baseline.rows.map((r) => r.id));

  const refs = {};
  const records = [];
  let modelCalls = 0;
  let aborted = null;
  let currentPhase = null;

  for (const kase of ordered) {
    if (kase.phase !== currentPhase) {
      currentPhase = kase.phase;
      if (currentPhase !== "main") {
        console.log(`\n=== ${PHASE_LABEL[currentPhase]} ===`);
        const cmd = ARGS.restartCmd;
        if (cmd) {
          const envNote = { "restart-clean": "real config", "restart-bad-model": "BAD model key", "restart-bad-northstar": "BAD Northstar token" }[currentPhase];
          console.log(`Running restart hook for: ${envNote}`);
          try { execSync(`${cmd} ${currentPhase}`, { stdio: "inherit", cwd: REPO }); } catch (e) { console.error(`restart hook failed: ${e.message}`); }
        } else {
          await ask(`Restart the container for "${PHASE_LABEL[currentPhase]}", then press Enter… `);
        }
      }
    }

    // ceiling guard — check BEFORE spending the call
    if (kase.expectsModelCall && modelCalls + 1 > ARGS.modelCeiling) {
      aborted = `model-call ceiling ${ARGS.modelCeiling} would be exceeded at ${kase.id}`;
      console.error(`\nABORT: ${aborted}`);
      break;
    }

    if (kase.requiresOpenWorkOrder && baseline.ok && !openIds.has(kase.requiresOpenWorkOrder)) {
      console.log(`SKIP ${kase.id}: requires ${kase.requiresOpenWorkOrder} to be open; not in the baseline`);
      records.push({ id: kase.id, skipped: `requires ${kase.requiresOpenWorkOrder} open` });
      continue;
    }

    const sent = await sendCase(kase, all);
    const woAfter = await snapshotWorkOrders();

    const traceIds = sent.result?.audit?.modelTraceIds ?? [];
    const priorTraces = new Set(Object.values(refs).flatMap((r) => r.traceIds ?? []));
    const freshTraces = (Array.isArray(traceIds) ? traceIds : []).filter((t) => !priorTraces.has(t));
    modelCalls += freshTraces.length;

    const statusCheck = evaluateStatus(kase, sent.result, sent.http);
    const checks = [
      { name: `EXPECTED STATUS — ${statusCheck.detail}`, pass: statusCheck.pass, detail: "" },
      ...globalChecks(kase, sent.result, sent.http),
      ...runChecks(kase, { result: sent.result, http: sent.http, elapsedMs: sent.elapsedMs, refs, woAfter, woBefore: baseline, eventId: sent.eventId }),
    ];

    const failed = checks.filter((c) => c.pass === false);
    const unknown = checks.filter((c) => c.pass === null);
    const verdict = sent.transportError ? "ERROR" : failed.length ? "FAIL" : unknown.length ? "PASS*" : "PASS";

    console.log(`${verdict.padEnd(5)} ${kase.id.padEnd(7)} ${statusCheck.detail}${failed.length ? ` — ${failed.length} check(s) failed` : ""}${freshTraces.length ? ` [${freshTraces.length} model call]` : " [no model call]"}  (calls so far: ${modelCalls}/${ARGS.modelCeiling})`);
    for (const f of failed) console.log(`        ✗ ${f.name}${f.detail ? ` — ${f.detail}` : ""}`);

    records.push({
      id: kase.id,
      category: kase.category,
      phase: kase.phase,
      chain: kase.chain ? `${kase.chain}.${kase.chainStep}` : null,
      verdict,
      expected: kase.expect,
      eventIdSent: sent.eventId,
      expectsModelCall: Boolean(kase.expectsModelCall),
      modelCallsObserved: freshTraces.length,
      modelTraceIds: traceIds,
      request: sent.sent,
      http: sent.http,
      elapsedMs: sent.elapsedMs,
      transportError: sent.transportError,
      checks,
      source: kase.source,
      manual: kase.manual ?? null,
      requirementsGap: kase.requirementsGap ?? null,
      note: kase.note ?? null,
      response: sent.result ?? (sent.rawText ? { __unparsed: truncate(sent.rawText, 4000) } : null),
    });
  }

  const finalSnap = await snapshotWorkOrders();
  const created = finalSnap.ok && baseline.ok ? newRows(baseline.rows, finalSnap.rows) : [];
  const allowed = new Set(CASES.meta.dispatchReadyCases.concat(CASES.meta.dispatchReadyConditional));

  const summary = {
    run: RUN_SUFFIX,
    startedAt: started,
    finishedAt: new Date().toISOString(),
    base: ARGS.base,
    revision: CASES.meta.revision,
    aborted,
    deliveries: records.length,
    passed: records.filter((r) => r.verdict === "PASS").length,
    passedWithUnknowns: records.filter((r) => r.verdict === "PASS*").length,
    failed: records.filter((r) => r.verdict === "FAIL").length,
    errored: records.filter((r) => r.verdict === "ERROR").length,
    skipped: records.filter((r) => r.skipped).length,
    modelCallsObserved: modelCalls,
    modelCallCeiling: ARGS.modelCeiling,
    workOrders: {
      baselineAvailable: baseline.ok,
      baselineCount: baseline.rows.length,
      finalCount: finalSnap.ok ? finalSnap.rows.length : null,
      createdDuringRun: created.map((r) => ({ id: r.id, asset: r.asset_id ?? r.assetId, event: rowEvent(r) })),
      expectedCreators: [...allowed],
      g8Note: "Only dispatch_ready cases may create a work order. Any other new row is a G8 failure.",
    },
    failures: records.filter((r) => r.verdict === "FAIL").map((r) => ({
      id: r.id,
      expected: r.expected,
      got: r.response?.status ?? `HTTP ${r.http}`,
      failedChecks: r.checks.filter((c) => c.pass === false).map((c) => `${c.name}${c.detail ? ` — ${c.detail}` : ""}`),
      requirementsGap: r.requirementsGap,
    })),
    manualReviewQueue: records.filter((r) => r.manual).map((r) => ({ id: r.id, what: r.manual })),
  };

  const outPath = ARGS.out ?? resolve(HERE, "results", `stress-run-${RUN_SUFFIX}.json`);
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, JSON.stringify({ summary, cases: records }, null, 2), "utf8");

  console.log("\n" + "=".repeat(70));
  console.log(`${summary.passed} passed, ${summary.passedWithUnknowns} passed-with-unverified, ${summary.failed} failed, ${summary.errored} errored, ${summary.skipped} skipped`);
  console.log(`model calls observed: ${modelCalls}/${ARGS.modelCeiling}`);
  if (created.length) console.log(`work orders created: ${created.map((r) => r.id).join(", ")}`);
  if (aborted) console.log(`ABORTED: ${aborted}`);
  console.log(`Results → ${outPath}`);
  if (summary.failed || summary.errored) {
    console.log("\nFailures are reported, not fixed. Fixes belong in the build session.");
    process.exitCode = 1;
  }
  if (currentPhase && currentPhase !== "main") {
    console.log("\nREMINDER: restore the real configuration and verify /health.");
  }
}

// ─── main ────────────────────────────────────────────────────────────────────
const ordered = orderCases(CASES.cases, ARGS.phases);
if (ARGS.dryRun) dryRun(ordered);
else await liveRun(ordered, CASES.cases);
