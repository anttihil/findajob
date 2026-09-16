import { syncBusy, triggerSync } from "../../state/sync";

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
