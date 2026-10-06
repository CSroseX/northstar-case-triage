#!/usr/bin/env node
// Read-only exploration of the Northstar APIs.
// Calls each GET route once and saves the raw response body to exploration/raw/.
// Secrets are read from .env at runtime and are never printed or written to disk.
// NO POST calls to Northstar are made by this script.

import { readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const projectRoot = join(here, "..");
const rawDir = join(here, "raw");

const BASE = "https://hiring.celecolabs.com/api/assessment";

// .env in this project uses "Key:value" lines, not KEY=value, so parse it by hand.
async function loadSecrets() {
  const text = await readFile(join(projectRoot, ".env"), "utf8");
  const out = {};
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const idx = trimmed.indexOf(":");
    if (idx === -1) continue;
    out[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
  }
  const northstar = out.API_Access_Key;
  if (!northstar) throw new Error("API_Access_Key missing from .env");
  return { northstar };
}

// Guard: never let a key reach a file, even by accident.
function assertNoSecrets(text, secrets) {
  for (const value of Object.values(secrets)) {
    if (value && text.includes(value)) throw new Error("refusing to write: response echoed a credential");
  }
  if (/cel_(northstar|model)_[A-Za-z0-9_-]{10,}/.test(text)) {
    throw new Error("refusing to write: credential-shaped string in payload");
  }
}

async function getOnce(route, params, secrets) {
  const url = new URL(BASE);
  url.searchParams.set("route", route);
  for (const [k, v] of Object.entries(params || {})) url.searchParams.set(k, v);

  const started = Date.now();
  const response = await fetch(url, {
    headers: { authorization: `Bearer ${secrets.northstar}`, accept: "application/json" },
    signal: AbortSignal.timeout(30_000),
  });
  const body = await response.text();
  return {
    route,
    params: params || {},
    requestUrl: url.toString(),
    httpStatus: response.status,
    durationMs: Date.now() - started,
    contentType: response.headers.get("content-type"),
    body,
  };
}

// One call per read endpoint, as scoped.
const calls = [
  ["requests", null, "requests"],
  ["customers", null, "customers"],
  ["agreements", null, "agreements"],
  ["technicians", null, "technicians"],
  ["work-order-history", null, "work-order-history"],
  ["work-orders", null, "work-orders"],
];

async function main() {
  const secrets = await loadSecrets();
  await mkdir(rawDir, { recursive: true });
  const index = [];

  for (const [route, params, filename] of calls) {
    let record;
    try {
      record = await getOnce(route, params, secrets);
    } catch (error) {
      record = { route, params: params || {}, error: String(error.message || error) };
    }

    const serialised = JSON.stringify(record, null, 2);
    assertNoSecrets(serialised, secrets);

    // Save the raw body verbatim; pretty-print when it parses as JSON.
    let pretty = record.body;
    let parsed = null;
    if (typeof record.body === "string") {
      try {
        parsed = JSON.parse(record.body);
        pretty = JSON.stringify(parsed, null, 2);
      } catch {
        /* keep raw text */
      }
    }
    if (pretty) {
      assertNoSecrets(pretty, secrets);
      await writeFile(join(rawDir, `${filename}.json`), pretty, "utf8");
    }
    await writeFile(
      join(rawDir, `${filename}.meta.json`),
      JSON.stringify({ ...record, body: undefined }, null, 2),
      "utf8"
    );

    const count = Array.isArray(parsed?.results) ? parsed.results.length : null;
    index.push({ route, httpStatus: record.httpStatus ?? null, durationMs: record.durationMs ?? null, resultCount: count, error: record.error ?? null });
    console.log(`${route}: HTTP ${record.httpStatus ?? "ERR"}${count === null ? "" : ` · ${count} results`}${record.error ? ` · ${record.error}` : ""}`);
  }

  await writeFile(join(here, "index.json"), JSON.stringify({ generatedAt: new Date().toISOString(), base: BASE, calls: index }, null, 2), "utf8");
  console.log(`\nSaved to ${rawDir}`);
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
