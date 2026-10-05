#!/usr/bin/env node

const requiredFields = ["caseId", "status", "entities", "classification", "entitlement", "nextActions", "customerResponseDraft", "missingInformation", "workOrder", "audit"];
const cases = [
  ["VIS-001", "covered_action", { requestId: "REQ-V001", receivedAt: "2026-09-20T08:42:00Z", channel: "portal", sender: { name: "Kavya Menon", email: "facilities@asterwarehousing.in" }, subject: "Main DG scheduled service", body: "Please arrange the covered quarterly service for Main DG at our Whitefield warehouse. The asset label reads AST-101.", attachments: [] }],
  ["VIS-002", "clarification_required", { requestId: "REQ-V002", receivedAt: "2026-09-20T08:57:00Z", channel: "email", sender: { name: "Deepa Kulkarni", email: "ops@bluepeakcoldchain.in" }, subject: "Hoskote freezer temperature rising", body: "The freezer plant at our Hoskote cold store is not pulling down below -12°C. I do not have the asset number.", attachments: [] }],
  ["VIS-003", "human_escalation_required", { requestId: "REQ-V003", receivedAt: "2026-09-20T09:04:00Z", channel: "email", sender: { name: "Farhan Ali", email: "admin@meridiandiagnostics.in" }, subject: "Fuel smell near backup generator", body: "Security reports a strong diesel smell and unusual vibration near the backup generator at our Indiranagar centre.", attachments: [] }],
  ["VIS-004", "account_review_required", { requestId: "REQ-V004", receivedAt: "2026-09-20T09:18:00Z", channel: "portal", sender: { name: "Madhav Shetty", email: "maintenance@helixcomponents.in" }, subject: "Compressor not listed in portal", body: "Please dispatch someone for compressor CMP-77 on line 2. I cannot find it in our covered asset list.", attachments: [] }],
  ["VIS-005", "duplicate_detected", { requestId: "REQ-V005-B", receivedAt: "2026-09-20T09:24:00Z", channel: "email", sender: { name: "Nishant Rao", email: "warehouselead@asterwarehousing.in" }, subject: "Following up: Main DG service", body: "Kavya raised the Main DG service request from the portal a few minutes ago. This email is for the same AST-101 visit.", attachments: [] }],
  ["VIS-006", "dispatch_ready", { requestId: "REQ-V006", receivedAt: "2026-09-20T09:31:00Z", channel: "portal", sender: { name: "Deepa Kulkarni", email: "ops@bluepeakcoldchain.in" }, subject: "Freezer plant 2 stopped", body: "AST-302 at SITE-021 has stopped cooling. There is no smoke, water or unusual smell.", attachments: [] }],
  ["VIS-007", "resource_escalation_required", { requestId: "REQ-V007", receivedAt: "2026-09-20T09:38:00Z", channel: "portal", sender: { name: "Sushma Rao", email: "facilities@asterclinical.in" }, subject: "Electrical-certified technician needed", body: "AST-205 at the Whitefield clinic needs an electrical-certified generator technician today. No one is in danger.", attachments: [] }],
  ["VIS-008", "covered_action", { requestId: "REQ-V008", receivedAt: "2026-09-20T09:46:00Z", channel: "email", sender: { name: "Kavya Menon", email: "facilities@asterwarehousing.in" }, subject: "Coverage confirmation for AST-101", body: "Please confirm coverage for AST-101 using the amendment signed in August, not the older base contract.", attachments: [] }],
];

export function evaluateCaseResult(expectedStatus, result) {
  const failures = requiredFields.filter((field) => !(field in (result || {}))).map((field) => `missing ${field}`);
  if (result?.status !== expectedStatus) failures.push(`expected ${expectedStatus}, received ${result?.status || "no status"}`);
  if (typeof result?.classification?.safetyRisk !== "boolean") failures.push("classification.safetyRisk must be boolean");
  if (!Array.isArray(result?.nextActions)) failures.push("nextActions must be an array");
  if (!Array.isArray(result?.audit?.sourceReferences)) failures.push("audit.sourceReferences must be an array");
  return failures;
}

async function main() {
  const baseUrl = String(process.argv[2] || "").replace(/\/$/, "");
  if (!/^https?:\/\//.test(baseUrl)) throw new Error("Usage: node celeco-visible-runner.mjs http://localhost:8080");
  const health = await fetch(`${baseUrl}/health`, { signal: AbortSignal.timeout(10_000) });
  if (!health.ok) throw new Error(`GET /health returned HTTP ${health.status}`);
  let passed = 0;
  for (const [id, expectedStatus, request] of cases) {
    try {
      const response = await fetch(`${baseUrl}/cases/process`, { method: "POST", headers: { "content-type": "application/json", "x-event-id": `visible-${id.toLowerCase()}` }, body: JSON.stringify(request), signal: AbortSignal.timeout(120_000) });
      const result = await response.json().catch(() => null);
      const failures = response.ok ? evaluateCaseResult(expectedStatus, result) : [`HTTP ${response.status}`];
      if (failures.length) console.log(`FAIL ${id}: ${failures.join("; ")}`);
      else { console.log(`PASS ${id}: ${expectedStatus}`); passed += 1; }
    } catch (error) { console.log(`FAIL ${id}: ${error.message}`); }
  }
  console.log(`\n${passed}/${cases.length} visible cases passed.`);
  process.exitCode = passed === cases.length ? 0 : 1;
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) main().catch((error) => { console.error(error.message); process.exitCode = 1; });
