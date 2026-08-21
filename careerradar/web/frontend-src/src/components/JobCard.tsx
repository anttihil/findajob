import { Link } from "wouter-preact";
import type { Job } from "../api/types";
import type { FilterQuery } from "../lib/filterQuery";
import { shortDate } from "../lib/format";
import { AccessBadge } from "./badges/AccessBadge";
import { TierBadge } from "./badges/TierBadge";

// Ported from `macros/job_card.html`.
export function JobCard({ job, query }: { job: Job; query: FilterQuery }) {
  return (
    <Link href={query.withJob(job.id)} class="job-card">
      <div class="job-card-details">
        <div class="job-card-header">
          <h4>{job.title}</h4>
        </div>
        <div class="job-company">{job.company}</div>
        <div class="job-meta-row">
          <span>
            <i class="fa-solid fa-location-dot"></i> {job.location || "Remote"}
          </span>
          <AccessBadge access={job.access} />
          <span>
            <i class="fa-solid fa-clock"></i>{" "}
            {job.date_posted ? `Posted ${shortDate(job.date_posted)}` : `Found ${shortDate(job.date_found)}`}
          </span>
        </div>
        <div class="matched-skills-preview">
          {job.matched_skills.slice(0, 5).map((skill) => (
            <span key={skill} class="skill-tag-sm">
              {skill}
            </span>
          ))}
          {job.matched_skills.length > 5 && (
            <span class="skill-tag-sm">+{job.matched_skills.length - 5} more</span>
          )}
        </div>
      </div>
      <div class="job-card-right">
        <div class="match-badge-wrap">
          {job.status !== "unread" && <span class={`job-status-indicator ${job.status}`}>{job.status}</span>}
          <TierBadge job={job} />
        </div>
        <span class="source-tag">{job.source}</span>
      </div>
    </Link>
  );
}
