import { useEffect, useState } from "preact/hooks";

// Browser preferences are deliberately local to this device and browser profile. Unlike
// IndexedDB, localStorage is enough for this small amount of non-sensitive UI state and
// lets the application recover gracefully when storage is unavailable or corrupt.
const PREFIX = "findajob.preferences.";

function storageKey(key: string): string {
  return `${PREFIX}${key}`;
}

export function readPreference<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(storageKey(key));
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function writePreference<T>(key: string, value: T): void {
  try {
    window.localStorage.setItem(storageKey(key), JSON.stringify(value));
  } catch {
    // Storage can be disabled or full. Preferences must never prevent the UI working.
  }
}

export function clearPreference(key: string): void {
  try {
    window.localStorage.removeItem(storageKey(key));
  } catch {
    // See writePreference().
  }
}

/** State that survives browser restarts but remains local to the current browser profile. */
export function useStoredPreference<T>(key: string, fallback: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => readPreference(key, fallback));

  useEffect(() => {
    writePreference(key, value);
  }, [key, value]);

  return [value, setValue];
}
