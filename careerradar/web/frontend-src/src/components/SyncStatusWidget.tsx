import { useEffect } from "preact/hooks";
import { checkSyncStatus, syncBusy, syncStatus, triggerSync } from "../state/sync";

// Ported from the `.sync-status-widget` block in `partials/sidebar.html`.
export function SyncStatusWidget() {
  useEffect(() => {
    checkSyncStatus();
  }, []);

  const stats = syncStatus.value?.last_run_stats;
  const showStats = !syncBusy.value && stats && stats.total_fetched > 0;

  return (
    <div class="sync-status-widget">
      <div class="sync-indicator">
        <span class={`status-dot ${syncBusy.value ? "orange" : "green"}`}></span>
        <span class="status-text">{syncBusy.value ? "Syncing Database..." : "Database Idle"}</span>
      </div>
      {showStats && stats && (
        <div class="sync-stats-info">
          Last Sync Scanned: <strong>{stats.total_fetched.toLocaleString()}</strong> posts
          <br />
          Evaluated: <strong>{stats.total_evaluated.toLocaleString()}</strong> fits
          <br />
          Saved: <strong>{stats.total_new}</strong> new
        </div>
      )}
      <button class="quick-sync-btn" disabled={syncBusy.value} onClick={triggerSync}>
        <i class="fa-solid fa-rotate"></i> Sync Now
      </button>
    </div>
  );
}
