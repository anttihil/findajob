import { describe, expect, it } from "vitest";
import { render } from "@testing-library/preact";
import { TierBadge } from "../src/components/badges/TierBadge";
import type { Job } from "../src/api/types";

function job(overrides: Partial<Job>): Job {
  return {
    id: 1,
    job_key: "k",
    title: "t",
    company: "c",
    location: null,
    country: null,
    url: "https://example.com",
    description: null,
    source: "indeed",
    match_score: null,
    matched_skills: [],
    resume_match: null,
    status: "unread",
    date_found: null,
    date_applied: null,
    access: null,
    role_family: null,
    seniority: null,
    is_remote: null,
    city: null,
    region: null,
    date_posted: null,
    salary_min: null,
    salary_max: null,
    salary_currency: null,
    salary_annual_usd: null,
    fit_score: null,
    verdict: null,
    eligibility: null,
    role_match: null,
    capability_match: null,
    seniority_gap: null,
    pareto_tier: null,
    core_requirements: [],
    requirement_assessments: [],
    hard_blockers: [],
    key_gaps: [],
    strengths: [],
    reasoning: null,
    role_summary: null,
    evidence_quality: null,
    pipeline_state: null,
    liveness: "unknown",
    requirement_summary: null,
    ...overrides,
  };
}

describe("TierBadge", () => {
  it("shows 'blocked' regardless of tier when eligibility is blocked", () => {
    const { getByText } = render(<TierBadge job={job({ eligibility: "blocked", pareto_tier: 1 })} />);
    expect(getByText("blocked")).toBeTruthy();
  });

  it("shows 'unscored' when there is no tier yet", () => {
    const { getByText } = render(<TierBadge job={job({ pareto_tier: null })} />);
    expect(getByText("unscored")).toBeTruthy();
  });

  it("uses badge-top for tier <= 2", () => {
    const { container } = render(<TierBadge job={job({ pareto_tier: 2, eligibility: "eligible" })} />);
    expect(container.querySelector(".badge-top")).toBeTruthy();
  });

  it("uses badge-good for tier 3-4", () => {
    const { container } = render(<TierBadge job={job({ pareto_tier: 4, eligibility: "eligible" })} />);
    expect(container.querySelector(".badge-good")).toBeTruthy();
  });

  it("uses badge-far for tier 5 and beyond", () => {
    const { container } = render(<TierBadge job={job({ pareto_tier: 5, eligibility: "eligible" })} />);
    expect(container.querySelector(".badge-far")).toBeTruthy();
  });

  it("appends an asterisk for conditional eligibility", () => {
    const { container } = render(<TierBadge job={job({ pareto_tier: 1, eligibility: "conditional" })} />);
    expect(container.textContent).toContain("*");
  });
});
