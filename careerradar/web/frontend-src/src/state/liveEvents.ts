import { signal } from "@preact/signals";
import type { PipelineStatusResponse, Stats } from "../api/types";

import { stats } from "./stats";

/** True when the EventSource connection to /api/live/events is open and healthy. */
export const liveConnected = signal<boolean>(false);

/** Real-time pipeline scrape/scoring progress pushed from backend SSE. */
export const livePipelineStatus = signal<PipelineStatusResponse | null>(null);

/** Number of newly scored/ingested jobs since the current feed was rendered. */
export const newJobsPendingCount = signal<number>(0);

let eventSource: EventSource | null = null;

/** Initialize the global SSE stream; safe to call repeatedly. */
export function initLiveEvents(): void {
  if (eventSource) return;

  eventSource = new EventSource("/api/live/events");

  eventSource.onopen = () => {
    liveConnected.value = true;
  };

  eventSource.onerror = () => {
    liveConnected.value = false;
  };

  eventSource.addEventListener("stats_update", (event) => {
    const data: Stats = JSON.parse(event.data);

    stats.value = data;
  });

  eventSource.addEventListener("pipeline_progress", (e) => {
    const data: PipelineStatusResponse = JSON.parse(e.data);

    livePipelineStatus.value = data;
  });

  eventSource.addEventListener("jobs_changed", () => {
    newJobsPendingCount.value++;
  });
}

/** Close the active SSE stream (e.g. during testing or hot reload). */
export function closeLiveEvents(): void {
  eventSource?.close();
  eventSource = null;
  liveConnected.value = false;
}
