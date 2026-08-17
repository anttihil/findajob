import type { Dossier } from "../../api/types";
import { BulletList } from "../../components/BulletList";
import { hostname } from "../../lib/format";

// Ported from `partials/dossier.html`.
export function DossierPanel({ dossier }: { dossier: Dossier }) {
  const intel = dossier.intel ?? {};
  const facts = [
    intel.size ? `size ${intel.size}` : "",
    intel.stage ? `stage ${intel.stage}` : "",
    intel.funding ? `funding ${intel.funding}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <>
      {intel.summary && <p>{intel.summary}</p>}
      {facts && <p class="dossier-facts">{facts}</p>}

      {intel.concerns && intel.concerns.length > 0 && (
        <>
          <h5 class="verdict-h5 text-red">Concerns</h5>
          <BulletList items={intel.concerns} />
        </>
      )}

      {intel.application_angle && (
        <>
          <h5 class="verdict-h5">Angle</h5>
          <p>{intel.application_angle}</p>
        </>
      )}

      {dossier.contacts.length > 0 && (
        <>
          <h5 class="verdict-h5">People</h5>
          <ul class="verdict-list">
            {dossier.contacts.map((contact, i) => (
              <li key={i}>
                {contact.name}
                {contact.role ? ` — ${contact.role}` : ""}
                {contact.public_url && (
                  <a href={contact.public_url} target="_blank" rel="noopener">
                    ↗
                  </a>
                )}
                <br />
                <small>{contact.relevance || ""}</small>
              </li>
            ))}
          </ul>
        </>
      )}

      {dossier.nearby_jobs.length > 0 && (
        <>
          <h5 class="verdict-h5">Other openings ({dossier.nearby_jobs.length})</h5>
          <ul class="verdict-list">
            {dossier.nearby_jobs.slice(0, 8).map((near, i) => (
              <li key={i}>
                {near.url ? (
                  <a href={near.url} target="_blank" rel="noopener">
                    {near.title}
                  </a>
                ) : (
                  near.title
                )}{" "}
                — {near.company} <small>({near.source})</small>
              </li>
            ))}
          </ul>
        </>
      )}

      {dossier.sources.length > 0 ? (
        <p class="dossier-sources">
          {dossier.sources.length} source(s):{" "}
          {dossier.sources.slice(0, 6).map((url, i, arr) => (
            <span key={url}>
              <a href={url} target="_blank" rel="noopener">
                {hostname(url)}
              </a>
              {i < arr.length - 1 ? ", " : ""}
            </span>
          ))}
        </p>
      ) : (
        <p class="dossier-sources">No web sources — company intel was not gathered.</p>
      )}
    </>
  );
}
