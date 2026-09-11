"use strict";

const API_PATH = "/v1/ask";
const HYBRID_MODE = "metadata+keyword+vector";

const state = {
  view: "search",
  searchMode: "keyword",
  controller: null,
};

const ui = {};

function byId(id) {
  return document.getElementById(id);
}

function createElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

function addText(parent, tag, className, text) {
  const node = createElement(tag, className, text);
  parent.append(node);
  return node;
}

function initialize() {
  Object.assign(ui, {
    form: byId("query-form"),
    query: byId("query-text"),
    queryLabel: byId("query-label"),
    description: byId("view-description"),
    searchModes: byId("search-modes"),
    submit: byId("submit-query"),
    submitLabel: document.querySelector(".submit-label"),
    status: byId("query-status"),
    summary: byId("result-summary"),
    results: byId("results-list"),
    notices: byId("provider-notices"),
    answer: byId("answer-region"),
    filterCount: byId("filter-count"),
    serviceState: byId("service-state"),
    exactOption: byId("exact-vector-option"),
    exactVector: byId("exact-vector"),
  });

  document.querySelectorAll("[data-view]").forEach((tab) => {
    tab.addEventListener("click", () => setView(tab.dataset.view));
    tab.addEventListener("keydown", handleTabKeydown);
  });
  document.querySelectorAll('input[name="search-mode"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked && input.value !== state.searchMode) {
        cancelPendingRequest();
        state.searchMode = input.value;
        updateModeDetails();
        showIdle();
      }
    });
  });
  document.querySelectorAll(".filter-drawer input, .filter-drawer textarea").forEach((input) => {
    input.addEventListener("input", updateFilterCount);
  });
  byId("clear-filters").addEventListener("click", clearFilters);
  ui.form.addEventListener("submit", submitQuery);

  updateModeDetails();
  updateFilterCount();
  checkHealth();
  initializeDecisions();
}

function handleTabKeydown(event) {
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) {
    return;
  }
  event.preventDefault();
  const views = ["search", "ask", "placements"];
  const direction = event.key === "ArrowRight" ? 1 : -1;
  const next = views[(views.indexOf(state.view) + direction + views.length) % views.length];
  setView(next);
  document.querySelector(`[data-view="${next}"]`).focus();
}

function setView(view) {
  if (!['search', 'ask', 'placements'].includes(view) || view === state.view) {
    return;
  }
  cancelPendingRequest();
  cancelDecisionRequest();
  state.view = view;
  document.querySelectorAll("[data-view]").forEach((tab) => {
    const selected = tab.dataset.view === view;
    tab.classList.toggle("is-active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  byId("query-view").setAttribute(
    "aria-labelledby",
    view === "search" ? "search-tab" : "ask-tab",
  );
  const placements = view === "placements";
  ui.form.hidden = placements;
  byId("placement-view").hidden = !placements;
  byId("search-results-panel").hidden = placements;
  byId("placement-results-panel").hidden = !placements;
  updateModeDetails();
  showIdle();
  (placements ? byId("decision-mode") : ui.query).focus();
}

function cancelPendingRequest() {
  if (!state.controller) {
    return;
  }
  const controller = state.controller;
  state.controller = null;
  controller.abort();
  setSubmitting(false);
}

function updateModeDetails() {
  if (state.view === "placements") {
    ui.description.textContent = "Inspect policy reasons and before/after placement, or preview a policy without moving data.";
    return;
  }
  const isAsk = state.view === "ask";
  ui.searchModes.hidden = isAsk;
  ui.queryLabel.textContent = isAsk ? "Ask a grounded question" : "Search your content";
  ui.description.textContent = isAsk
    ? "Ask across catalog, keyword, and vector signals. Every answer stays attached to inspectable evidence."
    : "Search extracted document content with a ranked keyword or vector query.";
  ui.submitLabel.textContent = isAsk ? "Ask" : "Search";
  ui.query.placeholder = isAsk
    ? "Try “How do operators prove that backups can restore service?”"
    : state.searchMode === "vector"
      ? "Try “How can we recover service after an outage?”"
      : "Try “quarterly restore drill”";
  ui.exactOption.hidden = isAsk || state.searchMode !== "vector";
  if (ui.exactOption.hidden) {
    ui.exactVector.checked = false;
  }
}

function activeFilterInputs() {
  return [
    byId("filter-bucket"),
    byId("filter-prefix"),
    byId("filter-tier"),
    byId("filter-mime"),
    byId("filter-size"),
    byId("filter-sha"),
    byId("filter-object-metadata"),
    byId("filter-document-metadata"),
  ];
}

function updateFilterCount() {
  const count = activeFilterInputs().filter((input) => input.value.trim() !== "").length;
  ui.filterCount.textContent = String(count);
  ui.filterCount.hidden = count === 0;
}

function clearFilters() {
  activeFilterInputs().forEach((input) => {
    input.value = "";
    input.setCustomValidity("");
  });
  ui.exactVector.checked = false;
  updateFilterCount();
  byId("filter-bucket").focus();
}

function parseMetadata(input, label) {
  const value = input.value.trim();
  input.setCustomValidity("");
  if (!value) {
    return {};
  }
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch (_error) {
    throw new FilterError(input, `${label} must be valid JSON.`);
  }
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new FilterError(input, `${label} must be a JSON object.`);
  }
  for (const item of Object.values(parsed)) {
    const scalar = item === null || ["string", "number", "boolean"].includes(typeof item);
    if (!scalar || (typeof item === "number" && !Number.isFinite(item))) {
      throw new FilterError(input, `${label} values must be strings, numbers, booleans, or null.`);
    }
  }
  return parsed;
}

class FilterError extends Error {
  constructor(input, message) {
    super(message);
    this.input = input;
  }
}

function buildRequest() {
  const filters = {};
  const scalarFilters = [
    ["bucket", byId("filter-bucket")],
    ["key_prefix", byId("filter-prefix")],
    ["tier", byId("filter-tier")],
    ["mime", byId("filter-mime")],
    ["content_sha256", byId("filter-sha")],
  ];
  scalarFilters.forEach(([name, input]) => {
    const value = input.value.trim();
    if (value) {
      filters[name] = value;
    }
  });

  const size = byId("filter-size").value.trim();
  if (size) {
    filters.size = Number(size);
  }
  const objectMetadata = parseMetadata(byId("filter-object-metadata"), "Object metadata");
  const documentMetadata = parseMetadata(
    byId("filter-document-metadata"),
    "Document metadata",
  );
  if (Object.keys(objectMetadata).length) {
    filters.object_metadata = objectMetadata;
  }
  if (Object.keys(documentMetadata).length) {
    filters.document_metadata = documentMetadata;
  }

  const visibleLimit = Number(byId("result-limit").value);
  const ask = state.view === "ask";
  const retrievalMode = ask
    ? HYBRID_MODE
    : state.searchMode === "keyword"
      ? "metadata+keyword"
      : "metadata+vector";
  const requestLimit = ask ? visibleLimit : Math.min(100, Math.max(visibleLimit * 5, 50));

  return {
    payload: {
      text: ui.query.value.trim(),
      filters,
      limit: requestLimit,
      candidate_limit: Math.min(1000, Math.max(100, requestLimit * 5)),
      passages_per_result: Number(byId("passage-limit").value),
      synthesize: ask,
      exact_vector: !ask && state.searchMode === "vector" && ui.exactVector.checked,
      retrieval_mode: retrievalMode,
    },
    visibleLimit,
  };
}

async function submitQuery(event) {
  event.preventDefault();
  ui.query.value = ui.query.value.trim();
  if (!ui.form.reportValidity()) {
    return;
  }

  let request;
  try {
    request = buildRequest();
  } catch (error) {
    if (error instanceof FilterError) {
      error.input.setCustomValidity(error.message);
      byId("filter-drawer").open = true;
      error.input.reportValidity();
      error.input.addEventListener("input", () => error.input.setCustomValidity(""), { once: true });
      showError(error.message);
      return;
    }
    throw error;
  }

  if (state.controller) {
    state.controller.abort();
  }
  const controller = new AbortController();
  state.controller = controller;
  showLoading();

  try {
    const response = await fetch(API_PATH, {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(request.payload),
      signal: controller.signal,
    });
    const body = await readJson(response);
    if (!response.ok) {
      const apiMessage = body?.error?.message || `The service returned HTTP ${response.status}.`;
      const requestId = body?.request_id || response.headers.get("X-Request-ID");
      throw new APIError(apiMessage, requestId);
    }
    renderResponse(body, request.visibleLimit);
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    if (error instanceof APIError) {
      showError(error.message, error.requestId);
    } else {
      showError("CogniStore could not be reached. Check the API service and try again.");
    }
  } finally {
    if (state.controller === controller) {
      state.controller = null;
      setSubmitting(false);
    }
  }
}

class APIError extends Error {
  constructor(message, requestId) {
    super(message);
    this.requestId = requestId;
  }
}

async function readJson(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    return null;
  }
  try {
    return await response.json();
  } catch (_error) {
    return null;
  }
}

function clearOutput() {
  ui.status.replaceChildren();
  ui.results.replaceChildren();
  ui.notices.replaceChildren();
  ui.answer.replaceChildren();
  ui.summary.hidden = true;
  ui.summary.replaceChildren();
}

function showIdle() {
  clearOutput();
  ui.status.className = "query-status idle-state";
  ui.status.hidden = false;
  ui.status.removeAttribute("aria-busy");
  addText(ui.status, "div", "status-glyph", state.view === "ask" ? "✦" : "⌕").ariaHidden = "true";
  addText(ui.status, "h3", "", state.view === "ask" ? "Ask with evidence" : "Ready when you are");
  addText(
    ui.status,
    "p",
    "",
    state.view === "ask"
      ? "Answers include ranked source objects and passage citations you can inspect."
      : "Run a search to see ranked objects, passage-level evidence, and stable citation IDs.",
  );
  setSubmitting(false);
}

function showLoading() {
  clearOutput();
  setSubmitting(true);
  ui.status.className = "query-status loading-state";
  ui.status.hidden = false;
  const pulse = createElement("div", "loading-pulse");
  pulse.setAttribute("aria-hidden", "true");
  pulse.append(createElement("span"), createElement("span"), createElement("span"));
  ui.status.append(pulse);
  addText(
    ui.status,
    "h3",
    "",
    state.view === "ask" ? "Gathering grounded context…" : `Running ${state.searchMode} search…`,
  );
  addText(ui.status, "p", "", "Checking current catalog state and ranking matching passages.");
  ui.status.setAttribute("aria-busy", "true");
}

function showError(message, requestId) {
  clearOutput();
  ui.status.className = "query-status error-state";
  ui.status.hidden = false;
  ui.status.removeAttribute("aria-busy");
  addText(ui.status, "div", "status-glyph", "!").ariaHidden = "true";
  addText(ui.status, "h3", "", "The query could not be completed");
  addText(ui.status, "p", "", message);
  if (requestId) {
    addText(ui.status, "code", "request-id", `Request ${requestId}`);
  }
  setSubmitting(false);
}

function setSubmitting(active) {
  ui.submit.disabled = active;
  ui.form.setAttribute("aria-busy", String(active));
}

function providerMap(response) {
  return new Map((response.providers || []).map((item) => [item.component, item]));
}

function selectResults(response, limit) {
  const sourceResults = Array.isArray(response.results) ? response.results.slice() : [];
  if (state.view === "ask") {
    return sourceResults.slice(0, limit).map((result) => ({ ...result, displayScore: result.score }));
  }
  const signal = state.searchMode;
  const selected = sourceResults
    .map((result) => {
      const component = (result.score_components || []).find((item) => item.signal === signal);
      return component ? { ...result, displayScore: component.raw_score } : null;
    })
    .filter(Boolean);
  selected.sort((left, right) => {
    const scoreOrder = right.displayScore - left.displayScore;
    if (scoreOrder) {
      return scoreOrder;
    }
    const leftKey = `${left.citation.bucket}/${left.citation.key}`;
    const rightKey = `${right.citation.bucket}/${right.citation.key}`;
    return leftKey.localeCompare(rightKey);
  });
  return selected.slice(0, limit);
}

function renderResponse(response, visibleLimit) {
  clearOutput();
  ui.status.hidden = true;
  ui.status.removeAttribute("aria-busy");
  renderProviderNotices(response);
  if (state.view === "ask") {
    renderAnswer(response);
  }
  const results = selectResults(response, visibleLimit);
  if (!results.length) {
    ui.status.className = "query-status empty-state";
    ui.status.hidden = false;
    addText(ui.status, "div", "status-glyph", "○").ariaHidden = "true";
    addText(ui.status, "h3", "", "No matching content");
    addText(
      ui.status,
      "p",
      "",
      "Try broader wording, remove a filter, or check the provider notice above.",
    );
  } else {
    results.forEach((result, index) => ui.results.append(renderResult(result, index + 1)));
  }
  renderSummary(response, results.length);
}

function renderSummary(response, count) {
  ui.summary.hidden = false;
  const countText = `${count} ${count === 1 ? "result" : "results"}`;
  addText(ui.summary, "strong", "", countText);
  addText(ui.summary, "span", "", response.mode || "unknown mode");
}

function notice(kind, title, message) {
  const box = createElement("div", `notice notice-${kind}`);
  addText(box, "strong", "", title);
  addText(box, "p", "", message);
  return box;
}

function renderProviderNotices(response) {
  const providers = providerMap(response);
  const expected = state.view === "ask" ? ["keyword", "vector"] : [state.searchMode];
  expected.forEach((component) => {
    const provider = providers.get(component);
    if (!provider || !["missing", "unavailable"].includes(provider.state)) {
      return;
    }
    const label = component === "vector" ? "Vector" : "Keyword";
    const reason = provider.state === "missing" ? "is not configured" : "is temporarily unavailable";
    ui.notices.append(
      notice(
        "warning",
        `${label} provider degraded`,
        `${label} retrieval ${reason}. Results use the providers that remain available.`,
      ),
    );
  });

  if (state.view === "ask") {
    const generation = response.generation_status;
    if (["provider_missing", "provider_unavailable"].includes(generation)) {
      const reason = generation === "provider_missing" ? "is not configured" : "is unavailable";
      ui.notices.append(
        notice(
          "warning",
          "Answer generation degraded",
          `The answer provider ${reason}. Ranked, cited retrieval results are still available below.`,
        ),
      );
    }
  }
}

function renderAnswer(response) {
  if (!response.answer) {
    return;
  }
  const card = createElement("article", "answer-card");
  const heading = createElement("div", "answer-heading");
  addText(heading, "span", "answer-spark", "✦").ariaHidden = "true";
  addText(heading, "h3", "", "Grounded answer");
  card.append(heading);
  addText(card, "p", "answer-text", response.answer.text);
  const citations = createElement("div", "answer-citations");
  addText(citations, "span", "", "Cites");
  (response.answer.citations || []).forEach((citation) => {
    addText(citations, "code", "citation-chip", citation);
  });
  card.append(citations);
  ui.answer.append(card);
}

function renderResult(result, rank) {
  const item = createElement("li", "result-card");
  const citation = result.citation || {};
  const header = createElement("div", "result-card-header");
  addText(header, "span", "result-rank", String(rank));
  const identity = createElement("div", "result-identity");
  const title = citation.document_metadata?.title || fileName(citation.key) || "Untitled object";
  addText(identity, "h3", "", title);
  addText(identity, "p", "object-key", `${citation.bucket || "?"}/${citation.key || "?"}`);
  header.append(identity);

  const score = createElement("div", "result-score");
  addText(score, "small", "", state.view === "ask" ? "Fused score" : `${capitalize(state.searchMode)} score`);
  addText(score, "strong", "", formatScore(result.displayScore));
  header.append(score);
  item.append(header);

  const facts = createElement("div", "object-facts");
  facts.append(fact("Tier", citation.tier), fact("Type", citation.mime || "Unknown"), fact("Size", formatBytes(citation.size)));
  item.append(facts);

  const components = createElement("div", "score-components");
  (result.score_components || []).forEach((component) => {
    const chip = createElement("span", `signal-chip signal-${component.signal}`);
    addText(chip, "strong", "", component.signal);
    addText(chip, "span", "", `rank ${component.rank} · raw ${formatScore(component.raw_score)}`);
    components.append(chip);
  });
  item.append(components);

  const passages = (result.passages || []).filter(
    (passage) => state.view === "ask" || passage.citation?.source === state.searchMode,
  );
  if (passages.length) {
    const evidence = createElement("section", "evidence-list");
    addText(evidence, "h4", "", "Passage evidence");
    passages.forEach((passage) => evidence.append(renderPassage(passage)));
    item.append(evidence);
  }

  const footer = createElement("div", "result-actions");
  const open = createElement("a", "open-object", "Download cited object");
  open.href = objectUrl(citation);
  open.download = fileName(citation.key) || "cognistore-object";
  open.append(createElement("span", "", "↗"));
  footer.append(open, renderObjectDetails(citation));
  item.append(footer);
  const explain = createElement("button", "decision-link", "Explain placement");
  explain.type = "button";
  explain.addEventListener("click", () => inspectObjectDecisions(citation));
  item.append(explain);
  return item;
}

function renderPassage(passage) {
  const block = createElement("article", "passage");
  const top = createElement("div", "passage-topline");
  addText(top, "span", `source-badge source-${passage.citation.source}`, passage.citation.source);
  addText(
    top,
    "span",
    "passage-rank",
    `provider rank ${passage.match.rank} · score ${formatScore(passage.match.raw_score)}`,
  );
  block.append(top);
  addText(block, "blockquote", "", passage.text || "No extracted passage text.");

  const details = createElement("details", "citation-details");
  addText(details, "summary", "", "Inspect passage citation");
  const grid = createElement("dl", "citation-grid");
  grid.append(
    detail("Citation ID", passage.citation.citation_id),
    detail("Passage", String(passage.citation.passage_index)),
    detail("Offsets", `${passage.citation.start_codepoint}–${passage.citation.end_codepoint}`),
    detail("Text SHA-256", passage.citation.text_sha256),
    detail("Source SHA-256", passage.citation.source_sha256),
  );
  if (passage.citation.space_id) {
    grid.append(detail("Embedding space", passage.citation.space_id));
  }
  details.append(grid);
  block.append(details);
  return block;
}

function renderObjectDetails(citation) {
  const details = createElement("details", "object-details");
  addText(details, "summary", "", "Object metadata");
  const grid = createElement("dl", "citation-grid");
  grid.append(
    detail("Object citation", citation.citation_id),
    detail("Object ID", citation.object_id),
    detail("Content SHA-256", citation.content_sha256 || "Unavailable"),
  );
  details.append(grid);
  details.append(metadataBlock("Object metadata", citation.object_metadata, citation.object_metadata_truncated));
  details.append(metadataBlock("Document metadata", citation.document_metadata, citation.document_metadata_truncated));
  return details;
}

function metadataBlock(label, metadata, truncated) {
  const section = createElement("section", "metadata-block");
  const heading = createElement("h5", "", label);
  if (truncated) {
    addText(heading, "span", "truncated-badge", "truncated");
  }
  section.append(heading);
  addText(section, "pre", "", JSON.stringify(metadata || {}, null, 2));
  return section;
}

function fact(label, value) {
  const node = createElement("span", "object-fact");
  addText(node, "small", "", label);
  addText(node, "strong", "", value || "Unknown");
  return node;
}

function detail(label, value) {
  const wrapper = createElement("div");
  addText(wrapper, "dt", "", label);
  addText(wrapper, "dd", "", value || "Unavailable");
  return wrapper;
}

function formatScore(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "—";
  }
  return number.toFixed(number >= 10 ? 2 : 4).replace(/0+$/, "").replace(/\.$/, "");
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "Unknown";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = bytes / 1024;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[index]}`;
}

function fileName(key) {
  if (typeof key !== "string") {
    return "";
  }
  return key.split("/").pop();
}

function capitalize(value) {
  return value ? `${value[0].toUpperCase()}${value.slice(1)}` : "";
}

function encodePathSegment(value) {
  if (value === ".") {
    return "%2E";
  }
  if (value === "..") {
    return "%2E%2E";
  }
  return encodeURIComponent(String(value)).replace(/[!'()*]/g, (character) =>
    `%${character.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

function objectUrl(citation) {
  const tier = encodePathSegment(citation.tier || "");
  const bucket = encodePathSegment(citation.bucket || "");
  const key = String(citation.key || "").split("/").map(encodePathSegment).join("/");
  return `/v1/objects/${tier}/${bucket}/${key}`;
}

async function checkHealth() {
  try {
    const response = await fetch("/healthz", { headers: { "Accept": "application/json" } });
    if (!response.ok) {
      throw new Error("unhealthy");
    }
    ui.serviceState.classList.add("is-online");
    ui.serviceState.classList.remove("is-offline");
    ui.serviceState.lastElementChild.textContent = "API online";
  } catch (_error) {
    ui.serviceState.classList.add("is-offline");
    ui.serviceState.classList.remove("is-online");
    ui.serviceState.lastElementChild.textContent = "API unavailable";
  }
}

document.addEventListener("DOMContentLoaded", initialize);
