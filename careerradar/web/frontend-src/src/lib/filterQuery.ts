// The dashboard's filter state, and the only copy of it. Ported from
// `careerradar/web/rendering.py::FilterQuery`.
//
// Constructed fresh from the URL's search string on every render (via wouter's
// `useSearch()`), the same way the Python version was constructed fresh from the request's
// query string on every request -- there is no second copy of "the current filter" sitting
// in component state to drift from what the address bar says.

export type JobStatusFilter = "unread" | "saved" | "applied" | "rejected";
export type SortKey = "fit" | "fit_score" | "match_score" | "date_posted";

export interface FilterValues {
  status: JobStatusFilter;
  access: string;
  country: string;
  fit: boolean | null;
  reason_type: string;
  liveness: string;
  q: string;
  sort: SortKey;
  limit: number;
  offset: number;
}

export const FILTER_DEFAULTS: FilterValues = {
  status: "unread",
  access: "",
  country: "",
  fit: null,
  reason_type: "",
  liveness: "",
  q: "",
  sort: "fit",
  limit: 50,
  offset: 0,
};

type Overrides = Partial<Record<keyof FilterValues, string | number | boolean | null>>;

// Typed once as an index signature so default-comparisons below don't need a cast per call.
const DEFAULTS = FILTER_DEFAULTS as unknown as Record<string, unknown>;

function parseValues(search: string): FilterValues {
  const params = new URLSearchParams(search);
  const raw: Record<string, string | null> = {};
  for (const key of Object.keys(FILTER_DEFAULTS)) {
    raw[key] = params.has(key) ? params.get(key) : null;
  }
  return {
    status: (raw.status as JobStatusFilter) || FILTER_DEFAULTS.status,
    access: raw.access ?? FILTER_DEFAULTS.access,
    country: raw.country ?? FILTER_DEFAULTS.country,
    fit:
      raw.fit === "true" || raw.fit === "1"
        ? true
        : raw.fit === "false" || raw.fit === "0"
          ? false
          : FILTER_DEFAULTS.fit,
    reason_type: raw.reason_type ?? FILTER_DEFAULTS.reason_type,
    liveness: raw.liveness ?? FILTER_DEFAULTS.liveness,
    q: raw.q ?? FILTER_DEFAULTS.q,
    sort: (raw.sort as SortKey) || FILTER_DEFAULTS.sort,
    limit: raw.limit ? Number(raw.limit) : FILTER_DEFAULTS.limit,
    offset: raw.offset ? Number(raw.offset) : FILTER_DEFAULTS.offset,
  };
}

export class FilterQuery {
  readonly values: FilterValues;

  constructor(search: string) {
    this.values = parseValues(search);
  }

  get status() {
    return this.values.status;
  }
  get access() {
    return this.values.access;
  }
  get country() {
    return this.values.country;
  }
  get fit() {
    return this.values.fit;
  }
  get reason_type() {
    return this.values.reason_type;
  }
  get liveness() {
    return this.values.liveness;
  }
  get q() {
    return this.values.q;
  }
  get sort() {
    return this.values.sort;
  }
  get limit() {
    return this.values.limit;
  }
  get offset() {
    return this.values.offset;
  }

  private queryString(overrides: Overrides, extra: Record<string, string | number | boolean | null> = {}): string {
    const merged: Record<string, string | number | boolean | null> = { ...this.values, ...overrides, ...extra };
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(merged)) {
      const isDefault = key in DEFAULTS && value === DEFAULTS[key];
      if (value === null || value === "" || isDefault) continue;
      params.set(key, String(value));
    }
    return params.toString();
  }

  /** A link to the dashboard with some filters changed. Resets pagination and drops the
   * open drawer -- the posting being read may not survive the new filter. */
  url(overrides: Overrides = {}): string {
    const qs = this.queryString({ offset: 0, ...overrides });
    return qs ? `/?${qs}` : "/";
  }

  /** A link to another page of the *same* filter. */
  page(offset: number): string {
    const qs = this.queryString({ offset: Math.max(0, offset) });
    return qs ? `/?${qs}` : "/";
  }

  withJob(jobId: number): string {
    const qs = this.queryString({}, { job: jobId });
    return `/?${qs}`;
  }

  withoutJob(): string {
    const qs = this.queryString({}, { job: null });
    return qs ? `/?${qs}` : "/";
  }

  isActive(key: keyof FilterValues, value: unknown): boolean {
    return this.values[key] === value;
  }

  /** The filter as (key, value) pairs, for a form that changes one other field. Paging is
   * left out deliberately -- any re-submit of the filter is changing it. */
  hiddenFields(exclude: keyof FilterValues | ""): [string, string | number][] {
    const skip = new Set([exclude, "offset"]);
    return Object.entries(this.values).filter(([key, value]) => {
      if (skip.has(key as keyof FilterValues)) return false;
      const isDefault = value === DEFAULTS[key];
      return value !== null && value !== "" && !isDefault;
    }) as [string, string | number][];
  }

  /** The filter, as query params for `/api/jobs` and `/api/jobs/{id}/context`. */
  asApiParams(): URLSearchParams {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(this.values)) {
      if (value === null || value === "") continue;
      params.set(key, String(value));
    }
    return params;
  }
}
