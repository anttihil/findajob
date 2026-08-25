import { useEffect } from "preact/hooks";
import { checkSyncStatus, syncBusy, syncStatus, triggerSync } from "../state/sync";

export function SyncStatusWidget() {
  useEffect(() => {
    checkSyncStatus();
  }, []);

  const stats = syncStatus.value?.last_run_stats;
  const showStats = !syncBusy.value && stats && stats.total_fetched > 0;

  return (
    <div class="sync-status-widget">
      <div class="sync-widget-header">SYS-ENGINE</div>
      <div class="sync-indicator">
        <span class="status-label">STATUS:</span>
        <span class={`status-badge-retro ${syncBusy.value ? "is-busy" : "is-online"}`}>
          {syncBusy.value ? "BUSY" : "ONLINE"}
        </span>
      </div>
      {showStats && stats && (
        <div class="sync-stats-info">
          SCANNED: <strong>{stats.total_fetched.toLocaleString()}</strong>
          <br />
          EVAL: <strong>{stats.total_evaluated.toLocaleString()}</strong>
          <br />
          SAVED: <strong>{stats.total_new}</strong>
        </div>
      )}
      <button class="quick-sync-btn" disabled={syncBusy.value} onClick={triggerSync}>
        <span>SYNC NOW ▶</span>
      </button>
    </div>
  );
}

