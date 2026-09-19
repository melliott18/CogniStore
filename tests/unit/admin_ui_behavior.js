"use strict";
// Execute production code against a DOM boundary; HTTP is controlled per scenario.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map();
class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.value = ""; this.disabled = false; this.hidden = false; this.open = false; this._text = ""; }
  set id(value) { nodes.set(value, this); this._id = value; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return [this._text, ...this.children.map((child) => child.textContent)].join(" ").trim(); }
  set innerHTML(_value) { throw new Error("Do not parse server data as HTML"); }
  append(...items) { this.children.push(...items); if (this.tagName === "select" && !this.value) this.value = items[0].value; }
  replaceChildren(...items) { this._text = ""; this.children = items; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  async fire(name) { for (const callback of this.listeners[name] || []) await callback({preventDefault() {}}); }
  reportValidity() { return true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
}
const root = path.resolve(__dirname, "../../cognistore/ui/static");
for (const match of fs.readFileSync(path.join(root, "admin.html"), "utf8").matchAll(/id="([^"]+)"/g)) {
  const element = new Element(); element.id = match[1];
}
global.document = {getElementById: (id) => nodes.get(id), createElement: (tag) => new Element(tag), addEventListener() {}, hidden: false};
if (!global.crypto) global.crypto = require("node:crypto").webcrypto;
for (const file of ["admin.js", "decisions.js"]) vm.runInThisContext(fs.readFileSync(path.join(root, file), "utf8"), {filename: file});
const session = {schema_version: 1, tenant_id: "tenant-a", actor_id: "operator-a", operations: ["get_admin_storage", "list_jobs", "preview_policy", "submit_policy_run", "submit_catalog_scan", "list_audit_events", "list_policy_decisions", "list_repairs", "preview_repair", "submit_repair"]};
const storage = {observed_at: "2026-09-19T00:00:00Z", catalog: {status: "ready"}, queue: {status: "not_configured"}, tiers: [{name: "hot", active: true, driver: "PosixDriver", health: {status: "unverified"}, encryption: {mode: "unknown", key_configured: false}, capabilities: {range_reads: true}, pools: []}]};
const calls = [];
function response(body, code = 200) { return {ok: code < 400, status: code, json: async () => body, headers: {get: () => "request-id"}}; }
function server(handler) { global.fetch = async (url, options) => { calls.push({url, options}); return handler(url, options); }; }
function walk(node, predicate) { return node.children.flatMap((child) => [...(predicate(child) ? [child] : []), ...walk(child, predicate)]); }
function findButton(label, parent = byId("view-body")) { return walk(parent, (node) => node.tagName === "button" && node.textContent === label)[0]; }
function defer() { let resolve; const promise = new Promise((done) => { resolve = done; }); return {promise, resolve}; }
async function main() {
  server((url) => response(url === "/v1/admin/session" ? session : storage));
  byId("access-token").value = "Bearer private-token";
  await connect();
  assert.equal(admin.token, "private-token");
  assert.equal(byId("access-token").value, "");
  assert.equal(calls[0].options.headers.Authorization, "Bearer private-token");
  assert.equal(calls[0].options.cache, "no-store");
  assert.match(byId("tenant-scope").textContent, /tenant-a/);
  assert.match(byId("view-body").textContent, /unverified/);

  // A policy manager can preview but cannot submit a run or catalog scan.
  admin.session = {...session, operations: ["preview_policy"]};
  await showView("policies");
  assert.ok(findButton("Preview object"));
  assert.equal(findButton("Submit policy run"), undefined);
  assert.equal(findButton("Submit catalog scan"), undefined);
  assert.equal(byId("view-body").textContent.includes("Tenant B"), false);

  // A changed policy draft invalidates the preview before enabling a scoped run.
  admin.session = session;
  await showView("policies");
  byId("policy-bucket").value = "docs"; byId("policy-key").value = "one.txt";
  const decision = {bucket: "docs", key: "one.txt", evaluated_at: "now", disposition: "move",
    current: {tier: "hot"}, proposed: {tier: "warm"}, changed_fields: ["tier"],
    explanation: {state: "unavailable"}, execution: {mode: "preview", state: "dry_run"}};
  server(() => response(decision));
  assert.equal(findButton("Submit policy run").disabled, true);
  await findButton("Preview object").fire("click");
  assert.equal(JSON.parse(calls.at(-1).options.body).bucket, "docs");
  assert.match(byId("view-body").textContent, /Dry run · No action executed/);
  assert.equal(findButton("Submit policy run").disabled, false);
  const form = walk(byId("view-body"), (node) => node.tagName === "form")[0];
  byId("policy-config").value = '{"policy":"content"}';
  await form.fire("input");
  assert.equal(findButton("Submit policy run").disabled, true);
  const latePreview = defer(); server(() => latePreview.promise);
  const previewRequest = findButton("Preview object").fire("click");
  byId("policy-config").value = '{"policy":"simple","threshold":42}';
  await form.fire("input"); latePreview.resolve(response(decision)); await previewRequest;
  assert.equal(findButton("Submit policy run").disabled, true);
  assert.match(byId("view-body").textContent, /Inputs changed during preview/);

  // No mutation before explicit confirmation; scope is snapshotted and double submission blocked.
  admin.session = session;
  await showView("policies");
  const payload = {bucket: "documents", prefix: "critical/", config: {policy: "simple"}};
  const before = calls.length;
  confirmAction("Submit policy run", payload, "Tenant tenant-a / documents / critical/*", "/v1/actions/policy-runs");
  payload.prefix = "wrong/";
  assert.equal(calls.length, before);
  assert.match(byId("confirmation-payload").textContent, /critical\//);
  await submitConfirmed({preventDefault() {}});
  assert.equal(calls.length, before);
  const action = defer(); server(() => action.promise);
  byId("confirmation-text").value = "CONFIRM";
  const firstSubmit = submitConfirmed({preventDefault() {}});
  await submitConfirmed({preventDefault() {}});
  assert.equal(calls.length, before + 1);
  assert.equal(JSON.parse(calls.at(-1).options.body).prefix, "critical/");
  assert.ok(calls.at(-1).options.headers["Idempotency-Key"]);
  cancelConfirmation(); assert.equal(byId("confirmation").open, true);
  action.resolve(response({job_id: "job-1", status: "queued"})); await firstSubmit;
  assert.equal(byId("confirmation").open, false);
  assert.match(byId("view-status").textContent, /Follow it in Jobs/);

  // List pagination, escaped values, and stale rows on a partial network failure.
  const malicious = '<img src=x onerror="alert(1)">';
  server((url) => response(url === "/v1/admin/session" ? session : {items: [{job_id: malicious, job_type: "policy.run", status: "queued", updated_at: "now"}], page: {next_cursor: "older cursor"}}));
  await showView("jobs");
  assert.ok(byId("view-body").textContent.includes(malicious));
  assert.equal(walk(byId("view-body"), (node) => node.tagName === "img").length, 0);
  server((url) => response(url === "/v1/admin/session" ? session : {items: [], page: {next_cursor: null}}));
  await loadView(true);
  assert.ok(calls.at(-1).url.includes("cursor=older+cursor"));
  assert.equal(admin.items.length, 1);
  server((url) => { if (url === "/v1/admin/session") return response(session); throw new TypeError("network"); });
  await loadView();
  assert.match(byId("view-status").textContent, /Visible data is stale/);
  assert.ok(byId("view-body").textContent.includes(malicious));

  // An older request cannot restore tenant A data after disconnection or tenant changes.
  const delayed = defer(); server(() => delayed.promise);
  const old = loadView();
  clearWorkspace("Disconnected"); delayed.resolve(response(storage)); await old;
  assert.equal(byId("view-body").children.length, 0);
  assert.equal(admin.token, "");

  // Switching away from a pending read leaves the new view refreshable.
  admin.session = session; admin.view = "jobs";
  const pendingRead = defer(); server(() => pendingRead.promise);
  const loading = loadView();
  assert.equal(byId("refresh-view").disabled, true);
  await showView("policies");
  assert.equal(byId("refresh-view").disabled, false);
  assert.equal(byId("admin-content").attributes["aria-busy"], "false");
  pendingRead.resolve(response(session)); await loading;

  // Session grant changes clear all cached tenant data before another view request.
  admin.session = session; admin.view = "jobs";
  server(() => response({...session, tenant_id: "tenant-b"}));
  await loadView();
  assert.equal(admin.session, null);
  assert.match(byId("session-status").textContent, /tenant or permissions changed/);

  // Both 401 and 403 clear token, controls, confirmation and evidence.
  for (const code of [401, 403]) {
    admin.session = session; admin.token = "secret";
    byId("view-body").textContent = "sensitive-data";
    confirmAction("Repair", {repair_id: "one"}, "tenant-a", "/v1/admin/repairs");
    server(() => response({}, code));
    await assert.rejects(adminRequest("/v1/admin/storage"), {name: "AbortError"});
    assert.equal(admin.token, ""); assert.equal(admin.session, null);
    assert.equal(byId("view-body").textContent, "");
    assert.equal(byId("confirmation-scope").textContent, "");
    assert.equal(byId("confirmation").open, false);
  }

  // Broken reports do not hide healthy siblings; repair history is visible.
  admin.session = session;
  const report = {repair_id: "repair-one", scope: {tenant_id: "tenant-a", bucket: "docs", prefix: "", tiers: ["hot", "warm"]}, status: "ready", counts: {}, history: [{actor_id: "operator-a", status: "running"}]};
  server((url) => response(url === "/v1/admin/session" ? session : {items: [report], errors: [{repair_id: "broken", message: "Repair report unavailable"}]}));
  await showView("repairs");
  assert.match(byId("view-status").textContent, /Partial failure/);
  assert.match(byId("view-body").textContent, /broken/);
  assert.match(byId("view-body").textContent, /operator-a/);
  assert.equal(findButton("Submit repair"), undefined);
  server(() => response({...report, actions: [{action: "resume_move"}], preview_token: "receipt"}));
  await findButton("Preview repair").fire("click");
  assert.ok(findButton("Submit repair"));
  await findButton("Submit repair").fire("click");
  assert.deepEqual(admin.pending.payload.confirmation, report.scope);
  assert.equal(byId("confirmation-payload").textContent.includes("receipt"), false);
  cancelConfirmation();

  // Out-of-order detail inspections leave the latest evidence intact.
  const output = new Element(); const slow = defer();
  const oldDetail = inspect(() => slow.promise, output, (data) => { output.textContent = data; });
  await inspect(async () => "new", output, (data) => { output.textContent = data; });
  slow.resolve("old"); await oldDetail; assert.equal(output.textContent, "new");
  console.log("Administration UI behavior passed");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
