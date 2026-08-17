import { useEffect, useState } from "preact/hooks";
import { getJSON, guard, reportError, ApiError } from "../../api/client";
import type { Config, Meta, SearchLinksResponse } from "../../api/types";

// Ported from `templates/tabs/search_links.html` + `frontend/js/features/link-generator.js`.
export function SearchLinksPage() {
  const [countries, setCountries] = useState<Meta["countries"]>([]);
  const [queries, setQueries] = useState<string[]>([]);
  const [country, setCountry] = useState("");
  const [query, setQuery] = useState("");
  const [generating, setGenerating] = useState(false);
  const [links, setLinks] = useState<SearchLinksResponse | null>(null);

  useEffect(() => {
    (async () => {
      const meta = await guard("Loading countries", () => getJSON<Meta>("/api/meta"));
      if (meta) {
        setCountries(meta.countries);
        setCountry(meta.countries[0]?.[0] ?? "");
      }
      const config = await guard("Loading search queries", () => getJSON<Config>("/api/config"));
      const searchQueries = (config?.search_queries as string[] | undefined) ?? [];
      setQueries(searchQueries);
      setQuery(searchQueries[0] ?? "");
    })();
  }, []);

  async function generate() {
    if (!country || !query) return;
    setGenerating(true);
    try {
      const result = await getJSON<SearchLinksResponse>(
        `/api/search-links?country=${encodeURIComponent(country)}&query=${encodeURIComponent(query)}`
      );
      setLinks(result);
    } catch (err) {
      reportError(
        err instanceof ApiError && err.status === 404
          ? "No active profile — build one with `careerradar profile build`"
          : "Generating search links",
        err
      );
    } finally {
      setGenerating(false);
    }
  }

  const countryLabel = countries.find(([code]) => code === country)?.[1] ?? country;

  return (
    <section class="tab-pane active">
      <div class="glass-card instruction-box">
        <h2>
          <i class="fa-solid fa-link text-purple"></i> Direct Job Board Search Link Generator
        </h2>
        <p>
          Scraping job sites like Indeed and LinkedIn directly can hit CAPTCHAs. This module
          generates optimized boolean search strings from the active profile's strongest skills
          and lets you search directly on the boards in your browser with one click.
        </p>
      </div>

      <div class="link-generator-card">
        <div class="form-grid-2">
          <div class="form-group">
            <label for="link-country-select">Target Country</label>
            <select
              id="link-country-select"
              class="form-select"
              value={country}
              onChange={(e) => setCountry((e.target as HTMLSelectElement).value)}
            >
              {countries.map(([code, label]) => (
                <option key={code} value={code}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div class="form-group">
            <label for="link-query-select">Target Role Title</label>
            <select
              id="link-query-select"
              class="form-select"
              value={query}
              onChange={(e) => setQuery((e.target as HTMLSelectElement).value)}
            >
              {queries.map((q) => (
                <option key={q} value={q}>
                  {q}
                </option>
              ))}
            </select>
          </div>
        </div>

        <button class="primary-btn mt-4" disabled={generating} onClick={generate}>
          <i class={`fa-solid ${generating ? "fa-spinner fa-spin" : "fa-wand-magic-sparkles"}`}></i>{" "}
          {generating ? "Generating..." : "Generate Custom Search Links"}
        </button>

        {links && (
          <div class="generated-links-results">
            <div class="divider"></div>
            <h4 class="section-subtitle">
              Search Results for <span>{query}</span> in <span>{countryLabel}</span>
            </h4>

            <div class="search-query-display">
              <span>Boolean Search Phrase Used:</span>
              <code>{links.search_query_used}</code>
            </div>

            <div class="link-buttons-grid">
              <a href={links.linkedin} target="_blank" rel="noopener" class="board-link linkedin">
                <i class="fa-brands fa-linkedin"></i>
                <div class="link-label">
                  <strong>Search on LinkedIn</strong>
                  <span>Opens pre-filled search in a new tab</span>
                </div>
                <i class="fa-solid fa-up-right-from-square arrow-out"></i>
              </a>
              <a href={links.indeed} target="_blank" rel="noopener" class="board-link indeed">
                <i class="fa-solid fa-circle-info"></i>
                <div class="link-label">
                  <strong>Search on Indeed</strong>
                  <span>Opens pre-filled search in a new tab</span>
                </div>
                <i class="fa-solid fa-up-right-from-square arrow-out"></i>
              </a>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
