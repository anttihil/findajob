import { useEffect, useState } from "preact/hooks";
import { getJSON, putJSON, reportError } from "../../api/client";
import type { SchedulerPreferences } from "../../api/types";

function nextSearchRuns(preferences: SchedulerPreferences | null): string[] {
  return Object.entries(preferences?.status.next_runs ?? {})
    .filter(([key]) => key.startsWith("search:"))
    .flatMap(([key, value]) => {
      if (!value) return [];
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return [];
      const source = key.slice("search:".length);
      const name = source === "linkedin" ? "LinkedIn" : source === "indeed" ? "Indeed" : source;
      return [`${name}: ${date.toLocaleString()}`];
    });
}

export function SchedulerControl({ compact = false }: { compact?: boolean }) {
  const [preferences, setPreferences] = useState<SchedulerPreferences | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getJSON<SchedulerPreferences>("/api/scheduler/preferences")
      .then(setPreferences)
      .catch((err) => reportError("Loading automatic update settings", err));
  }, []);

  useEffect(() => {
    if (!preferences?.enabled) return;
    const timer = window.setInterval(() => {
      getJSON<SchedulerPreferences>("/api/scheduler/preferences")
        .then(setPreferences)
        .catch((err) => reportError("Refreshing automatic update settings", err));
    }, 15000);
    return () => window.clearInterval(timer);
  }, [preferences?.enabled]);

  async function toggle() {
    if (!preferences || saving) return;
    setSaving(true);
    try {
      setPreferences(
        await putJSON<SchedulerPreferences>("/api/scheduler/preferences", {
          enabled: !preferences.enabled,
        }),
      );
    } catch (err) {
      reportError("Updating automatic updates", err);
    } finally {
      setSaving(false);
    }
  }

  const enabled = preferences?.enabled ?? false;
  const next = nextSearchRuns(preferences);
  return (
    <div class={compact ? "scheduler-control-compact" : "glass-card scheduler-control"}>
      {!compact && (
        <h3>
          <i class="fa-solid fa-clock"></i> Automatic updates
        </h3>
      )}
      <div class="scheduler-control-row">
        <div>
          <strong>{enabled ? "Automatic updates are on" : "Keep searches up to date"}</strong>
          <p class="card-note">
            {enabled
              ? next.length
                ? `Next searches — ${next.join("; ")}`
                : "Preparing the next search…"
              : "Periodically scrapes enabled sources and scores new postings. This may use API credits."}
          </p>
        </div>
        <button class="btn btn-primary" disabled={!preferences || saving} onClick={toggle}>
          {saving ? "Saving…" : enabled ? "Turn off" : "Turn on"}
        </button>
      </div>
    </div>
  );
}
