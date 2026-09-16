import { syncBusy, triggerSync } from "../../state/sync";

// Ported from the sync box in `templates/tabs/settings.html`. The sidebar's own sync widget
// (`components/SyncStatusWidget.tsx`) shares the same `state/sync.ts` signals, so the two
// can never disagree about whether a scrape is running.
export function SyncTrigger({ compact = false }: { compact?: boolean }) {
  return (
    <div class={compact ? "sync-box-compact" : "glass-card sync-box"}>
      {!compact && (
        <h3>
          <i class="fa-solid fa-rotate-right"></i> Run Scrape
        </h3>
      )}
      <div class="sync-actions">
        <button class={compact ? "btn btn-primary" : "primary-btn-lg"} disabled={syncBusy.value} onClick={triggerSync}>
          <i class="fa-solid fa-satellite-dish"></i> Run Scrape Now
        </button>
        {syncBusy.value && (
          <div class="sync-progress-indicator">
            <i class="fa-solid fa-circle-notch fa-spin"></i>
            <span>Scraping…</span>
          </div>
        )}
      </div>
    </div>
  );
}
