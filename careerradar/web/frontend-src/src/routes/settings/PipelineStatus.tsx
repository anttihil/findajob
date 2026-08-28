import { useEffect, useRef } from "preact/hooks";
import { getJSON, reportError } from "../../api/client";
import type {
  PipelineScoreStatus,
  PipelineScrapeStatus,
  PipelineStatusResponse,
} from "../../api/types";
import { renderMeters } from "../../charts/charts";
import { ago } from "../../lib/format";
import { livePipelineStatus } from "../../state/liveEvents";

function ScrapeStage({ scrape }: { scrape: PipelineScrapeStatus }) {
  const meterRef = useRef<HTMLDivElement>(null);
  const running = scrape.in_progress;

  useEffect(() => {
    if (!meterRef.current) return;
    if (running && scrape.cells_planned) {
      renderMeters(
        meterRef.current,
        [
          {
            label: "cells",
            value: scrape.cells_done ?? 0,
            display: `${scrape.cells_done ?? 0}/${scrape.cells_planned}`,
          },
        ],
        { max: scrape.cells_planned },
      );
    } else if (!running && scrape.previous_run?.cells) {
      const [succeeded, planned] = scrape.previous_run.cells;
      if (planned > 0) {
        renderMeters(
          meterRef.current,
          [
            {
              label: "cells",
              value: succeeded,
              display: `${succeeded}/${planned}`,
            },
          ],
          { max: planned },
        );
      } else {
        meterRef.current.innerHTML = "";
      }
    } else {
      meterRef.current.innerHTML = "";
    }
  }, [scrape, running]);

  let detail: string;
  if (running && scrape.cells_planned) {
    detail = `${scrape.cells_done} of ${scrape.cells_planned} planned cells scraped this run`;
  } else if (running) {
    detail = "starting up...";
  } else if (!scrape.previous_run?.last_run) {
    detail = "never run";
  } else {
    const prev = scrape.previous_run;
    const [succeeded, planned] = prev.cells || [0, 0];
    detail = `last run ${ago(prev.hours_since)} (${prev.status}): ${succeeded}/${planned} cells, ${prev.postings_new} new postings`;
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
      <div ref={meterRef}></div>
      <p class="card-note">{detail}</p>
    </div>
  );
}

function ScoreStage({ score }: { score: PipelineScoreStatus }) {
  const meterRef = useRef<HTMLDivElement>(null);
  const active = score.recent_verdicts_5min > 0;
  const scoredFraction = 1 - (score.backlog_share || 0);

  useEffect(() => {
    if (!meterRef.current) return;
    renderMeters(
      meterRef.current,
      [
        {
          label: "scored",
          value: scoredFraction,
          display: `${(scoredFraction * 100).toFixed(0)}%`,
        },
      ],
      { max: 1 },
    );
  }, [scoredFraction]);

  const detail = [`${score.backlog.toLocaleString()} unscored`];
  detail.push(
    score.last_verdict
      ? `last verdict ${ago(score.hours_since)}`
      : "no verdicts yet",
  );
  if (active)
    detail.push(`${score.recent_verdicts_5min} scored in the last 5 minutes`);

  return (
    <div class="pipeline-stage mt-4">
      <div class="pipeline-stage-header">
        <span class={`status-dot ${active ? "orange" : "green"}`}></span>
        <span class="pipeline-stage-title">Scoring</span>
        <span class="pipeline-stage-status">{active ? "scoring" : "idle"}</span>
      </div>
      <div ref={meterRef}></div>
      <p class="card-note">{detail.join(" · ")}</p>
    </div>
  );
}

export function PipelineStatus() {
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
    <div class="glass-card mt-6">
      <h3>
        <i class="fa-solid fa-gauge-high"></i> Pipeline Progress
      </h3>
      <p>Live status for background scraping and scoring pipelines.</p>
      {livePipelineStatus.value && (
        <>
          <ScrapeStage scrape={livePipelineStatus.value.scrape} />
          <ScoreStage score={livePipelineStatus.value.score} />
        </>
      )}
    </div>
  );
}
