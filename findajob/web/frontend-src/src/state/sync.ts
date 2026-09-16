// Shared scrape state keeps the sidebar and Settings views synchronized.

import { signal } from "@preact/signals";
import { getJSON, reportError } from "../api/client";

export interface SyncErrorEntry {
  source: string;
  timestamp?: string;
  severity?: "error" | "warning" | "info";
  error: string;
}

export interface SyncStatus {
  sync_in_progress: boolean;
  last_run_stats?: {
    total_fetched: number;
    total_evaluated: number;
    total_new: number;
  };
  errors?: SyncErrorEntry[];
}

export const syncStatus = signal<SyncStatus | null>(null);
export const syncBusy = signal(false);

let pollHandle: ReturnType<typeof setInterval> | null = null;
// Notifies callers when a completed scrape may have made the current feed stale.
let onSyncFinished: (() => void) | null = null;

export function setSyncFinishedListener(fn: (() => void) | null): void {
  onSyncFinished = fn;
}

export async function checkSyncStatus(): Promise<void> {
  let data: SyncStatus;
  try {
    data = await getJSON<SyncStatus>("/api/sync/status");
  } catch (err) {
    reportError("Polling sync status", err);
    stopPolling();
    return;
  }

  syncStatus.value = data;

  if (data.sync_in_progress) {
    syncBusy.value = true;
    return;
  }

  const wasPolling = pollHandle !== null;
  stopPolling();
  syncBusy.value = false;

  if (wasPolling) onSyncFinished?.();
}

function startPolling(): void {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(checkSyncStatus, 2000);
}

function stopPolling(): void {
  if (pollHandle) {
    clearInterval(pollHandle);
    pollHandle = null;
  }
}

export async function triggerSync(): Promise<void> {
  syncBusy.value = true;
  try {
    const res = await fetch("/api/sync", { method: "POST" });
    if (!res.ok && res.status !== 409) {
      throw new Error(`sync returned ${res.status}`);
    }
    startPolling();
  } catch (err) {
    reportError("Triggering a scrape", err);
    syncBusy.value = false;
  }
}
