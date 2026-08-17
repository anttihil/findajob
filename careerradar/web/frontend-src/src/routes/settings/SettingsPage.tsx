import { useEffect, useState } from "preact/hooks";
import { getJSON, guard, postJSON, reportError } from "../../api/client";
import type { Config, Meta } from "../../api/types";
import { PipelineStatus } from "./PipelineStatus";
import { SyncTrigger } from "./SyncTrigger";

const SOURCE_LABELS: Record<string, string> = {
  indeed: "Indeed (volume source — full descriptions)",
  linkedin: "LinkedIn (budgeted supplement)",
};

// Ported from `templates/tabs/settings.html` + `frontend/js/features/settings.js`.
export function SettingsPage() {
  const [countries, setCountries] = useState<Meta["countries"]>([]);
  const [config, setConfig] = useState<Config | null>(null);
  const [checkedCountries, setCheckedCountries] = useState<Set<string>>(new Set());
  const [checkedSources, setCheckedSources] = useState<Record<string, boolean>>({});
  const [queriesText, setQueriesText] = useState("");
  const [maxTier, setMaxTier] = useState(10);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    guard("Loading countries", () => getJSON<Meta>("/api/meta")).then((meta) => {
      if (meta) setCountries(meta.countries);
    });
    guard("Loading configuration", () => getJSON<Config>("/api/config")).then((data) => {
      if (!data) return;
      setConfig(data);
      setCheckedCountries(new Set((data.countries as string[] | undefined) ?? []));
      const scraper = data.scraper as { sources?: Record<string, boolean> } | undefined;
      setCheckedSources(scraper?.sources ?? (data.sources as Record<string, boolean> | undefined) ?? {});
      setQueriesText(((data.search_queries as string[] | undefined) ?? []).join("\n"));
      const matching = data.matching as { max_tier?: number } | undefined;
      setMaxTier(matching?.max_tier ?? 10);
    });
  }, []);

  function toggleCountry(code: string) {
    setCheckedCountries((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  }

  async function save(e: Event) {
    e.preventDefault();
    setSaving(true);
    const queries = queriesText
      .split("\n")
      .map((q) => q.trim())
      .filter(Boolean);
    try {
      // /api/config deep-merges server-side, so omitting a key leaves it untouched --
      // posting a partial payload used to overwrite everything it did not mention.
      const result = await postJSON<{ success: boolean; config: Config }>("/api/config", {
        countries: [...checkedCountries],
        search_queries: queries,
        scraper: { sources: checkedSources },
        matching: { max_tier: maxTier },
      });
      setConfig(result.config);
      alert("Configuration saved successfully!");
    } catch (err) {
      reportError("Saving configuration", err);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section class="tab-pane active">
      <div class="settings-container">
        <SyncTrigger />
        <PipelineStatus />

        <div class="glass-card settings-card mt-6">
          <h3>
            <i class="fa-solid fa-gear"></i> Search Configuration
          </h3>

          <form onSubmit={save}>
            <div class="form-group mt-4">
              <label>Target Countries</label>
              <div class="checkbox-grid">
                {countries.map(([code, label]) => (
                  <label key={code} class="checkbox-label">
                    <input
                      type="checkbox"
                      checked={checkedCountries.has(code)}
                      onChange={() => toggleCountry(code)}
                    />{" "}
                    {label} ({code})
                  </label>
                ))}
              </div>
            </div>

            <div class="form-group mt-4">
              <label>Enabled Crawler Sources</label>
              <div class="checkbox-grid">
                {Object.entries(SOURCE_LABELS).map(([key, label]) => (
                  <label key={key} class="checkbox-label">
                    <input
                      type="checkbox"
                      checked={Boolean(checkedSources[key])}
                      onChange={(e) =>
                        setCheckedSources((prev) => ({
                          ...prev,
                          [key]: (e.target as HTMLInputElement).checked,
                        }))
                      }
                    />{" "}
                    {label}
                  </label>
                ))}
              </div>
            </div>

            <div class="form-group mt-4">
              <label for="settings-queries">Search Queries / Role Titles (One per line)</label>
              <textarea
                id="settings-queries"
                rows={5}
                class="form-textarea"
                value={queriesText}
                onInput={(e) => setQueriesText((e.target as HTMLTextAreaElement).value)}
              ></textarea>
            </div>

            <div class="form-group mt-4">
              <div class="slider-header">
                <label for="settings-score">Show postings down to tier</label>
                <span>{maxTier}</span>
              </div>
              <input
                type="range"
                id="settings-score"
                min="1"
                max="10"
                class="form-slider"
                value={maxTier}
                onInput={(e) => setMaxTier(Number((e.target as HTMLInputElement).value))}
              />
            </div>

            <button type="submit" class="save-btn mt-6" disabled={saving || !config}>
              <i class="fa-solid fa-floppy-disk"></i> Save Settings Configuration
            </button>
          </form>
        </div>
      </div>
    </section>
  );
}
