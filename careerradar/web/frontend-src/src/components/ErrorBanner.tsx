import { appErrors, dismissAppErrors } from "../state/errors";

export function ErrorBanner() {
  if (appErrors.value.length === 0) return null;

  return (
    <div class="sync-error-banner glass-card" id="app-error-banner">
      <div class="banner-title">
        <span>
          <i class="fa-solid fa-circle-exclamation text-red"></i> Request failures
        </span>
        <button class="close-banner-btn" onClick={dismissAppErrors}>
          <i class="fa-solid fa-xmark"></i>
        </button>
      </div>
      <ul class="sync-errors-list">
        {appErrors.value.map((err, i) => (
          <li key={i} class="sync-error-item severity-error">
            <strong>{err.what}</strong>
            <span>{err.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
