import type { MarketProgressionPoint } from "../../api/types";

export function ProgressionChart({ points }: { points: MarketProgressionPoint[] }) {
  const width = 900;
  const height = 300;
  const left = 60;
  const right = 24;
  const top = 28;
  const bottom = 52;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  // Both series use exactly the same linear count scale, starting at zero.
  const max = Math.max(4, Math.ceil(Math.max(...points.map((p) => p.total_postings), 0) / 4) * 4);
  const x = (index: number) =>
    left + (points.length === 1 ? plotWidth / 2 : (index * plotWidth) / (points.length - 1));
  const y = (count: number) => top + plotHeight * (1 - count / max);
  const series = [
    { key: "total_postings" as const, label: "Jobs found", dashed: false },
    { key: "strong_fits" as const, label: "Strong fits", dashed: true },
  ];
  const dateTicks = [
    ...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1]),
  ].filter((i) => i >= 0);
  return (
    <div class="market-progression">
      <div class="market-chart-legend">
        <span>━ Jobs found</span>
        <span>┄ Strong fits</span>
        <span>Daily counts · same scale</span>
      </div>
      <div class="market-chart-scroll">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          class="market-trend-chart"
          role="img"
          aria-label="Daily jobs found and strong fits by discovery date, on the same zero-based count scale"
        >
          {[0, 1, 2, 3, 4].map((tick) => {
            const value = (max * tick) / 4;
            return (
              <g key={tick}>
                <line
                  x1={left}
                  x2={width - right}
                  y1={y(value)}
                  y2={y(value)}
                  class="market-chart-grid"
                />
                <text x={left - 10} y={y(value) + 4} text-anchor="end">
                  {value.toLocaleString()}
                </text>
              </g>
            );
          })}
          <text x={left} y={15}>
            Count
          </text>
          {dateTicks.map((index) => (
            <text
              key={index}
              x={x(index)}
              y={height - 22}
              text-anchor={
                index === 0 && points.length > 1
                  ? "start"
                  : index === points.length - 1 && points.length > 1
                    ? "end"
                    : "middle"
              }
            >
              {points[index]?.date}
            </text>
          ))}
          {series.map(({ key, dashed }) => (
            <polyline
              key={key}
              data-series={key}
              points={points.map((point, index) => `${x(index)},${y(point[key])}`).join(" ")}
              fill="none"
              stroke="currentColor"
              stroke-width={dashed ? 3 : 2}
              stroke-dasharray={dashed ? "7 5" : undefined}
            />
          ))}
          {points.map((point, index) => (
            <g key={point.date}>
              <title>
                {point.date}: {point.total_postings} jobs found, {point.scored_postings} scored,{" "}
                {point.strong_fits} strong fits
              </title>
              <circle cx={x(index)} cy={y(point.total_postings)} r={3} fill="currentColor" />
              <rect
                x={x(index) - 3}
                y={y(point.strong_fits) - 3}
                width={6}
                height={6}
                fill="var(--bg-screen)"
                stroke="currentColor"
                stroke-width={1.5}
              />
            </g>
          ))}
        </svg>
      </div>
    </div>
  );
}
