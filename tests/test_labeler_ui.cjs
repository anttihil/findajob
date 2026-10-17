const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const source = readFileSync(
  join(__dirname, "../findajob/labeler/static/app.js"), "utf8",
);

async function openLabeler(queues, consistencyReport = {}) {
  const nodes = new Map();
  const requests = [];
  function makeNode() {
    return {
      children: [],
      listeners: {},
      replaceChildren() { this.children = []; },
      append(...children) { this.children.push(...children); },
      addEventListener(event, handler) { this.listeners[event] = handler; },
      setAttribute() {},
    };
  }
  const context = {
    document: {
      querySelector(selector) {
        if (!nodes.has(selector)) {
          nodes.set(selector, makeNode());
        }
        return nodes.get(selector);
      },
      createElement: makeNode,
      addEventListener() {},
    },
    async fetch(url, options) {
      requests.push(url);
      return {
        ok: true,
        async json() {
          if (url.endsWith("/resolution")) return { resolution: JSON.parse(options.body) };
          if (url === "/api/consistency") return consistencyReport;
          return url === "/api/queues" ? { queues } : { example: null };
        },
      };
    },
  };
  runInNewContext(source, context);
  await new Promise((resolve) => setImmediate(resolve));
  return { context, nodes, requests };
}

test("startup opens pending relabel reviews after the initial queue is complete", async () => {
  const { nodes, requests } = await openLabeler([
    { queue_name: "initial", pending: 0, completed: 75, total: 75 },
    { queue_name: "relabel-10pct-seed1", pending: 8, completed: 0, total: 8 },
  ]);
  assert.equal(nodes.get("#queue-select").value, "relabel-10pct-seed1");
  assert.equal(requests[1], "/api/next?queue=relabel-10pct-seed1");
  assert.equal(nodes.get("#progress-copy").textContent, "0 of 8 reviewed");
});

test("startup keeps the initial queue when it still has pending reviews", async () => {
  const { requests } = await openLabeler([
    { queue_name: "initial", pending: 1 },
    { queue_name: "consistency", pending: 8 },
  ]);
  assert.equal(requests[1], "/api/next?queue=initial");
});

test("progress refresh preserves the selected queue after its last review", async () => {
  const { context, nodes } = await openLabeler([
    { queue_name: "initial", pending: 0 },
    { queue_name: "consistency", pending: 8 },
  ]);
  await runInNewContext('state.queue = "initial"; loadQueues()', context);
  assert.equal(nodes.get("#queue-select").value, "initial");
});

test("startup handles completed queues and an empty database", async () => {
  const { requests } = await openLabeler([{ queue_name: "initial", pending: 0 }]);
  assert.equal(requests[1], "/api/next?queue=initial");
  const empty = await openLabeler([]);
  assert.deepEqual(empty.requests, ["/api/queues"]);
  assert.equal(empty.nodes.get("#progress-copy").textContent, "Import a queue first");
});

test("comparison view shows human labels and evidence, expanding disagreements", async () => {
  const { context, nodes } = await openLabeler([], {
    total: 2, agreed: 1, disagreed: 1, agreement_rate: 0.5, unresolved: 1,
    comparisons: [
      { title: "<Example>", company: "Robots", agrees: false,
        first_outcome: "stretch", second_outcome: "pass",
        first_notes: "Adjacent only", description: "Posting", profile_text: "Profile" },
      { title: "Platform", agrees: true, first_outcome: "apply", second_outcome: "apply" },
    ],
  });
  await runInNewContext("showComparisons()", context);
  assert.equal(nodes.get("#comparison-summary").textContent,
    "1 of 2 labels agree (50%) · 1 disagreements · 1 unresolved");
  assert.equal(nodes.get("#workspace").hidden, true);
  assert.equal(nodes.get("#comparison-view").hidden, false);
  const [disagreement, agreement] = nodes.get("#comparison-list").children;
  assert.equal(disagreement.open, true);
  assert.equal(agreement.open, false);
  assert.equal(disagreement.children[0].textContent, "<Example> · Robots");
  assert.equal(disagreement.children[0].children[0].textContent, "Stretch → Pass");
  assert.equal(disagreement.children[1].children[0].children[4].textContent, "Notes: Adjacent only");
  assert.equal(disagreement.children[4].textContent, "Posting");
  assert.equal(disagreement.children[6].textContent, "Profile");
});

test("saving a final label resolves a disagreement without changing agreement", async () => {
  const { context, nodes, requests } = await openLabeler([], {
    total: 1, agreed: 0, disagreed: 1, agreement_rate: 0, unresolved: 1,
    comparisons: [{ id: 30, title: "Role", agrees: false,
      first_outcome: "stretch", second_outcome: "pass" }],
  });
  await runInNewContext("showComparisons()", context);
  const actions = nodes.get("#comparison-list").children[0].children[2];
  const select = actions.children[1].children[0];
  const notes = actions.children[2].children[0];
  const save = actions.children[3];
  const status = actions.children[4];
  await save.listeners.click();
  assert.match(status.textContent, /Choose the first or second/);
  assert.equal(requests.includes("/api/examples/30/resolution"), false);
  select.value = "2";
  notes.value = "Core specialization gap.";
  await save.listeners.click();
  assert.match(status.textContent, /Saved: use second label/);
  assert.match(nodes.get("#comparison-summary").textContent, /0 of 1 labels agree.*0 unresolved/);
  select.value = "1";
  await save.listeners.click();
  assert.match(status.textContent, /Saved: use first label/);
  assert.match(nodes.get("#comparison-summary").textContent, /0 unresolved/);
  assert.equal(save.disabled, false);
});

test("comparison view explains when no review pairs are complete", async () => {
  const { context, nodes } = await openLabeler([], { total: 0, comparisons: [] });
  await runInNewContext("showComparisons()", context);
  assert.match(nodes.get("#comparison-summary").textContent, /No completed review pairs/);
  assert.equal(nodes.get("#comparison-list").children.length, 0);
});
