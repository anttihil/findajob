// Server-Sent Events (SSE) live connection and reactive signal manager.
//
// ARCHITECTURE HINTS & EXPLANATIONS:
// ----------------------------------
// 1. Single EventSource Stream:
//    Instead of multiple components running independent `setInterval` loops (e.g. SyncWidget
//    polling /api/sync/status every 2s, PipelineStatus polling /api/pipeline/status every 5s),
//    a single persistent EventSource connection to `/api/live/events` delivers all updates.
//
// 2. Preact Signal Integration:
//    When an SSE message arrives, we directly update global Preact signals (`stats`,
//    `livePipelineStatus`, `syncBusy`, `newJobsPendingCount`). Any component rendering
//    those signals will re-render automatically without prop drilling or custom event buses.
//
// 3. Reconnection & Resilience:
//    The browser's native `EventSource` automatically attempts reconnection if the network
//    drops. On reconnect, `onopen` fires again, letting us reset state or trigger fresh fetches.

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

/**
 * Initialize the global SSE stream. Safe to call multiple times (idempotent).
 *
 * IMPLEMENTATION GUIDE:
 * 1. Check if `eventSource` is already initialized; if so, return.
 * 2. Create `new EventSource("/api/live/events")`.
 * 3. Handle standard events:
 *    - `eventSource.onopen`: set `liveConnected.value = true`.
 *    - `eventSource.onerror`: set `liveConnected.value = false`.
 * 4. Add custom event listeners via `eventSource.addEventListener(type, handler)`:
 *    - `"stats_update"`: Parse JSON payload as `Stats` -> update `stats.value`.
 *    - `"pipeline_progress"`: Parse JSON as `PipelineStatusResponse` -> update `livePipelineStatus.value` and `syncBusy.value`.
 *    - `"jobs_changed"`: Increment `newJobsPendingCount.value += 1`.
 *    - `"ping"`: Keep-alive heartbeat (no-op or reset watchdog timer).
 */
export function initLiveEvents(): void {
  // TODO: Implement EventSource initialization and event listener dispatching
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
  // TODO: Close EventSource connection and reset liveConnected.value
  eventSource?.close();
  eventSource = null;
  liveConnected.value = false;
}
