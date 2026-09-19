"use strict";

// The token and operational data live only in this page. No browser persistence.
const admin = {
  token: "", session: null, epoch: 0, view: null, requests: new Set(),
  busy: false, updatedAt: 0, pending: null, cursor: null, items: [], filters: {}, errors: [],
  pausePolling: false,
};
const adminViews = [
  ["storage", "Storage", ["get_admin_storage"]],
  ["policies", "Policies & actions", ["preview_policy", "submit_catalog_scan"]],
  ["jobs", "Jobs", ["list_jobs"]],
  ["audit", "Audit & explanations", ["list_audit_events", "list_policy_decisions"]],
  ["repairs", "Repairs", ["list_repairs"]],
];
function byId(id) { return document.getElementById(id); }
function createElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function addText(parent, tag, className, text) {
  const node = createElement(tag, className, text); parent.append(node); return node;
}
function capitalize(text) { return text.charAt(0).toUpperCase() + text.slice(1); }
function detail(label, value) {
  const row = createElement("div");
  addText(row, "dt", "", label); addText(row, "dd", "", value); return row;
}
function notice(_kind, title, message) {
  const node = createElement("div", "admin-note");
  addText(node, "strong", "", title); addText(node, "p", "", message); return node;
}
function permitted(operation) { return Boolean(admin.session?.operations.includes(operation)); }
function button(parent, label, action, className = "") {
  const node = addText(parent, "button", className, label);
  node.type = "button"; node.addEventListener("click", action); return node;
}
function field(parent, label, id, {value = "", required = false, tag = "input"} = {}) {
  const wrapper = addText(parent, "label", "", label);
  const input = createElement(tag); input.id = id; input.value = value;
  input.required = required; input.maxLength = tag === "textarea" ? 65536 : 8192;
  if (tag === "textarea") { input.rows = 8; input.spellcheck = false; }
  wrapper.append(input); return input;
}
function scopeText() { return `Tenant: ${admin.session?.tenant_id || "unavailable"}`; }
function status(message, state = "ready") {
  byId("view-status").textContent = message;
  byId("view-status").setAttribute("data-state", state);
}
function abortRequests() {
  for (const controller of admin.requests) controller.abort();
  admin.requests.clear(); admin.epoch += 1; admin.busy = false;
}
function clearWorkspace(message) {
  abortRequests(); admin.token = ""; admin.session = null; admin.view = null;
  admin.updatedAt = 0; admin.items = []; admin.cursor = null; admin.pending = null;
  byId("access-token").value = "";
  byId("admin-nav").replaceChildren(); byId("view-body").replaceChildren();
  byId("view-title").textContent = "Connect to begin";
  byId("session-status").textContent = message;
  byId("tenant-scope").textContent = ""; byId("tenant-scope").hidden = true;
  byId("refresh-view").hidden = true; byId("disconnect").hidden = true;
  byId("refresh-view").disabled = false;
  byId("admin-content").setAttribute("aria-busy", "false");
  byId("confirmation-payload").textContent = "";
  byId("confirmation-scope").textContent = "";
  byId("confirmation-text").value = "";
  byId("confirmation-title").textContent = "Confirm action";
  if (byId("confirmation").open) byId("confirmation").close();
  status("");
}
class AdminError extends Error {}
async function adminRequest(path, options = {}) {
  // Only fixed same-origin API paths are passed here, never response-provided URLs.
  const controller = new AbortController(); admin.requests.add(controller);
  const epoch = admin.epoch;
  const timer = setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch(path, {
      ...options, credentials: "same-origin", cache: "no-store", signal: controller.signal,
      headers: {Accept: "application/json", ...(options.body ? {"Content-Type": "application/json"} : {}),
        ...(admin.token ? {Authorization: `Bearer ${admin.token}`} : {}), ...options.headers},
    });
    if (epoch !== admin.epoch) throw new DOMException("Superseded", "AbortError");
    if (response.status === 401 || response.status === 403) {
      clearWorkspace("Access denied or expired. Connect with a currently authorized identity.");
      throw new DOMException("Authorization changed", "AbortError");
    }
    let body;
    try { body = await response.json(); } catch (_error) { throw new AdminError("The service returned an invalid response."); }
    if (epoch !== admin.epoch) throw new DOMException("Superseded", "AbortError");
    if (!response.ok) {
      const requestId = body?.request_id || response.headers.get("X-Request-ID");
      throw new AdminError(`${body?.error?.message || `Request failed (HTTP ${response.status}).`}${requestId ? ` Request ${requestId}.` : ""}`);
    }
    return body;
  } catch (error) {
    if (error.name === "AbortError" && epoch === admin.epoch) {
      throw new AdminError("Request timed out. Refresh to check the latest outcome before retrying an action.");
    }
    throw error;
  } finally { clearTimeout(timer); admin.requests.delete(controller); }
}
async function connect(event) {
  event?.preventDefault();
  const token = byId("access-token").value.trim().replace(/^Bearer\s+/i, "");
  clearWorkspace("Checking your permissions…"); admin.token = token;
  try {
    const session = await adminRequest("/v1/admin/session");
    if (!Array.isArray(session.operations) || !session.tenant_id) throw new AdminError("Invalid session response.");
    admin.session = session;
    byId("session-status").textContent = "Connected. Views and actions reflect your current grants.";
    byId("tenant-scope").textContent = `${scopeText()} · Actor: ${session.actor_id}`;
    byId("tenant-scope").hidden = false; byId("disconnect").hidden = false;
    const views = adminViews.filter(([_id, _label, ops]) => ops.some(permitted));
    for (const [id, label] of views) button(byId("admin-nav"), label, () => showView(id));
    if (views.length) await showView(views[0][0]);
    else status("No administration views are available for your current permissions.");
  } catch (error) {
    if (error.name !== "AbortError") clearWorkspace(error instanceof AdminError ? error.message : "Cannot reach CogniStore. Connect again to retry.");
  }
}
async function showView(view) {
  if (admin.pending || !admin.session) return;
  const item = adminViews.find(([id, _label, ops]) => id === view && ops.some(permitted));
  if (!item) return;
  abortRequests(); admin.view = view; admin.updatedAt = 0; admin.cursor = null;
  admin.items = []; admin.filters = {}; admin.errors = []; admin.pausePolling = false;
  byId("view-title").textContent = item[1]; byId("view-body").replaceChildren();
  byId("refresh-view").hidden = false;
  byId("refresh-view").disabled = false;
  byId("admin-content").setAttribute("aria-busy", "false");
  for (const node of byId("admin-nav").children) node.setAttribute("aria-current", node.textContent === item[1] ? "page" : "false");
  if (view === "policies") { renderPolicies(); status("Draft a configuration, inspect its preview, then submit an allowed action."); return; }
  if (view === "audit") renderAuditFilters();
  await loadView();
}
async function loadView(append = false) {
  if (admin.busy || !admin.session || admin.pending) return;
  if (admin.view === "policies") { await showView("policies"); return; }
  const epoch = admin.epoch; admin.busy = true;
  admin.pausePolling = append;
  byId("admin-content").setAttribute("aria-busy", "true");
  byId("refresh-view").disabled = true;
  status(admin.updatedAt ? "Refreshing… Last successful data remains visible." : "Loading…", "loading");
  try {
    const session = await adminRequest("/v1/admin/session");
    if (JSON.stringify(session) !== JSON.stringify(admin.session)) {
      clearWorkspace("Your tenant or permissions changed. Connect again to load your authorized workspace.");
      return;
    }
    const params = new URLSearchParams({limit: "30", ...admin.filters});
    params.delete("evidence");
    if (append && admin.cursor) params.set("cursor", admin.cursor);
    const path = {storage: "/v1/admin/storage", jobs: `/v1/jobs?${params}`,
      audit: `/v1/${admin.filters.evidence === "decisions" ? "policy-decisions" : "audit/events"}?${params}`,
      repairs: `/v1/admin/repairs?${params}`}[admin.view];
    const data = await adminRequest(path);
    if (admin.view === "storage") renderStorage(data);
    else {
      if (!Array.isArray(data.items)) throw new AdminError("The service returned an invalid list.");
      admin.items = append ? [...admin.items, ...data.items] : data.items;
      admin.errors = append ? [...admin.errors, ...(data.errors || [])] : data.errors || [];
      admin.cursor = data.page?.next_cursor || null;
      renderList(admin.view);
    }
    admin.updatedAt = Date.now();
    const partial = admin.errors.length || (admin.view === "storage" && (
      data.catalog?.status === "unavailable" || data.queue?.status === "unavailable" ||
      data.tiers?.some((tier) => tier.health?.status === "unavailable")));
    status(`${partial ? "Partial failure · Some resources are unavailable. " : ""}Updated ${new Date(admin.updatedAt).toLocaleTimeString()} · ${scopeText()}${append && admin.view === "jobs" ? " · Auto-refresh paused on older records. Refresh to return to the latest jobs." : ""}`, partial ? "error" : "ready");
  } catch (error) {
    if (error.name !== "AbortError") {
      const message = error instanceof AdminError ? error.message : "Cannot reach CogniStore.";
      status(`${message} ${admin.updatedAt ? "Visible data is stale." : "No data loaded."} Refresh to retry.`, "stale");
    }
  } finally {
    if (epoch === admin.epoch) {
      admin.busy = false; byId("refresh-view").disabled = false;
      byId("admin-content").setAttribute("aria-busy", "false");
    }
  }
}
function renderStorage(data) {
  const host = byId("view-body"); host.replaceChildren();
  addText(host, "p", "admin-note", `Catalog: ${data.catalog?.status || "unavailable"} · Job queue: ${data.queue?.status || "unavailable"} · Observed: ${data.observed_at}`);
  addText(host, "p", "admin-note", "Driver health marked unverified has not been probed. Configuration availability does not establish backend reachability. Secrets and connection locations are omitted.");
  const grid = createElement("div", "admin-grid"); host.append(grid);
  for (const tier of data.tiers || []) {
    const card = createElement("article", "admin-card"); grid.append(card);
    addText(card, "h3", "", tier.name);
    addText(card, "p", "", `Driver: ${tier.driver || "Not configured"} · ${tier.active === true ? "Active" : tier.active === false ? "Inactive" : "Activation unknown"}`);
    addText(card, "p", "", `Health: ${tier.health?.status || "unavailable"}`);
    const capabilities = Object.entries(tier.capabilities || {}).map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`).join(" · ");
    addText(card, "p", "admin-note", capabilities || "Capabilities unavailable");
    addText(card, "p", "", `Encryption: ${tier.encryption?.mode || "unavailable"} · Key configured: ${tier.encryption?.key_configured ?? "unknown"}`);
    for (const pool of tier.pools || []) addText(card, "p", "admin-note", `Pool ${pool.pool_id} · ${pool.region || "Region unknown"} · ${pool.member_count} members · ${(pool.localities || []).join(", ")}`);
  }
  if (!grid.children.length) addText(grid, "p", "admin-empty", "No tiers are configured for this tenant.");
}
function parseConfig(input) {
  try {
    const value = JSON.parse(input.value);
    if (!value || Array.isArray(value) || typeof value !== "object") throw new Error();
    return value;
  } catch (_error) { throw new AdminError("Policy configuration must be a valid JSON object."); }
}
function errorText(error) { return error instanceof AdminError ? error.message : "Cannot reach CogniStore. Refresh to check the latest outcome."; }
const inspectionVersions = new WeakMap();
async function inspect(action, output, render) {
  const epoch = admin.epoch;
  const version = (inspectionVersions.get(output) || 0) + 1;
  inspectionVersions.set(output, version);
  output.textContent = "Loading evidence…";
  try { const data = await action(); if (epoch === admin.epoch && inspectionVersions.get(output) === version) { output.replaceChildren(); render(data); } }
  catch (error) { if (epoch === admin.epoch && inspectionVersions.get(output) === version && error.name !== "AbortError") output.textContent = errorText(error); }
}
function renderPolicies() {
  const host = byId("view-body");
  if (permitted("preview_policy")) {
    const form = createElement("form", "admin-form"); host.append(form);
    addText(form, "h3", "", "Policy configuration");
    const grid = createElement("div", "admin-grid"); form.append(grid);
    const bucket = field(grid, "Bucket", "policy-bucket", {required: true});
    const key = field(grid, "Object key for preview", "policy-key", {required: true});
    const prefix = permitted("submit_policy_run") ? field(form, "Run key prefix (empty includes the entire bucket)", "policy-prefix") : null;
    const config = field(form, "Policy JSON", "policy-config", {tag: "textarea", value: '{"policy":"simple","threshold":1048576,"allowed_tiers":["hot","warm"]}'});
    addText(form, "p", "admin-note", "The preview evaluates one existing object. A submitted run evaluates all objects in the bucket and prefix and may move data or remove source copies. Workers recheck authorization and placement safeguards.");
    const controls = createElement("div", "admin-inline"); form.append(controls);
    const preview = button(controls, "Preview object", async () => {
      if (!form.reportValidity()) return;
      let payload;
      try { payload = {bucket: bucket.value, key: key.value, config: parseConfig(config)}; }
      catch (error) { status(errorText(error), "error"); return; }
      const signature = JSON.stringify([bucket.value, key.value, prefix?.value, config.value]);
      preview.disabled = true; if (submit) submit.disabled = true;
      await inspect(() => adminRequest("/v1/policies/preview", {method: "POST", body: JSON.stringify(payload)}), output, (data) => {
        if (signature !== JSON.stringify([bucket.value, key.value, prefix?.value, config.value])) {
          output.textContent = "Inputs changed during preview. Preview again."; return;
        }
        output.append(renderDecision(data));
        if (submit) submit.disabled = false;
      });
      preview.disabled = false;
    });
    const submit = permitted("submit_policy_run") ? button(controls, "Submit policy run", () => {
      if (!form.reportValidity()) return;
      try {
        const payload = {bucket: bucket.value, prefix: prefix.value, config: parseConfig(config)};
        confirmAction("Submit policy run", payload, `${scopeText()} · Bucket: ${payload.bucket} · Prefix: ${payload.prefix || "ALL OBJECTS"}. This can move data and remove source copies. The preview covers one object; the run covers every matching object.`, "/v1/actions/policy-runs");
      } catch (error) { status(errorText(error), "error"); }
    }) : null;
    if (submit) submit.disabled = true;
    const output = createElement("ol", "admin-evidence results-list"); form.append(output);
    form.addEventListener("submit", (event) => event.preventDefault());
    form.addEventListener("input", () => { if (submit) submit.disabled = true; output.replaceChildren(); });
  }
  if (permitted("submit_catalog_scan")) {
    const form = createElement("form", "admin-form"); host.append(form);
    addText(form, "h3", "", "Catalog scan");
    const grid = createElement("div", "admin-grid"); form.append(grid);
    const tier = field(grid, "Tier", "scan-tier", {required: true});
    const bucket = field(grid, "Bucket", "scan-bucket", {required: true});
    const prefix = field(form, "Key prefix (empty includes the entire bucket)", "scan-prefix");
    button(form, "Submit catalog scan", () => {
      if (!form.reportValidity()) return;
      const payload = {tier: tier.value, bucket: bucket.value, prefix: prefix.value};
      confirmAction("Submit catalog scan", payload, `${scopeText()} · Tier: ${payload.tier} · Bucket: ${payload.bucket} · Prefix: ${payload.prefix || "ALL OBJECTS"}. This updates catalog records for the selected scope.`, "/v1/actions/catalog-scans");
    });
    form.addEventListener("submit", (event) => event.preventDefault());
  }
}
function renderAuditFilters() {
  const form = createElement("form", "admin-form"); byId("view-body").append(form);
  const source = field(form, "Evidence", "audit-source", {tag: "select"});
  if (permitted("list_audit_events")) { const option = addText(source, "option", "", "Audit trail"); option.value = "audit"; }
  if (permitted("list_policy_decisions")) { const option = addText(source, "option", "", "Placement explanations"); option.value = "decisions"; }
  const grid = createElement("div", "admin-grid"); form.append(grid);
  const bucket = field(grid, "Bucket filter", "audit-bucket");
  const key = field(grid, "Object key filter", "audit-key");
  const job = field(grid, "Job ID filter", "audit-job");
  const apply = addText(form, "button", "", "Apply filters"); apply.type = "submit";
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); abortRequests(); admin.updatedAt = 0; admin.items = []; admin.cursor = null;
    admin.filters = {evidence: source.value};
    for (const [name, input] of [["bucket", bucket], ["key", key], ["job_id", job]]) if (input.value) admin.filters[name] = input.value;
    byId("list-output")?.replaceChildren(); await loadView();
  });
  admin.filters.evidence = source.value;
  const output = createElement("div", "admin-evidence"); output.id = "list-output"; byId("view-body").append(output);
  if (permitted("verify_audit_integrity")) {
    const verification = createElement("div", "admin-evidence"); byId("view-body").append(verification);
    button(form, "Verify audit integrity", () => inspect(() => adminRequest("/v1/audit/verify", {method: "POST", body: "{}"}), verification, (result) => {
      addText(verification, "h3", "", result.valid ? "Audit integrity verified" : "Audit integrity check failed");
      addText(verification, "p", "admin-note", result.anchored ? "Verified against a checkpoint." : "Internal consistency check only; no external checkpoint supplied.");
      addText(verification, "pre", "", JSON.stringify(result, null, 2));
    }));
  }
}
function renderList(view) {
  let host = view === "audit" ? byId("list-output") : byId("view-body");
  host.replaceChildren();
  for (const error of admin.errors) addText(host, "p", "admin-note", `${error.repair_id || error.job_id || "Record"}: ${error.message}. Refresh to retry this record.`);
  if (!admin.items.length) addText(host, "p", "admin-empty", admin.errors.length
    ? "No records could be loaded on this page. Review the failures above."
    : view === "repairs" ? "No repair reports are registered for this tenant. An operator can register an existing consistency repair report." : "No records found in this tenant and scope.");
  else if (view === "audit" && admin.filters.evidence === "decisions") {
    const list = createElement("ol", "results-list"); host.append(list);
    for (const item of admin.items) list.append(renderDecision(item));
  } else if (view === "repairs") renderRepairs(host);
  else {
    const wrap = createElement("div", "admin-table-wrap"); host.append(wrap);
    const table = createElement("table", "admin-table"); wrap.append(table);
    const titles = view === "jobs" ? ["Job", "Type", "Status", "Updated", "Evidence"] : ["Event", "Outcome", "Scope", "Recorded", "Evidence"];
    const head = createElement("thead"); const headings = createElement("tr");
    for (const title of titles) { const th = addText(headings, "th", "", title); th.scope = "col"; }
    head.append(headings); table.append(head); const body = createElement("tbody"); table.append(body);
    const evidence = createElement("div", "admin-evidence"); host.append(evidence);
    for (const item of admin.items) {
      const row = createElement("tr"); body.append(row);
      const values = view === "jobs" ? [item.job_id, item.job_type, item.status, item.updated_at] : [item.event_type, item.outcome, [item.bucket, item.object_key].filter(Boolean).join("/") || "Tenant", item.recorded_at];
      for (const value of values) addText(row, "td", "", String(value ?? "Unavailable"));
      const cell = createElement("td"); row.append(cell);
      button(cell, "Inspect", () => {
        if (view === "jobs") {
          admin.pausePolling = true;
          status("Auto-refresh paused while inspecting evidence. Refresh to load the latest job history.");
        }
        return inspect(async () => view === "jobs" ? await adminRequest(`/v1/jobs/${encodeURIComponent(item.job_id)}`) : item, evidence, (data) => {
        addText(evidence, "h3", "", view === "jobs" ? "Job status" : "Audit evidence");
        addText(evidence, "pre", "", JSON.stringify(data, null, 2));
        if (view === "jobs" && permitted("list_policy_decisions")) button(evidence, "Placement explanations", () => inspect(() => adminRequest(`/v1/policy-decisions?job_id=${encodeURIComponent(item.job_id)}&limit=100`), evidence, (page) => {
          const list = createElement("ol", "results-list"); evidence.append(list);
          for (const decision of page.items) list.append(renderDecision(decision));
          if (!page.items.length) addText(evidence, "p", "admin-empty", "No retained decisions for this job.");
          if (page.page?.next_cursor) addText(evidence, "p", "admin-note", "More decisions are available. Filter by this job in Audit & explanations to paginate the full history.");
        }));
        });
      });
    }
  }
  if (admin.cursor) {
    const more = button(host, "Load older records", async () => { more.disabled = true; await loadView(true); more.disabled = false; }, "admin-more");
  }
}
function renderRepairs(host) {
  addText(host, "p", "admin-note", "Repairs use operator-registered reports. Preview checks current eligibility; submission revalidates source generations, retained copies, legal holds, and tenant scope.");
  for (const report of admin.items) {
    const card = createElement("article", "admin-card admin-evidence"); host.append(card);
    addText(card, "h3", "", report.repair_id);
    addText(card, "p", "", `Status: ${report.status || "available"} · Bucket: ${report.scope?.bucket || "ALL"} · Prefix: ${report.scope?.prefix || "ALL OBJECTS"} · Tiers: ${(report.scope?.tiers || []).join(", ")}`);
    addText(card, "p", "admin-note", Object.entries(report.counts || {}).map(([name, count]) => `${capitalize(name)}: ${count}`).join(" · ") || "No repair attempts recorded.");
    if (report.history?.length) {
      const history = createElement("details"); card.append(history);
      addText(history, "summary", "", "Repair attempt history");
      addText(history, "pre", "", JSON.stringify(report.history, null, 2));
    }
    const output = createElement("div", "admin-evidence");
    if (permitted("preview_repair")) button(card, "Preview repair", async () => {
      await inspect(() => adminRequest("/v1/admin/repairs/preview", {method: "POST", body: JSON.stringify({repair_id: report.repair_id})}), output, (preview) => {
        addText(output, "p", "admin-note", "Plan only · No repair executed. Review every action and its scope before submitting.");
        addText(output, "p", "", Object.entries(preview.counts || {}).map(([name, count]) => `${capitalize(name)}: ${count}`).join(" · ") || "No repair actions needed.");
        const wrap = createElement("div", "admin-table-wrap"); output.append(wrap);
        const table = createElement("table", "admin-table"); wrap.append(table);
        const head = createElement("thead"); const headings = createElement("tr");
        for (const title of ["Object", "Current → proposed tier", "Action", "Outcome / reason"]) {
          const th = addText(headings, "th", "", title); th.scope = "col";
        }
        head.append(headings); table.append(head); const body = createElement("tbody"); table.append(body);
        for (const action of preview.actions || []) {
          const row = createElement("tr"); body.append(row);
          for (const value of [`${action.bucket || preview.scope.bucket}/${action.key || ""}`,
            `${action.source_tier || "Unavailable"} → ${action.destination_tier || "Unavailable"}`,
            decisionLabel(action.action), `${decisionLabel(action.status)} · ${decisionLabel(action.reason)}`]) addText(row, "td", "", value);
        }
        const evidence = createElement("details"); output.append(evidence);
        addText(evidence, "summary", "", "Full repair evidence");
        addText(evidence, "pre", "", JSON.stringify({scope: preview.scope, counts: preview.counts, actions: preview.actions}, null, 2));
        if (permitted("submit_repair") && preview.preview_token) button(output, "Submit repair", () => {
          const payload = {repair_id: preview.repair_id, preview_token: preview.preview_token, confirmation: preview.scope};
          confirmAction("Submit repair", payload, `${scopeText()} · Report: ${preview.repair_id} · Bucket: ${preview.scope.bucket || "ALL"} · Prefix: ${preview.scope.prefix || "ALL OBJECTS"} · Tiers: ${(preview.scope.tiers || []).join(", ")}. Repairs may finish moves, reconcile records, or delete stale source copies.`, "/v1/admin/repairs");
        });
      });
    });
    card.append(output);
  }
}
function confirmAction(title, payload, scope, path) {
  if (admin.pending || !admin.session) return;
  admin.pending = {title, payload: JSON.parse(JSON.stringify(payload)), path, epoch: admin.epoch, key: crypto.randomUUID(), submitting: false};
  byId("confirmation-title").textContent = title;
  byId("confirmation-scope").textContent = scope;
  // The repair preview token is an internal receipt, not useful to an operator.
  const displayed = {...payload}; delete displayed.preview_token;
  byId("confirmation-payload").textContent = JSON.stringify(displayed, null, 2);
  byId("confirmation-text").value = ""; byId("confirm-action").disabled = false;
  byId("cancel-action").disabled = false; byId("confirmation").showModal();
  byId("confirmation-text").focus();
}
function cancelConfirmation() {
  if (admin.pending?.submitting) return;
  admin.pending = null; byId("confirmation").close();
  byId("confirmation-payload").textContent = "";
}
async function submitConfirmed(event) {
  event.preventDefault();
  const pending = admin.pending;
  if (!pending || pending.submitting || pending.epoch !== admin.epoch || byId("confirmation-text").value !== "CONFIRM") return;
  pending.submitting = true; byId("confirm-action").disabled = true; byId("cancel-action").disabled = true;
  try {
    const result = await adminRequest(pending.path, {method: "POST", body: JSON.stringify(pending.payload), headers: {"Idempotency-Key": pending.key}});
    if (pending.epoch !== admin.epoch) return;
    admin.pending = null; byId("confirmation").close();
    byId("confirmation-payload").textContent = "";
    status(`${pending.title} accepted. ${result.job_id ? `Job ${result.job_id} · ${result.status}. Follow it in Jobs.` : `Repair ${result.repair_id} · ${result.status || "completed"}. Refresh Repairs and review audit evidence.`}`);
    const output = createElement("div", "admin-evidence");
    addText(output, "h3", "", "Action result"); addText(output, "pre", "", JSON.stringify(result, null, 2));
    byId("view-body").append(output);
  } catch (error) {
    if (pending.epoch === admin.epoch && error.name !== "AbortError") {
      // Do not silently repeat a potentially accepted action after a network failure.
      admin.pending = null; byId("confirmation").close();
      byId("confirmation-payload").textContent = "";
      status(`${errorText(error)} Check Jobs, Repairs, or Audit before submitting another action.`, "error");
    }
  }
}
function initializeAdmin() {
  byId("connect-form").addEventListener("submit", connect);
  byId("disconnect").addEventListener("click", () => clearWorkspace("Disconnected. Connect to resume."));
  byId("refresh-view").addEventListener("click", () => loadView());
  byId("confirmation-form").addEventListener("submit", submitConfirmed);
  byId("cancel-action").addEventListener("click", cancelConfirmation);
  byId("confirmation").addEventListener("cancel", (event) => { event.preventDefault(); cancelConfirmation(); });
  setInterval(() => {
    if (!admin.session || document.hidden || admin.pending) return;
    if (admin.updatedAt && Date.now() - admin.updatedAt > 60000) status("Visible data is stale (over one minute old). Refresh to verify current state.", "stale");
    if (admin.view === "jobs" && !admin.pausePolling && admin.items.some((job) => ["queued", "running", "retrying"].includes(job.status))) loadView();
  }, 10000);
  connect();
}
document.addEventListener("DOMContentLoaded", initializeAdmin);
