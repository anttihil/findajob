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
        tooltip: "Share of matching postings requiring this skill.",
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
        tooltip: "Share of related stack technologies you already know.",
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
          {row.user_has && <span class="gap-tag is-have">in profile</span>}
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
  const [data, setData] = useState<SkillGapResponse | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const tilesRef = useRef<HTMLDivElement>(null);
  const coverageRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getJSON<SkillGapResponse>(`/api/skills/gap?window_days=${windowDays}`)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch(() => {
        if (!cancelled) setData(null);
      });
    return () => {
      cancelled = true;
    };
  }, [windowDays]);

  useEffect(() => {
    if (!data || !tilesRef.current) return;
    const p = data.provenance;
    renderStatTiles(tilesRef.current, [
      {
        value: p.n_postings ?? 0,
        label: "postings analysed",
        tooltip: "Eligible postings in the window.",
      },
      {
        value: p.n_good_fit ?? 0,
        label: "strong matches",
        sub: `score ≥ ${p.good_fit_threshold ?? 60}`,
        tooltip: "Postings meeting the match threshold for gap analysis.",
      },
      { value: data.views.priority_gaps.length, label: "skills to acquire" },
      { value: data.views.validated_strengths.length, label: "validated strengths" },
    ]);
  }, [data]);

  useEffect(() => {
    if (!data || !coverageRef.current) return;
    const p = data.provenance;
    renderCoverageStrip(coverageRef.current, [
      { label: "window", value: `${data.window_days}d`, warn: p.window_below_minimum },
      {
        label: "suppressed",
        value: data.views.suppressed.length,
        warn: data.views.suppressed.length > 0,
      },
    ]);
  }, [data]);

  const notes: { severity: "warning" | "info"; text: string }[] = [];
  if (data?.provenance.cold_start) {
    notes.push({
      severity: "info",
      text: "Building baseline — small corpus size; full scrape cycle takes ~5 days.",
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
          </div>
        </div>
        <div class="stat-tile-row" ref={tilesRef}></div>
        <div class="coverage-strip" ref={coverageRef}></div>
        <p class="card-note">
          Ranked by <strong>blocking gap</strong> — missing skills across matching
          postings.
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
          <p class="card-note">On your resume and in active demand.</p>
          {data && <GapList rows={data.views.validated_strengths} kind="strength" onSelect={setSelectedSkill} />}
        </div>
        <div class="glass-card">
          <h3>
            <i class="fa-solid fa-circle-minus"></i> Dead weight
          </h3>
          <p class="card-note">On your resume with minimal market demand.</p>
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
          Skills with insufficient sample size, company spread, or coverage for reliable metrics.
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
