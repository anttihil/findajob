import { useEffect } from "preact/hooks";
import { getJSON, reportError } from "../../api/client";
import type {
  PipelineScoreStatus,
  PipelineScrapeStatus,
  PipelineStatusResponse,
} from "../../api/types";
import { ago } from "../../lib/format";
import { livePipelineStatus } from "../../state/liveEvents";

function since(timestamp: string | null | undefined): string | null {
  if (!timestamp) return null;
  const elapsed = Date.now() - new Date(timestamp).getTime();
  return Number.isFinite(elapsed) ? ago(Math.max(0, elapsed) / 3_600_000) : null;
}

function ScrapeStage({ scrape }: { scrape: PipelineScrapeStatus }) {
  const running = scrape.in_progress;
  const [done, planned] = running
    ? [scrape.cells_done ?? 0, scrape.cells_planned ?? 0]
    : scrape.previous_run?.cells ?? [0, 0];
  const percent = planned ? Math.round((done / planned) * 100) : null;
  const elapsed = since(scrape.started_at);
  let detail: string;
  if (running && scrape.cells_planned) {
    detail = `${percent}% · ${done}/${planned} cells${elapsed ? ` · started ${elapsed}` : ""}`;
  } else if (running) {
    detail = "Starting…";
  } else if (!scrape.previous_run?.last_run) {
    detail = "Never run";
  } else {
    const prev = scrape.previous_run;
    detail = `${ago(prev.hours_since)} · ${percent ?? 0}% · ${done}/${planned} cells`;
  }

  return (
    <div class="pipeline-stage">
      <div class="pipeline-stage-header">
        <span class={`status-dot ${running ? "orange" : "green"}`}></span>
        <span class="pipeline-stage-title">Scraping</span>
        <span class="pipeline-stage-status">
          {running ? "running" : "idle"}
        </span>
      </div>
      <p class="card-note">{detail}</p>
    </div>
  );
}

function ScoreStage({ score }: { score: PipelineScoreStatus }) {
  const active = score.recent_verdicts_5min > 0;
  const scoredFraction = 1 - (score.backlog_share || 0);
  const detail = [
    `${(scoredFraction * 100).toFixed(0)}% scored`,
    `${score.backlog.toLocaleString()} unscored`,
    score.last_verdict ? ago(score.hours_since) : "Never",
  ];

  return (
    <div class="pipeline-stage mt-4">
      <div class="pipeline-stage-header">
        <span class={`status-dot ${active ? "orange" : "green"}`}></span>
        <span class="pipeline-stage-title">Scoring</span>
        <span class="pipeline-stage-status">{active ? "scoring" : "idle"}</span>
      </div>
      <p class="card-note">{detail.join(" · ")}</p>
    </div>
  );
}

export function PipelineStatus({ compact = false }: { compact?: boolean }) {
  useEffect(() => {
    if (!livePipelineStatus.value) {
      getJSON<PipelineStatusResponse>("/api/pipeline/status")
        .then((data) => {
          if (data) livePipelineStatus.value = data;
        })
        .catch((err) => {
          reportError("Loading pipeline status", err);
        });
    }
  }, []);

  return (
    <div class={compact ? "pipeline-status-compact" : "glass-card mt-6"}>
      {!compact && (
        <h3>
          <i class="fa-solid fa-gauge-high"></i> Pipeline
        </h3>
      )}
      {livePipelineStatus.value && (
        <>
          <ScrapeStage scrape={livePipelineStatus.value.scrape} />
          <ScoreStage score={livePipelineStatus.value.score} />
        </>
      )}
    </div>
  );
}
