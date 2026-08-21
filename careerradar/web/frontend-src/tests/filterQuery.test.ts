import { describe, expect, it } from "vitest";
import { FilterQuery } from "../src/lib/filterQuery";

describe("FilterQuery", () => {
  it("defaults status to unread with an empty query string", () => {
    const q = new FilterQuery("");
    expect(q.status).toBe("unread");
    expect(q.sort).toBe("fit");
    expect(q.limit).toBe(50);
    expect(q.max_tier).toBeNull();
  });

  it("reads values off the query string", () => {
    const q = new FilterQuery("?status=saved&country=US&max_tier=3&offset=50&q=python");
    expect(q.status).toBe("saved");
    expect(q.country).toBe("US");
    expect(q.max_tier).toBe(3);
    expect(q.offset).toBe(50);
    expect(q.q).toBe("python");
  });

  describe("url()", () => {
    it("drops keys left at their default", () => {
      const q = new FilterQuery("?status=saved");
      // status stays saved (unchanged), sort is left at its default and should not appear.
      expect(q.url({ status: "saved" })).toBe("/?status=saved");
    });

    it("returns the bare path when every filter is default", () => {
      const q = new FilterQuery("");
      expect(q.url()).toBe("/");
    });

    it("always resets offset to 0", () => {
      const q = new FilterQuery("?offset=100");
      expect(q.url({ status: "saved" })).toBe("/?status=saved");
    });

    it("changes exactly the overridden filter", () => {
      const q = new FilterQuery("?status=unread&country=US");
      expect(q.url({ country: "FI" })).toBe("/?country=FI");
    });

    it("includes search query q when set and resets offset", () => {
      const q = new FilterQuery("?status=unread&offset=50");
      expect(q.url({ q: "devops" })).toBe("/?q=devops");
    });

    it("drops q when cleared to empty string", () => {
      const q = new FilterQuery("?q=python");
      expect(q.url({ q: "" })).toBe("/");
    });
  });

  describe("page()", () => {
    it("preserves the filter and sets offset", () => {
      const q = new FilterQuery("?status=saved&q=kubernetes");
      expect(q.page(50)).toBe("/?status=saved&q=kubernetes&offset=50");
    });

    it("clamps negative offsets to 0, which is then dropped as default", () => {
      const q = new FilterQuery("?status=saved");
      expect(q.page(-10)).toBe("/?status=saved");
    });
  });

  describe("withJob() / withoutJob()", () => {
    it("adds job to the query string", () => {
      const q = new FilterQuery("?status=saved");
      expect(q.withJob(42)).toBe("/?status=saved&job=42");
    });

    it("omits job when everything else is default", () => {
      const q = new FilterQuery("");
      expect(q.withJob(42)).toBe("/?job=42");
    });

    it("drops job and returns the bare filter", () => {
      const q = new FilterQuery("?status=saved&job=42");
      expect(q.withoutJob()).toBe("/?status=saved");
    });

    it("returns the root path when nothing else is set", () => {
      const q = new FilterQuery("?job=42");
      expect(q.withoutJob()).toBe("/");
    });
  });

  it("isActive compares against the current value", () => {
    const q = new FilterQuery("?status=saved");
    expect(q.isActive("status", "saved")).toBe(true);
    expect(q.isActive("status", "applied")).toBe(false);
  });

  describe("hiddenFields()", () => {
    it("excludes offset and the named field, and default values", () => {
      const q = new FilterQuery("?status=saved&country=US&sort=fit_score&offset=50&q=python");
      const fields = Object.fromEntries(q.hiddenFields("sort"));
      expect(fields).toEqual({ status: "saved", country: "US", q: "python" });
    });
  });

  describe("asApiParams()", () => {
    it("omits null/empty values but keeps explicit defaults for the endpoint", () => {
      const q = new FilterQuery("?status=saved&country=US&q=rust");
      const params = q.asApiParams();
      expect(params.get("status")).toBe("saved");
      expect(params.get("country")).toBe("US");
      expect(params.get("q")).toBe("rust");
      expect(params.has("access")).toBe(false);
      expect(params.get("sort")).toBe("fit");
    });
  });
});
