import { syncBusy, triggerSync } from "../../state/sync";

// Ported from the sync box in `templates/tabs/settings.html`. The sidebar's own sync widget
// (`components/SyncStatusWidget.tsx`) shares the same `state/sync.ts` signals, so the two
// can never disagree about whether a scrape is running.
export function SyncTrigger() {
  return (
    <div class="glass-card sync-box">
      <h3>
        <i class="fa-solid fa-rotate-right"></i> Database Sync Engine
      </h3>
      <p>Run a scrape pass now. Postings land unscored; the scoring stage picks them up on its own timer.</p>

      <div class="sync-actions">
        <button class="primary-btn-lg" disabled={syncBusy.value} onClick={triggerSync}>
          <i class="fa-solid fa-satellite-dish"></i> Run Scrape Now
        </button>
        {syncBusy.value && (
          <div class="sync-progress-indicator">
            <i class="fa-solid fa-circle-notch fa-spin"></i>
            <span>Scrape in progress. Fetching and normalizing postings...</span>
          </div>
        )}
      </div>
    </div>
  );
}
