// One place where the browser talks to the API. Ported from `frontend/js/api.js`.
//
// Failures are pushed into the `appErrors` signal instead of being logged and dropped, so
// an analytics panel that renders nothing is distinguishable from one with nothing to show.

import { appErrors } from "../state/errors";

export class ApiError extends Error {
  url: string;
  status: number;
  body: string;

  constructor(url: string, status: number, body: string) {
    super(`${status} from ${url}`);
    this.url = url;
    this.status = status;
    this.body = body;
  }
}

async function bodyText(res: Response): Promise<string> {
  try {
    return await res.text();
  } catch {
    return "";
  }
}

export async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new ApiError(url, res.status, await bodyText(res));
  return res.json() as Promise<T>;
}

export async function postJSON<T>(url: string, payload: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new ApiError(url, res.status, await bodyText(res));
  return res.json() as Promise<T>;
}

export async function putJSON<T>(url: string, payload: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new ApiError(url, res.status, await bodyText(res));
  return res.json() as Promise<T>;
}

export async function deleteJSON<T = { success: boolean }>(url: string): Promise<T> {
  const res = await fetch(url, {
    method: "DELETE",
  });
  if (!res.ok) throw new ApiError(url, res.status, await bodyText(res));
  return res.json() as Promise<T>;
}

// A 404 that is a legitimate answer rather than a fault -- the resource is simply absent.
// Callers that use this are saying the absence is expected; anything else still surfaces.
export async function getJSONOrNull<T>(url: string): Promise<T | null> {
  try {
    return await getJSON<T>(url);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export function reportError(what: string, err: unknown): void {
  console.error(what, err);
  const detail =
    err instanceof ApiError ? `HTTP ${err.status}` : ((err as Error)?.message ?? String(err));
  appErrors.value = [...appErrors.value, { what, detail }];
}

// Wrap a loader so one broken panel cannot take the rest of the page with it.
export async function guard<T>(what: string, fn: () => Promise<T>): Promise<T | null> {
  try {
    return await fn();
  } catch (err) {
    reportError(what, err);
    return null;
  }
}
