"use strict";

const decisionState = { controller: null, historyUrl: null, nextCursor: null };

function initializeDecisions() {
  byId("decision-form").addEventListener("submit", submitDecision);
  byId("decision-mode").addEventListener("change", updateDecisionMode);
  byId("decision-more").addEventListener("click", () => loadDecisionHistory(true));
  byId("decision-config").addEventListener("input", () => {
    byId("decision-config").setCustomValidity("");
  });
  byId("decision-form").addEventListener("input", () => {
    cancelDecisionRequest();
    decisionState.historyUrl = null;
    decisionState.nextCursor = null;
    byId("decision-more").hidden = true;
  });
  updateDecisionMode();
}

function updateDecisionMode() {
  cancelDecisionRequest();
  const mode = byId("decision-mode").value;
  const job = mode === "job";
  const preview = mode === "preview";
  byId("decision-object-fields").hidden = job;
  byId("decision-job-field").hidden = !job;
  byId("decision-config-field").hidden = !preview;
  for (const id of ["decision-bucket", "decision-key"]) {
    byId(id).disabled = job;
    byId(id).required = !job;
  }
  byId("decision-job").disabled = !job;
  byId("decision-job").required = job;
  byId("decision-config").disabled = !preview;
  byId("submit-decision").textContent = preview ? "Preview without moving data" : "Inspect history";
  byId("decision-help").textContent = preview
    ? "Dry run only. This evaluates the configuration below without creating a job or moving data."
    : "Inspect retained decisions and their latest execution outcomes. Submit again to refresh job status.";
  resetDecisionOutput();
}

function resetDecisionOutput() {
  byId("decision-results").replaceChildren();
  byId("decision-status").textContent = "Choose an object or job to inspect, or run a dry-run preview.";
  byId("decision-more").hidden = true;
  decisionState.historyUrl = null;
  decisionState.nextCursor = null;
}

function setDecisionBusy(busy) {
  byId("submit-decision").disabled = busy;
  byId("decision-more").disabled = busy;
  byId("decision-form").setAttribute("aria-busy", String(busy));
  byId("decision-results").setAttribute("aria-busy", String(busy));
}

function cancelDecisionRequest() {
  if (decisionState.controller) {
    decisionState.controller.abort();
    decisionState.controller = null;
    setDecisionBusy(false);
    byId("decision-status").textContent = "Request cancelled. Submit again to inspect placement.";
  }
}

async function inspectObjectDecisions(citation) {
  setView("placements");
  byId("decision-mode").value = "object";
  updateDecisionMode();
  byId("decision-bucket").value = citation.bucket || "";
  byId("decision-key").value = citation.key || "";
  await submitDecision({ preventDefault() {} });
}

async function submitDecision(event) {
  event.preventDefault();
  if (!byId("decision-form").reportValidity()) {
    return;
  }
  cancelDecisionRequest();
  const mode = byId("decision-mode").value;
  if (mode === "preview") {
    const input = byId("decision-config");
    let config;
    try {
      config = JSON.parse(input.value);
      if (!config || typeof config !== "object" || Array.isArray(config)) {
        throw new Error("object required");
      }
    } catch (_error) {
      input.setCustomValidity("Preview policy must be a valid JSON object.");
      input.reportValidity();
      resetDecisionOutput();
      byId("decision-status").textContent = "Preview policy must be a valid JSON object.";
      return;
    }
    resetDecisionOutput();
    await fetchDecisions("/v1/policies/preview", {
      method: "POST",
      body: JSON.stringify({
        bucket: byId("decision-bucket").value,
        key: byId("decision-key").value,
        config,
      }),
    }, false, true);
    return;
  }
  resetDecisionOutput();
  const query = new URLSearchParams({ limit: "20" });
  if (mode === "job") {
    query.set("job_id", byId("decision-job").value.trim());
  } else {
    query.set("bucket", byId("decision-bucket").value);
    query.set("key", byId("decision-key").value);
  }
  decisionState.historyUrl = `/v1/policy-decisions?${query}`;
  await loadDecisionHistory(false);
}

async function loadDecisionHistory(append) {
  if (!decisionState.historyUrl || (append && !decisionState.nextCursor)) {
    return;
  }
  const cursor = append ? `&cursor=${encodeURIComponent(decisionState.nextCursor)}` : "";
  await fetchDecisions(`${decisionState.historyUrl}${cursor}`, { method: "GET" }, append, false);
}

async function fetchDecisions(url, options, append, preview) {
  cancelDecisionRequest();
  const controller = new AbortController();
  decisionState.controller = controller;
  setDecisionBusy(true);
  byId("decision-status").textContent = preview ? "Evaluating dry-run preview…" : "Loading placement history…";
  byId("decision-more").hidden = true;
  try {
    const response = await fetch(url, {
      ...options,
      headers: { "Accept": "application/json", ...(preview ? { "Content-Type": "application/json" } : {}) },
      signal: controller.signal,
    });
    const body = await readJson(response);
    if (decisionState.controller !== controller) {
      return;
    }
    if (!response.ok) {
      throw new APIError(
        body?.error?.message || `The service returned HTTP ${response.status}.`,
        body?.request_id || response.headers.get("X-Request-ID"),
      );
    }
    if (!body || (preview ? !body.execution : !Array.isArray(body.items))) {
      throw new APIError("The service returned an invalid placement response.");
    }
    const items = preview ? [body] : body.items;
    if (!append) {
      byId("decision-results").replaceChildren();
    }
    items.forEach((decision) => byId("decision-results").append(renderDecision(decision)));
    decisionState.nextCursor = preview ? null : body.page?.next_cursor;
    byId("decision-more").hidden = !decisionState.nextCursor;
    const count = byId("decision-results").children.length;
    byId("decision-status").textContent = preview
      ? "Dry-run preview · No job created and no data moved."
      : count ? `${count} retained ${count === 1 ? "decision" : "decisions"}. Execution status reflects this request.`
        : "No retained decisions found. A dry-run preview can explain a policy against this object's current placement.";
  } catch (error) {
    if (error.name === "AbortError" || decisionState.controller !== controller) {
      return;
    }
    const message = error instanceof APIError ? error.message : "CogniStore could not be reached. Try again.";
    byId("decision-status").textContent = error.requestId ? `${message} Request ${error.requestId}` : message;
    // An older-page request can be retried without discarding the visible history.
    byId("decision-more").hidden = !append || !decisionState.nextCursor;
  } finally {
    if (decisionState.controller === controller) {
      decisionState.controller = null;
      setDecisionBusy(false);
    }
  }
}

function decisionValue(value) {
  if (value === null || value === undefined) {
    return "Unavailable";
  }
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function decisionLabel(value) {
  return capitalize(String(value || "unavailable").replaceAll("_", " "));
}

function renderDecision(decision) {
  const card = createElement("li", "result-card decision-card");
  const execution = decision.execution || {};
  const dryRun = execution.mode === "preview" || execution.state === "dry_run";
  const labels = {
    dry_run: "Dry run · No action executed",
    not_requested: "Recorded decision · No move requested",
    planned: "Move planned · Not completed",
    running: "Move running · Not completed",
    retrying: "Move retrying · Not completed",
    completed: "Move completed",
    failed: "Move failed · Proposed placement is not confirmation",
    unavailable: "Execution outcome unavailable",
  };
  addText(card, "p", `decision-state ${dryRun ? "decision-preview" : "decision-recorded"}`,
    dryRun ? labels.dry_run : labels[execution.state] || labels.unavailable);
  addText(card, "h3", "", `${decision.bucket}/${decision.key}`);
  addText(card, "p", "decision-time", `Evaluated ${decisionValue(decision.evaluated_at)}`);

  const placements = createElement("table", "placement-diff");
  addText(placements, "caption", "", "Placement at decision time and proposed outcome");
  const headings = createElement("tr");
  ["Field", "Before decision", "Proposed"].forEach((label) => {
    const heading = addText(headings, "th", "", label);
    heading.scope = "col";
  });
  const head = createElement("thead");
  head.append(headings);
  const row = createElement("tr");
  const field = addText(row, "th", "", "Tier");
  field.scope = "row";
  addText(row, "td", "", decisionValue(decision.current?.tier));
  addText(row, "td", (decision.changed_fields || []).includes("tier") ? "placement-changed" : "", decisionValue(decision.proposed?.tier));
  const body = createElement("tbody");
  body.append(row);
  placements.append(head, body);
  card.append(placements);
  const changed = decision.changed_fields || [];
  addText(card, "p", "decision-change", changed.length
    ? `Changed fields: ${changed.map(decisionLabel).join(", ")} (proposal)`
    : decision.proposed?.tier == null ? "Placement diff unavailable" : "No placement fields changed");
  addText(card, "p", "decision-disposition", `Decision: ${decisionLabel(decision.disposition)}`);

  renderDecisionExplanation(card, decision.explanation || {});
  const trace = createElement("dl", "citation-grid decision-trace");
  for (const [label, value] of [
    ["Decision ID", decision.decision_id], ["Job ID", execution.job_id],
    ["Job outcome", execution.job?.status], ["Job error", execution.job?.error_type],
    ["Job retryable", execution.job?.retryable],
    ["Move ID", execution.move_id], ["Correlation ID", execution.correlation_id],
    ["Execution updated", execution.updated_at],
  ]) {
    if (value !== null && value !== undefined) {
      trace.append(detail(label, decisionValue(value)));
    }
  }
  card.append(trace);
  return card;
}

function renderDecisionExplanation(card, explanation) {
  const reason = explanation.structured_reason;
  if (!reason) {
    card.append(notice("warning", "Explanation unavailable", explanation.state === "legacy"
      ? "This legacy decision has no retained structured reasons. Its execution outcome is shown separately."
      : "Structured rule and constraint evidence could not be loaded for this decision."));
    return;
  }
  addText(card, "h4", "decision-section-title", "Policy reason");
  addText(card, "p", "", decisionLabel(reason.code));
  const policy = reason.policy || {};
  addText(card, "p", "decision-time", `${decisionValue(policy.name)} · Version ${decisionValue(policy.version)}`);

  addText(card, "h4", "decision-section-title", "Decisive signals");
  const signals = createElement("ul", "decision-signals");
  (reason.decisive_signals || []).forEach((signal) => {
    const comparison = signal.operator && signal.threshold != null
      ? ` ${signal.operator} ${decisionValue(signal.threshold)}` : "";
    const rule = signal.rule_index != null ? ` · Rule ${signal.rule_index + 1}` : "";
    addText(signals, "li", "", `${decisionLabel(signal.name)}: ${decisionValue(signal.value)}${comparison}${rule}`);
  });
  card.append(signals);
  if (!signals.children.length) {
    addText(card, "p", "decision-time", "No decisive signals were retained for this decision.");
  }

  addText(card, "h4", "decision-section-title", "Guardrail outcomes");
  const constraints = reason.constraints || {};
  const checks = createElement("dl", "citation-grid decision-constraints");
  const constraintValues = [
    ["Outcome", decisionLabel(reason.disposition)],
    ["Allowed destinations", constraints.allowed_destination_tiers],
    ["Importance", constraints.importance_level ?? "Not tagged"],
    ["Importance allowed tiers", constraints.importance_allowed_tiers],
    ["Residency active", constraints.residency_active],
    ["Minimum residency (seconds)", constraints.minimum_residency_seconds],
    ["Residency expires", constraints.residency_expires_at],
    ["Cooldown active", constraints.cooldown_active],
    ["Cooldown (seconds)", constraints.cooldown_seconds],
    ["Cooldown expires", constraints.cooldown_expires_at],
    ["Stability override", constraints.stability_override_kind],
    ["Suppressed candidate tier", constraints.candidate_destination_tier],
    ["Rejected destination", constraints.rejected_destination_tier],
  ];
  constraintValues.forEach(([label, value]) => {
    if (value !== null && value !== undefined) {
      checks.append(detail(label, decisionValue(value)));
    }
  });
  card.append(checks);
  (constraints.hysteresis_checks || []).forEach((check) => {
    addText(card, "p", "decision-time", `${decisionLabel(check.kind)} hysteresis: value ${decisionValue(check.value)}, baseline ${decisionValue(check.baseline_threshold)}, effective threshold ${decisionValue(check.effective_threshold)}, band ${decisionValue(check.configured_band)}`);
  });

  addText(card, "h4", "decision-section-title", "Model details");
  if (explanation.model_details === "available" && policy.model) {
    addText(card, "p", "decision-time", `${decisionValue(policy.model.identity)} · Version ${decisionValue(policy.model.version)}`);
  } else {
    addText(card, "p", "decision-time", explanation.model_details === "not_applicable"
      ? "Not applicable to this rule-based decision."
      : "Model details unavailable. Retained rule reasons and guardrail evidence are shown above.");
  }
  addText(card, "p", "decision-time", reason.confidence?.value == null
    ? `Confidence unavailable · ${decisionLabel(reason.confidence?.source)}`
    : `Confidence: ${decisionValue(reason.confidence.value)}`);
}
