import { beforeEach, describe, expect, it } from "vitest";
import {
  clearPreference,
  readPreference,
  writePreference,
} from "../src/lib/preferences";

describe("browser preferences", () => {
  beforeEach(() => window.localStorage.clear());

  it("round-trips a preference through localStorage", () => {
    writePreference("market-search-query", "platform engineer");

    expect(readPreference("market-search-query", "")).toBe("platform engineer");
  });

  it("uses the fallback for missing or malformed data", () => {
    expect(readPreference("missing", "all")).toBe("all");
    window.localStorage.setItem("careerradar.preferences.invalid", "not json");
    expect(readPreference("invalid", "all")).toBe("all");
  });

  it("clears a saved preference", () => {
    writePreference("dashboard-filter", "country=FI");
    clearPreference("dashboard-filter");

    expect(readPreference("dashboard-filter", "")).toBe("");
  });
});
