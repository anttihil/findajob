const state = {
  queues: [],
  queue: "initial",
  current: null,
  annotation: null,
  modelVerdict: null,
  saving: false,
  comparing: false,
  evaluating: false,
  evaluationId: null,
};

const $ = (selector) => document.querySelector(selector);
const workspace = $("#workspace");
const emptyState = $("#empty-state");
const errorNode = $("#error");

async function request(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "Request failed");
  return body;
}

function queueSummary() {
  return state.queues.find((queue) => queue.queue_name === state.queue);
}

function renderProgress() {
  const queue = queueSummary();
  $("#progress-copy").textContent = queue
    ? `${queue.completed} of ${queue.total} reviewed`
    : "No examples";
}

async function loadQueues(preserveSelection = true) {
  const body = await request("/api/queues");
  state.queues = body.queues;
  const select = $("#queue-select");
  const previous = preserveSelection ? state.queue : "";
  select.replaceChildren();
  for (const queue of state.queues) {
    const option = document.createElement("option");
    option.value = queue.queue_name;
    option.textContent = `${queue.queue_name} · ${queue.pending} left`;
    select.append(option);
  }
  if (state.queues.some((queue) => queue.queue_name === previous)) state.queue = previous;
  else if (state.queues.length) {
    const nextQueue = state.queues.find((queue) => queue.pending > 0) || state.queues[0];
    state.queue = nextQueue.queue_name;
  }
  select.value = state.queue;
  renderProgress();
}

function metaPart(text, className = "") {
  const span = document.createElement("span");
  span.textContent = text;
  if (className) span.className = className;
  return span;
}

function renderExample() {
  const example = state.current;
  workspace.hidden = !example;
  emptyState.hidden = Boolean(example);
  if (!example) return;
  $("#review-position").textContent = `Blinded review · item ${example.position}`;
  $("#job-title").textContent = example.title;
  $("#split-select").value = example.split;
  const meta = $("#job-meta");
  meta.replaceChildren();
  if (example.company) meta.append(metaPart(example.company));
  if (example.location) meta.append(metaPart(example.location, "dot"));
  if (example.date_posted) meta.append(metaPart(example.date_posted, "dot"));
  if (example.url) {
    const link = document.createElement("a");
    link.href = example.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = "Original posting ↗";
    link.className = "dot";
    meta.append(link);
  }
  $("#job-description").textContent = example.description;
  $("#profile-text").textContent = example.profile_text || "No rendered profile text in snapshot.";
  $("#profile-text").className = "profile-text collapsed";
  $("#profile-toggle").textContent = "Expand";
  $("#reveal-card").hidden = true;
  $("#decision-card").hidden = false;
  errorNode.textContent = "";
  state.annotation = example.annotation;
  state.modelVerdict = example.model_verdict;
  if (state.annotation) renderReveal();
  window.scrollTo({ top: 0, behavior: "instant" });
}

async function loadNext() {
  leaveEvaluation();
  state.comparing = false;
  $("#comparison-view").hidden = true;
  state.current = null;
  state.annotation = null;
  state.modelVerdict = null;
  const body = await request(`/api/next?queue=${encodeURIComponent(state.queue)}`);
  state.current = body.example;
  renderExample();
  renderProgress();
}

function agreementLabel(value) {
  if (value === 1) return ["Agrees", "agree"];
  if (value === 0) return ["Disagrees", "disagree"];
  return ["Not comparable", "neutral"];
}

function comparisonText(tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
}

function outcomeLabel(outcome) {
  return { apply: "Apply", stretch: "Stretch", pass: "Pass", insufficient: "Insufficient information" }[outcome];
}

async function showComparisons() {
  leaveEvaluation();
  state.comparing = true;
  workspace.hidden = true;
  emptyState.hidden = true;
  $("#comparison-view").hidden = false;
  $("#comparison-error").textContent = "";
  $("#comparison-summary").textContent = "Loading comparisons…";
  const list = $("#comparison-list");
  list.replaceChildren();
  try {
    const report = await request("/api/consistency");
    const renderSummary = () => {
      $("#comparison-summary").textContent = report.total
        ? `${report.agreed} of ${report.total} labels agree (${Math.round(report.agreement_rate * 100)}%) · ${report.disagreed} disagreements · ${report.unresolved} unresolved`
        : "No completed review pairs yet. Complete a blinded relabel queue first.";
    };
    renderSummary();
    for (const item of report.comparisons) {
      const details = document.createElement("details");
      details.className = "card comparison-item";
      details.open = !item.agrees;
      const summary = comparisonText("summary", `${item.title} · ${item.company || "Unknown company"}`);
      summary.append(comparisonText("span",
        `${outcomeLabel(item.first_outcome)} → ${outcomeLabel(item.second_outcome)}`,
        `agreement ${item.agrees ? "agree" : "disagree"}`));
      details.append(summary);
      const rounds = document.createElement("div");
      rounds.className = "comparison-rounds";
      for (const [prefix, title] of [["first", "First round"], ["second", "Blinded second round"]]) {
        const column = document.createElement("section");
        column.append(comparisonText("h2", title));
        column.append(comparisonText("p", `Outcome: ${outcomeLabel(item[`${prefix}_outcome`])}`));
        column.append(comparisonText("p", `Reason: ${item[`${prefix}_reason`] || "Not specified"}`));
        column.append(comparisonText("p", `Confidence: ${item[`${prefix}_confidence`] ?? "Not specified"}`));
        column.append(comparisonText("p", `Notes: ${item[`${prefix}_notes`] || "None"}`));
        rounds.append(column);
      }
      details.append(rounds);
      if (!item.agrees) {
        const actions = document.createElement("section");
        actions.className = "resolution-actions";
        actions.append(comparisonText("h2", "Final dataset label"));
        const selectLabel = comparisonText("label", "Choose which review to use");
        const select = document.createElement("select");
        select.append(comparisonText("option", "Choose a label…"));
        select.children[0].value = "";
        for (const [round, prefix] of [[1, "first"], [2, "second"]]) {
          const option = comparisonText("option", `Use ${prefix} label: ${outcomeLabel(item[`${prefix}_outcome`])}`);
          option.value = String(round);
          select.append(option);
        }
        select.value = item.resolution ? String(item.resolution.selected_round) : "";
        selectLabel.append(select);
        actions.append(selectLabel);
        const notesLabel = comparisonText("label", "Resolution rationale (optional)");
        const notes = document.createElement("textarea");
        notes.rows = 3;
        notes.maxLength = 5000;
        notes.placeholder = "What criterion settles this case?";
        notes.value = item.resolution?.notes || "";
        notesLabel.append(notes);
        actions.append(notesLabel);
        const save = comparisonText("button", "Save final label", "next-button");
        save.type = "button";
        const status = comparisonText("p", "", "hint");
        status.setAttribute("role", "status");
        const renderStatus = () => {
          status.textContent = item.resolution
            ? `Saved: use ${item.resolution.selected_round === 1 ? "first" : "second"} label in exports.`
            : "Unresolved: exports currently use the first-round label.";
        };
        renderStatus();
        save.addEventListener("click", async () => {
          if (!select.value) {
            status.textContent = "Choose the first or second label before saving.";
            return;
          }
          save.disabled = true;
          try {
            const body = await request(`/api/examples/${item.id}/resolution`, {
              method: "PUT",
              body: JSON.stringify({ selected_round: Number(select.value), notes: notes.value }),
            });
            if (!item.resolution) report.unresolved -= 1;
            item.resolution = body.resolution;
            renderStatus();
            renderSummary();
          } catch (error) {
            status.textContent = `Could not save: ${error.message}`;
          } finally {
            save.disabled = false;
          }
        });
        actions.append(save, status);
        details.append(actions);
      }
      details.append(comparisonText("h2", "Posting evidence"));
      details.append(comparisonText("article", item.description, "description"));
      details.append(comparisonText("h2", "Candidate profile"));
      details.append(comparisonText("pre", item.profile_text, "profile-text"));
      list.append(details);
    }
  } catch (error) {
    $("#comparison-summary").textContent = "Could not load comparisons.";
    $("#comparison-error").textContent = error.message;
  }
}

function renderReveal() {
  const annotation = state.annotation;
  const verdict = state.modelVerdict;
  $("#decision-card").hidden = true;
  $("#reveal-card").hidden = false;
  $("#model-outcome").textContent = verdict?.outcome === "match" ? "Model: Match" : "Model: Reject";
  $("#model-reason-type").textContent = verdict?.reason_type || "No reason category";
  $("#model-reason").textContent = verdict?.reason_description || "No explanation was stored.";
  const [label, className] = agreementLabel(annotation.agreement);
  $("#agreement").textContent = label;
  $("#agreement").className = `agreement ${className}`;
  const canChooseReason = ["stretch", "pass"].includes(annotation.outcome);
  $("#reason-section").hidden = !canChooseReason;
  for (const button of document.querySelectorAll("[data-reason]")) {
    button.classList.toggle("selected", button.dataset.reason === annotation.decisive_reason);
  }
  for (const button of document.querySelectorAll("[data-confidence]")) {
    button.classList.toggle("selected", Number(button.dataset.confidence) === annotation.confidence);
  }
  $("#notes").value = annotation.notes || "";
  $("#save-state").textContent = "Label saved";
}

let evaluationTimer;
function leaveEvaluation() {
  state.evaluating = false;
  clearTimeout(evaluationTimer);
  $("#evaluation-view").hidden = true;
}

async function loadEvaluations() {
  const body = await request("/api/evaluations");
  const datasets = $("#evaluation-dataset");
  const selected = datasets.value;
  datasets.replaceChildren();
  for (const item of body.exports) {
    const option = comparisonText("option", `${item.version} · ${item.example_count} labels · ${JSON.stringify(item.manifest.split_counts)}`);
    option.value = item.version;
    datasets.append(option);
  }
  if (body.exports.some((item) => item.version === selected)) datasets.value = selected;
  if (!$("#candidate-rules").dataset.initialized) {
    $("#candidate-rules").value = body.default_candidate_rules;
    $("#evaluation-model").value = body.default_model;
    $("#evaluation-provider").value = body.default_provider;
    $("#candidate-rules").dataset.initialized = "true";
  }
  $("#evaluation-start").disabled = !body.exports.length;
  const list = $("#evaluation-runs");
  list.replaceChildren();
  for (const run of body.runs) {
    const button = comparisonText("button", `${run.dataset_version} · ${run.split} · ${run.model} · ${run.status} (${run.completed}/${run.total}) · ${run.created_at}`, "text-button");
    button.type = "button";
    button.addEventListener("click", () => openEvaluation(run.id));
    list.append(button);
  }
  if (!body.runs.length) list.append(comparisonText("p", "No evaluations yet.", "hint"));
}

async function showEvaluations() {
  state.evaluating = true;
  state.comparing = false;
  workspace.hidden = true;
  emptyState.hidden = true;
  $("#comparison-view").hidden = true;
  $("#evaluation-view").hidden = false;
  $("#evaluation-error").textContent = "";
  try {
    await loadEvaluations();
    if (state.evaluationId) await openEvaluation(state.evaluationId);
  } catch (error) {
    $("#evaluation-error").textContent = error.message;
  }
}

const percent = (value) => value == null ? "—" : `${(100 * value).toFixed(1)}%`;

function evaluationTable(headers, rows) {
  const table = document.createElement("table");
  table.className = "evaluation-table";
  const heading = document.createElement("tr");
  for (const header of headers) heading.append(comparisonText("th", header));
  table.append(heading);
  for (const cells of rows) {
    const row = document.createElement("tr");
    for (const cell of cells) row.append(comparisonText("td", String(cell)));
    table.append(row);
  }
  return table;
}

function renderEvaluation(run) {
  const result = $("#evaluation-result");
  result.replaceChildren();
  result.append(comparisonText("h2", `${run.dataset_version} · ${run.split} · ${run.status}`));
  result.append(comparisonText("p", `${run.completed}/${run.total} evaluations · ${run.provider}/${run.model} · ${run.created_at}`));
  if (run.error) result.append(comparisonText("p", run.error, "error"));
  result.append(comparisonText("p", run.metric_policy, "hint"));
  const variants = ["baseline", "candidate"];
  if (run.summary.baseline) {
    const fields = [
      ["Agreement", (m) => percent(m.agreement)],
      ["Weighted agreement", (m) => percent(m.weighted_agreement)],
      ["False-positive matches / predicted matches", (m) => `${m.false_positive_matches} · ${percent(m.false_positive_match_rate)}`],
      ["False negatives / human Apply or Stretch", (m) => `${m.false_negatives} · ${percent(m.false_negative_rate)}`],
      ["Scored / eligible", (m) => `${m.scored}/${m.eligible}`],
      ["Failed or pending", (m) => m.failed],
      ["Insufficient (excluded)", (m) => m.insufficient],
      ["Provider attempts", (m) => m.calls],
      ["Prompt / completion tokens (recorded)", (m) => `${m.tokens.prompt} / ${m.tokens.completion} (${m.usage_recorded} evaluations)`],
      ["Measured cost", (m) => m.cost_usd == null ? "Unavailable" : `$${m.cost_usd.toFixed(6)} (${m.cost_recorded} evaluations)`],
      ["Cost / evaluation (recorded)", (m) => m.cost_per_evaluation_usd == null ? "Unavailable" : `$${m.cost_per_evaluation_usd.toFixed(6)}`],
    ];
    result.append(evaluationTable(["Metric", "Baseline", "Candidate"], fields.map(([name, format]) =>
      [name, ...variants.map((v) => format(run.summary[v]))])));
    for (const variant of variants) {
      const metric = run.summary[variant];
      result.append(comparisonText("h3", `${variant} confusion matrix`));
      result.append(evaluationTable(["Human ↓ / predicted →", "Apply", "Stretch", "Pass"],
        ["apply", "stretch", "pass"].map((human) => [outcomeLabel(human),
          ...["apply", "stretch", "pass"].map((p) => metric.confusion_matrix[human][p])])));
    }
    const strata = document.createElement("details");
    strata.append(comparisonText("summary", "Metrics by split, original model reason, and human decisive reason"));
    for (const field of ["split", "model_reason_type", "human_reason"]) {
      strata.append(comparisonText("h3", field));
      strata.append(evaluationTable(["Group", "Labels", "Baseline agreement", "Candidate agreement", "Baseline FP / FN", "Candidate FP / FN"],
        Object.keys(run.summary.baseline.strata[field]).map((key) => {
          const a = run.summary.baseline.strata[field][key];
          const b = run.summary.candidate.strata[field][key];
          return [key, a.eligible, percent(a.agreement), percent(b.agreement),
            `${a.false_positive_matches} / ${a.false_negatives}`, `${b.false_positive_matches} / ${b.false_negatives}`];
        })));
    }
    result.append(strata);
  }
  result.append(comparisonText("h3", "Per-example predictions"));
  const examples = [...run.examples].sort((a, b) => {
    const disagrees = (r) => variants.some((v) => r[v] && r[v].outcome !== r.human_outcome);
    return Number(disagrees(b)) - Number(disagrees(a));
  });
  for (const row of examples) {
    const details = document.createElement("details");
    details.className = "card comparison-item";
    details.append(comparisonText("summary", `#${row.example_id} ${row.job.title} · Human: ${outcomeLabel(row.human_outcome)} · Baseline: ${outcomeLabel(row.baseline?.outcome) || "—"} · Candidate: ${outcomeLabel(row.candidate?.outcome) || "—"}`));
    details.append(comparisonText("p", `Split: ${row.split} · Human reason: ${row.human_reason} · ${row.human_notes || ""}`));
    for (const variant of variants) {
      details.append(comparisonText("h3", variant));
      details.append(comparisonText("p", row[variant]?.error || row[variant]?.verdict?.reason_description || "Not evaluated"));
      const prompt = document.createElement("details");
      prompt.append(comparisonText("summary", `Exact system prompt · ${row.prompt_hashes[variant]}`));
      prompt.append(comparisonText("pre", row.prompts[variant], "profile-text"));
      details.append(prompt);
    }
    const evidence = document.createElement("details");
    evidence.append(comparisonText("summary", "Exact rendered posting"));
    evidence.append(comparisonText("pre", row.rendered_posting, "profile-text"));
    details.append(evidence);
    result.append(details);
  }
}

async function openEvaluation(id) {
  clearTimeout(evaluationTimer);
  state.evaluationId = id;
  try {
    const run = await request(`/api/evaluations/${encodeURIComponent(id)}`);
    if (!state.evaluating || state.evaluationId !== id) return;
    renderEvaluation(run);
    if (["queued", "running"].includes(run.status)) {
      evaluationTimer = setTimeout(() => openEvaluation(id), 1500);
    } else await loadEvaluations();
  } catch (error) {
    $("#evaluation-error").textContent = error.message;
  }
}

$("#evaluate-button").addEventListener("click", showEvaluations);
$("#evaluation-back").addEventListener("click", () => {
  leaveEvaluation();
  renderExample();
});
$("#export-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#evaluation-error").textContent = "";
  try {
    const version = $("#export-version").value.trim();
    await request("/api/exports", { method: "POST", body: JSON.stringify({ version }) });
    await loadEvaluations();
    $("#evaluation-dataset").value = version;
    $("#export-version").value = "";
  } catch (error) { $("#evaluation-error").textContent = error.message; }
});
$("#evaluation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#evaluation-start").disabled = true;
  $("#evaluation-error").textContent = "";
  try {
    const body = await request("/api/evaluations", { method: "POST", body: JSON.stringify({
      version: $("#evaluation-dataset").value, split: $("#evaluation-split").value,
      model: $("#evaluation-model").value.trim(), candidate_rules: $("#candidate-rules").value,
      provider: $("#evaluation-provider").value.trim(),
      profile_boundary: $("#profile-boundary").value,
      fit_threshold: Number($("#evaluation-threshold").value),
    }) });
    await loadEvaluations();
    await openEvaluation(body.id);
  } catch (error) { $("#evaluation-error").textContent = error.message; }
  finally { $("#evaluation-start").disabled = false; }
});

async function label(outcome) {
  if (!state.current || state.saving) return;
  state.saving = true;
  errorNode.textContent = "";
  for (const button of document.querySelectorAll("[data-outcome]")) button.disabled = true;
  try {
    const body = await request(`/api/examples/${state.current.id}/label`, {
      method: "POST",
      body: JSON.stringify({ outcome, review_round: state.current.review_round }),
    });
    state.annotation = body.annotation;
    state.modelVerdict = body.model_verdict;
    renderReveal();
    await loadQueues();
  } catch (error) {
    errorNode.textContent = error.message;
  } finally {
    state.saving = false;
    for (const button of document.querySelectorAll("[data-outcome]")) button.disabled = false;
  }
}

let saveTimer;
async function saveDetails(patch = {}) {
  if (!state.annotation || state.saving) return;
  state.annotation = {
    ...state.annotation,
    decisive_reason: patch.decisive_reason ?? state.annotation.decisive_reason,
    confidence: patch.confidence ?? state.annotation.confidence,
    notes: patch.notes ?? $("#notes").value,
  };
  renderReveal();
  $("#save-state").textContent = "Saving…";
  const annotationId = state.annotation.id;
  const payload = {
    decisive_reason: state.annotation.decisive_reason,
    confidence: state.annotation.confidence,
    notes: state.annotation.notes,
  };
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const body = await request(`/api/annotations/${annotationId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      if (state.annotation?.id === annotationId) {
        state.annotation = body.annotation;
        $("#save-state").textContent = "Saved";
      }
    } catch (error) {
      if (state.annotation?.id === annotationId) $("#save-state").textContent = error.message;
    }
  }, 180);
}

document.addEventListener("click", (event) => {
  const outcomeButton = event.target.closest("[data-outcome]");
  if (outcomeButton) label(outcomeButton.dataset.outcome);
  const reasonButton = event.target.closest("[data-reason]");
  if (reasonButton) saveDetails({ decisive_reason: reasonButton.dataset.reason });
  const confidenceButton = event.target.closest("[data-confidence]");
  if (confidenceButton) saveDetails({ confidence: Number(confidenceButton.dataset.confidence) });
});

$("#notes").addEventListener("input", () => saveDetails({ notes: $("#notes").value }));
$("#next-button").addEventListener("click", loadNext);
$("#compare-button").addEventListener("click", showComparisons);
$("#back-button").addEventListener("click", () => {
  state.comparing = false;
  $("#comparison-view").hidden = true;
  renderExample();
});
$("#queue-select").addEventListener("change", async (event) => {
  state.queue = event.target.value;
  await loadNext();
});
$("#split-select").addEventListener("change", async (event) => {
  if (!state.current) return;
  try {
    await request(`/api/examples/${state.current.id}/split`, {
      method: "PUT",
      body: JSON.stringify({ split: event.target.value }),
    });
    state.current.split = event.target.value;
  } catch (error) {
    errorNode.textContent = error.message;
  }
});
$("#profile-toggle").addEventListener("click", () => {
  const profile = $("#profile-text");
  const expanded = profile.classList.toggle("expanded");
  profile.classList.toggle("collapsed", !expanded);
  $("#profile-toggle").textContent = expanded ? "Collapse" : "Expand";
});

document.addEventListener("keydown", (event) => {
  if (state.comparing || state.evaluating) return;
  const tag = event.target.tagName;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
  const key = event.key.toLowerCase();
  if (!state.annotation && { a: 1, s: 1, p: 1, i: 1 }[key]) {
    const outcomes = { a: "apply", s: "stretch", p: "pass", i: "insufficient" };
    event.preventDefault();
    label(outcomes[key]);
  } else if (state.annotation && key === "n") {
    event.preventDefault();
    loadNext();
  }
});

async function start() {
  try {
    await loadQueues(false);
    if (state.queues.length) await loadNext();
    else {
      workspace.hidden = true;
      emptyState.hidden = false;
      $("#progress-copy").textContent = "Import a queue first";
    }
  } catch (error) {
    workspace.hidden = true;
    emptyState.hidden = false;
    $("#empty-state h1").textContent = "Could not load labeler";
    $("#empty-state p").textContent = error.message;
  }
}

start();
