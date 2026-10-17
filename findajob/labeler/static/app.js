const state = {
  queues: [],
  queue: "initial",
  current: null,
  annotation: null,
  modelVerdict: null,
  saving: false,
  comparing: false,
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
  if (state.comparing) return;
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
