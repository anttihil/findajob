import { useEffect, useRef, useState } from "preact/hooks";
import { getJSON } from "../../api/client";
import type { SkillGapResponse, SkillGapRow } from "../../api/types";
import { renderCoverageStrip, renderMeters, renderStatTiles } from "../../charts/charts";
import { SkillDrawer } from "./SkillDrawer";

function GapRow({ row, index, kind, onSelect }: {
  row: SkillGapRow;
  index: number;
  kind: "gap" | "strength" | "dead";
  onSelect: (skill: string) => void;
}) {
  const metersRef = useRef<HTMLDivElement>(null);
  const pct = (v: number | undefined) => `${((v || 0) * 100).toFixed(0)}%`;

  useEffect(() => {
    if (kind !== "gap" || !metersRef.current) return;
    renderMeters(metersRef.current, [
      {
        label: "blocking",
        value: row.blocking_gap,
        display: pct(row.blocking_gap),
        tooltip:
          "Share of postings you otherwise match well that require this skill — the " +
          "reason to learn it next.",
      },
      {
        label: "demand",
        value: row.demand,
        display: pct(row.demand),
        tooltip: "Share of all in-scope postings requiring it.",
      },
      {
        label: "adjacent",
        value: row.adjacency,
        display: pct(row.adjacency),
        tooltip:
          "How much of the surrounding stack you already know — a proxy for how " +
          "reachable this skill is from where you are.",
      },
    ]);
  }, [row, kind]);

  return (
    <div class={`gap-row is-${kind}`}>
      <div class="gap-row-head">
        <span class="gap-rank">{index + 1}</span>
        {kind === "gap" ? (
          <span class="gap-priority" title="Composite acquisition priority">
            {(row.priority * 100).toFixed(0)}
          </span>
        ) : (
          <span class="gap-priority is-demand" title="Share of postings requiring it">
            {pct(row.demand)}
          </span>
        )}
        <button class="gap-name" onClick={() => onSelect(row.skill)}>
          {row.label}
        </button>
        <span class="gap-tags">
          <span class="gap-tag">{row.category}</span>
          {kind === "gap" && <span class={`gap-tag is-effort-${row.effort}`}>{row.effort} effort</span>}
          {row.user_level && <span class="gap-tag is-have">level {row.user_level}</span>}
        </span>
      </div>
      {kind === "gap" && <div class="gap-row-body" ref={metersRef}></div>}
      <div class="gap-row-foot">
        <span title="Postings mentioning this skill">n={row.n_raw}</span>
        <span title="Distinct companies">{row.n_companies} companies</span>
        <span title="Wilson score interval on demand">
          demand {pct(row.demand)} [{pct(row.demand_ci_low)}–{pct(row.demand_ci_high)}]
        </span>
        {row.salary_lift && (
          <span title="Median salary ratio vs the corpus baseline">salary ×{row.salary_lift.toFixed(2)}</span>
        )}
      </div>
    </div>
  );
}

function GapList({ rows, kind, onSelect }: {
  rows: SkillGapRow[];
  kind: "gap" | "strength" | "dead";
  onSelect: (skill: string) => void;
}) {
  if (!rows.length) return <p class="chart-empty">Nothing to show yet.</p>;
  return (
    <div class="gap-list">
      {rows.map((row, i) => (
        <GapRow key={row.skill} row={row} index={i} kind={kind} onSelect={onSelect} />
      ))}
    </div>
  );
}

// Ported from `templates/tabs/skills.html` + `frontend/js/features/skills.js`.
export function SkillsPage() {
  const [windowDays, setWindowDays] = useState(90);
  const [weighting, setWeighting] = useState("interest");
  const [data, setData] = useState<SkillGapResponse | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const tilesRef = useRef<HTMLDivElement>(null);
  const coverageRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getJSON<SkillGapResponse>(`/api/skills/gap?window_days=${windowDays}&weighting=${weighting}`)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch(() => {
        if (!cancelled) setData(null);
      });
    return () => {
      cancelled = true;
    };
  }, [windowDays, weighting]);

  useEffect(() => {
    if (!data || !tilesRef.current) return;
    const p = data.provenance;
    renderStatTiles(tilesRef.current, [
      {
        value: p.n_postings ?? 0,
        label: "postings analysed",
        sub: `n_eff ${p.n_eff ?? 0}`,
        tooltip:
          "Effective sample size after reweighting. Lower than the raw count because an " +
          "uneven scrape mix costs precision.",
      },
      {
        value: p.n_good_fit ?? 0,
        label: "strong matches",
        sub: `score ≥ ${p.good_fit_threshold ?? 60}`,
        tooltip: "Postings you already match well. Blocking gaps are measured against these.",
      },
      { value: data.views.priority_gaps.length, label: "skills to acquire" },
      { value: data.views.validated_strengths.length, label: "validated strengths" },
    ]);
  }, [data]);

  useEffect(() => {
    if (!data || !coverageRef.current) return;
    const p = data.provenance;
    renderCoverageStrip(coverageRef.current, [
      { label: "weighting", value: data.weighting_effective || data.weighting_mode },
      { label: "window", value: `${data.window_days}d`, warn: p.window_below_minimum },
      { label: "strata", value: `${p.strata_used ?? 0}/${p.strata_available ?? 0}` },
      {
        label: "suppressed",
        value: data.views.suppressed.length,
        warn: data.views.suppressed.length > 0,
      },
    ]);
  }, [data]);

  const notes: { severity: "warning" | "info"; text: string }[] = [];
  if (data?.weighting_fallback_reason) notes.push({ severity: "warning", text: data.weighting_fallback_reason });
  if (data?.provenance.cold_start) {
    notes.push({
      severity: "info",
      text:
        "Building baseline — the corpus is still small, so treat these figures as " +
        "provisional. A full scrape cycle takes about 5 days.",
    });
  }
  if (data?.provenance.residual_bias_note) {
    notes.push({ severity: "info", text: data.provenance.residual_bias_note });
  }

  return (
    <section class="tab-pane active">
      {notes.length > 0 && (
        <div class="glass-card analysis-banner">
          {notes.map((n, i) => (
            <p key={i} class={`banner-note is-${n.severity}`}>
              <i class={`fa-solid ${n.severity === "warning" ? "fa-triangle-exclamation" : "fa-circle-info"}`}></i>{" "}
              {n.text}
            </p>
          ))}
        </div>
      )}

      <div class="glass-card">
        <div class="card-header-row">
          <h3>
            <i class="fa-solid fa-arrow-trend-up text-purple"></i> Skills to acquire next
          </h3>
          <div class="inline-controls">
            <select
              class="form-select-sm"
              value={String(windowDays)}
              onChange={(e) => setWindowDays(Number((e.target as HTMLSelectElement).value))}
            >
              <option value="90">Last 90 days</option>
              <option value="30">Last 30 days</option>
              <option value="14">Last 14 days</option>
            </select>
            <select
              class="form-select-sm"
              value={weighting}
              onChange={(e) => setWeighting((e.target as HTMLSelectElement).value)}
            >
              <option value="interest">Weight: my target mix</option>
              <option value="observed">Unweighted (diagnostic)</option>
            </select>
          </div>
        </div>
        <div class="stat-tile-row" ref={tilesRef}></div>
        <div class="coverage-strip" ref={coverageRef}></div>
        <p class="card-note">
          Ranked by <strong>blocking gap</strong> — how often a skill you lack appears in
          postings you <em>otherwise</em> match well. That is a more actionable signal than
          raw popularity, and it is weighted down for skills that take longer to learn.
        </p>
        {data ? (
          <GapList rows={data.views.priority_gaps} kind="gap" onSelect={setSelectedSkill} />
        ) : (
          <p class="chart-empty">Loading…</p>
        )}
      </div>

      <div class="two-col-grid">
        <div class="glass-card">
          <h3>
            <i class="fa-solid fa-circle-check text-green"></i> Validated strengths
          </h3>
          <p class="card-note">On your resume and genuinely in demand.</p>
          {data && <GapList rows={data.views.validated_strengths} kind="strength" onSelect={setSelectedSkill} />}
        </div>
        <div class="glass-card">
          <h3>
            <i class="fa-solid fa-circle-minus"></i> Dead weight
          </h3>
          <p class="card-note">On your resume, but almost nobody asks for it. Resume space with little market return.</p>
          {data && <GapList rows={data.views.dead_weight} kind="dead" onSelect={setSelectedSkill} />}
        </div>
      </div>

      <div class="glass-card">
        <div class="card-header-row">
          <h3>
            <i class="fa-solid fa-eye-slash"></i> Suppressed
          </h3>
          <span class="results-count">{data ? `${data.views.suppressed.length} skills` : ""}</span>
        </div>
        <p class="card-note">
          Reported rather than hidden: these skills lack the sample size, company spread, or
          coverage to state a number honestly.
        </p>
        <div class="suppressed-list">
          {data && data.views.suppressed.length === 0 && <p class="chart-empty">Nothing suppressed.</p>}
          {data?.views.suppressed.map((s) => (
            <span key={s.skill} class="suppressed-chip" title={s.reason}>
              {s.label}
              <em>{s.reason}</em> · n={s.n_raw}
            </span>
          ))}
        </div>
      </div>

      <SkillDrawer skill={selectedSkill} onClose={() => setSelectedSkill(null)} />
    </section>
  );
}
