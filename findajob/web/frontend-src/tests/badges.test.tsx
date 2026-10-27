import { describe, expect, it } from "vitest";
import { render } from "@testing-library/preact";
import { VerdictPanel } from "../src/routes/dashboard/VerdictPanel";
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
    seniority: null,
    is_remote: null,
    city: null,
    region: null,
    date_posted: null,
    salary_min: null,
    salary_max: null,
    salary_currency: null,
    salary_annual_usd: null,
    fit: null,
    reason_type: null,
    reason_description: null,
    pipeline_state: null,
    liveness: "unknown",
    ...overrides,
  };
}

describe("TierBadge", () => {
  it("shows fit badge when fit is true", () => {
    const { getByText, container } = render(
      <TierBadge job={job({ fit: true, reason_type: "match" })} />,
    );
    expect(getByText("Fit · match")).toBeTruthy();
    expect(container.querySelector(".badge-top")).toBeTruthy();
  });

  it("shows no fit badge when fit is false", () => {
    const { getByText, container } = render(
      <TierBadge job={job({ fit: false, reason_type: "skills" })} />,
    );
    expect(getByText("skills")).toBeTruthy();
    expect(container.querySelector(".badge-blocked")).toBeTruthy();
  });

  it("shows 'no fit' when fit is false without reason_type", () => {
    const { getByText, container } = render(
      <TierBadge job={job({ fit: false, reason_type: null })} />,
    );
    expect(getByText("no fit")).toBeTruthy();
    expect(container.querySelector(".badge-blocked")).toBeTruthy();
  });

  it("shows 'unscored' when fit is null", () => {
    const { getByText, container } = render(<TierBadge job={job({ fit: null })} />);
    expect(getByText("unscored")).toBeTruthy();
    expect(container.querySelector(".badge-unscored")).toBeTruthy();
  });
});

it("shows review priority independently of qualification", () => {
  const { getByText } = render(<TierBadge job={job({ fit: false, priority: "consider" })} />);
  expect(getByText("Worth considering")).toBeTruthy();
});

it("shows an unreviewed posting without an assessment", () => {
  const { getByText } = render(<VerdictPanel job={job({ fit: null, stage: "unreviewed" })} />);
  expect(getByText("Not assessed yet.")).toBeTruthy();
});

it("displays priority and explanation with a null fit judgment", () => {
  const { getByText, queryByText } = render(
    <VerdictPanel
      job={job({
        fit: null,
        priority: "consider",
        stage: "assessed",
        reason_description: "Credible adjacent work.",
        profile_stale: true,
      })}
    />,
  );
  expect(getByText("Worth considering")).toBeTruthy();
  expect(getByText("Credible adjacent work.")).toBeTruthy();
  expect(getByText("Assessed against an earlier profile. Reassessment is explicit.")).toBeTruthy();
  expect(queryByText("Not assessed yet.")).toBeNull();
  expect(queryByText("Qualification stretch or gap")).toBeNull();
});
