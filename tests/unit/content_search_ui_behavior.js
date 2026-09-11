"use strict";

// Run the shipped scripts with an executable DOM boundary and mocked HTTP.
// This checks rendered values, form requests and asynchronous view changes;
// no third-party JavaScript packages or browser download are required.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.className = "";
    this._text = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return [this._text, ...this.children.map((node) => node.textContent)].join(" ").trim(); }
  set innerHTML(_value) { throw new Error("Server values must never be parsed as HTML"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  async fire(name) {
    for (const handler of this.listeners[name] || []) {
      await handler({ preventDefault() {} });
    }
  }
  setCustomValidity(message) { this.validationMessage = message; }
  reportValidity() { return !this.validationMessage; }
}

const staticRoot = path.resolve(__dirname, "../../cognistore/ui/static");
const html = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");
const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element()]));
global.document = {
  getElementById(id) {
    assert.ok(elements.has(id), `UI references missing HTML element ${id}`);
    return elements.get(id);
  },
  createElement(tag) { return new Element(tag); },
  addEventListener() {},
};
for (const name of ["app.js", "decisions.js"]) {
  vm.runInThisContext(fs.readFileSync(path.join(staticRoot, name), "utf8"), { filename: name });
}

function descendants(node, tag) {
  return node.children.flatMap((child) => [
    ...(child.tagName === tag ? [child] : []), ...descendants(child, tag),
  ]);
}

const malicious = '<img src=x onerror="alert(1)">';
const fixture = {
  schema_version: 1,
  decision_id: "decision-1",
  bucket: "documents",
  key: malicious,
  evaluated_at: "2026-09-11T00:00:00Z",
  action: "move",
  disposition: "move",
  current: { tier: "hot" },
  proposed: { tier: "warm" },
  changed_fields: ["tier"],
  explanation: {
    state: "available",
    model_details: "unavailable",
    structured_reason: {
      code: "size_threshold",
      disposition: "move",
      policy: { name: "simple", version: "1", model: null },
      confidence: { value: null, source: "not_applicable" },
      decisive_signals: [
        { name: "size_bytes", value: 50, operator: ">", threshold: 0, rule_index: 0 },
        { name: "name_match", value: false, operator: "==", threshold: null },
      ],
      constraints: {
        allowed_destination_tiers: ["hot", "warm"],
        importance_level: "normal",
        residency_active: false,
        minimum_residency_seconds: 0,
        cooldown_active: false,
        cooldown_seconds: 0,
      },
    },
  },
  execution: { mode: "preview", state: "dry_run", job_id: null, job: null },
};

function response(body, status = 200) {
  return {
    ok: status < 400, status,
    headers: { get: (name) => name === "content-type" ? "application/json" : "request-test" },
    json: async () => body,
  };
}

async function main() {
  const preview = renderDecision(fixture);
  assert.match(preview.textContent, /Dry run · No action executed/);
  assert.doesNotMatch(preview.textContent, /Move completed/);
  assert.match(preview.textContent, /Size bytes: 50 > 0 · Rule 1/);
  assert.match(preview.textContent, /Name match: false/);
  assert.match(preview.textContent, /Residency active false/);
  assert.match(preview.textContent, /Model details unavailable/);
  assert.match(preview.textContent, /Policy reason Size threshold/);
  assert.ok(preview.textContent.includes(malicious));
  assert.equal(descendants(preview, "img").length, 0);
  assert.deepEqual(descendants(preview, "td").map((node) => node.textContent), ["hot", "warm"]);

  for (const [state, expected] of [
    ["planned", "Move planned · Not completed"],
    ["running", "Move running · Not completed"],
    ["retrying", "Move retrying · Not completed"],
    ["completed", "Move completed"],
    ["failed", "Move failed · Proposed placement is not confirmation"],
    ["unavailable", "Execution outcome unavailable"],
  ]) {
    const card = renderDecision({
      ...fixture,
      execution: {
        mode: "persisted", state, job_id: "job-1",
        job: { status: "failed", error_type: "DestinationUnavailable", retryable: false },
      },
    });
    assert.ok(card.textContent.includes(expected));
    assert.match(card.textContent, /Job outcome failed/);
    assert.match(card.textContent, /Job error DestinationUnavailable/);
    assert.match(card.textContent, /Job retryable false/);
    assert.doesNotMatch(card.textContent, /Dry run/);
  }

  const suppressed = renderDecision({
    ...fixture, disposition: "suppressed", proposed: { tier: "hot" }, changed_fields: [],
    explanation: {
      ...fixture.explanation,
      structured_reason: {
        ...fixture.explanation.structured_reason,
        code: "cooldown", disposition: "suppressed",
        constraints: { cooldown_active: true, candidate_destination_tier: "warm" },
      },
    },
    execution: { mode: "persisted", state: "not_requested" },
  });
  assert.match(suppressed.textContent, /No placement fields changed/);
  assert.match(suppressed.textContent, /Decision: Suppressed/);
  assert.match(suppressed.textContent, /Suppressed candidate tier warm/);
  assert.match(suppressed.textContent, /Cooldown active true/);
  assert.match(suppressed.textContent, /No move requested/);

  const legacy = renderDecision({
    ...fixture,
    explanation: { state: "legacy", structured_reason: null, model_details: "unavailable" },
  });
  assert.match(legacy.textContent, /legacy decision has no retained structured reasons/);
  assert.doesNotMatch(legacy.textContent, /Size threshold/);

  byId("decision-mode").value = "object";
  initializeDecisions();
  byId("decision-mode").value = "preview";
  await byId("decision-mode").fire("change");
  assert.equal(byId("decision-config").disabled, false);
  assert.equal(byId("decision-job").disabled, true);
  byId("decision-bucket").value = "docs & reports";
  byId("decision-key").value = "../folder/space ?#&.txt";
  byId("decision-config").value = '{"policy":"simple","threshold":0}';
  const requests = [];
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    return response(fixture);
  };
  await byId("decision-form").fire("submit");
  assert.equal(requests[0].url, "/v1/policies/preview");
  assert.equal(requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    bucket: "docs & reports", key: "../folder/space ?#&.txt", config: { policy: "simple", threshold: 0 },
  });
  assert.match(byId("decision-status").textContent, /No job created and no data moved/);
  assert.equal(byId("submit-decision").disabled, false);
  assert.equal(byId("decision-results").children.length, 1);

  byId("decision-config").value = "[]";
  await byId("decision-form").fire("submit");
  assert.equal(requests.length, 1);
  assert.match(byId("decision-config").validationMessage, /valid JSON object/);
  assert.equal(byId("decision-results").children.length, 0);

  byId("decision-mode").value = "object";
  await byId("decision-mode").fire("change");
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    return response({ items: [fixture], page: { next_cursor: requests.length === 2 ? "next/+=?" : null } });
  };
  await byId("decision-form").fire("submit");
  const query = new URL(requests[1].url, "http://localhost").searchParams;
  assert.equal(query.get("bucket"), "docs & reports");
  assert.equal(query.get("key"), "../folder/space ?#&.txt");
  assert.equal(byId("decision-more").hidden, false);
  await byId("decision-more").fire("click");
  assert.equal(new URL(requests[2].url, "http://localhost").searchParams.get("cursor"), "next/+=?");
  assert.equal(byId("decision-results").children.length, 2);
  assert.equal(byId("decision-more").hidden, true);

  byId("decision-mode").value = "job";
  await byId("decision-mode").fire("change");
  assert.equal(byId("decision-bucket").disabled, true);
  assert.equal(byId("decision-job").required, true);
  byId("decision-job").value = "job-50";
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    return response({ items: [], page: { next_cursor: null } });
  };
  await byId("decision-form").fire("submit");
  const jobQuery = new URL(requests[3].url, "http://localhost").searchParams;
  assert.equal(jobQuery.get("job_id"), "job-50");
  assert.equal(jobQuery.has("bucket"), false);
  assert.match(byId("decision-status").textContent, /No retained decisions found/);

  global.fetch = async () => response({ error: { message: "Policy unavailable" } }, 503);
  await byId("decision-form").fire("submit");
  assert.match(byId("decision-status").textContent, /Policy unavailable Request request-test/);
  assert.equal(byId("decision-form").attributes["aria-busy"], "false");

  let resolveOld;
  let oldSignal;
  global.fetch = (_url, options) => {
    oldSignal = options.signal;
    return new Promise((resolve) => { resolveOld = resolve; });
  };
  const oldRequest = byId("decision-form").fire("submit");
  byId("decision-mode").value = "preview";
  await byId("decision-mode").fire("change");
  assert.equal(oldSignal.aborted, true);
  resolveOld(response({ items: [fixture], page: { next_cursor: "stale" } }));
  await oldRequest;
  assert.equal(byId("decision-results").children.length, 0);
  assert.equal(byId("decision-more").hidden, true);
  assert.equal(byId("submit-decision").disabled, false);
  process.stdout.write("Placement UI behavior passed\n");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
