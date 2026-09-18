import { Link } from "wouter-preact";
import type { Job } from "../api/types";
import type { FilterQuery } from "../lib/filterQuery";
import { retroDate } from "../lib/format";
import { TierBadge } from "./badges/TierBadge";

export function JobCard({ job, query }: { job: Job; query: FilterQuery }) {
  return (
    <Link href={query.withJob(job.id)} class="job-card">
      <div class="job-card-details">
        <div class="job-card-timestamp">
          {retroDate(job.date_posted || job.date_found)}
        </div>
        <div class="job-card-header">
          <h4>{job.title}</h4>
        </div>
        <div class="job-company">{job.company}</div>

        <div class="job-meta-row">
          <span>
            <i class="fa-solid fa-location-dot"></i> {job.location || "Remote"}
          </span>
          <span>
            <i class="fa-solid fa-server"></i> {job.source}
          </span>
          {(job.listing_count ?? 1) > 1 && (
            <span title="Same company and normalized title; other listings remain available in the database">
              <i class="fa-solid fa-layer-group"></i> {job.listing_count} listings
            </span>
          )}
        </div>
        <div class="matched-skills-preview">
          {job.matched_skills.slice(0, 5).map((skill) => (
            <span key={skill} class="skill-tag-sm">
              [{skill}]
            </span>
          ))}
          {job.matched_skills.length > 5 && (
            <span class="skill-tag-sm">+{job.matched_skills.length - 5} more</span>
          )}
        </div>
      </div>
      <div class="job-card-right">
        <div class="match-badge-wrap">
          {job.status !== "unread" && <span class={`job-status-indicator ${job.status}`}>[{job.status}]</span>}
          {job.tailored_resume_id != null && (
            <span class="job-status-indicator" style={{ borderColor: "#8b5cf6", color: "#8b5cf6" }} title="Tailored resume available">
              <i class="fa-solid fa-file-lines"></i> [Resume]
            </span>
          )}
          <TierBadge job={job} />
        </div>
        <div class="job-card-action">
          <span>VIEW DETAILS ▶</span>
        </div>
      </div>
    </Link>
  );
}
