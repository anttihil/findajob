import { describe, expect, it } from "vitest";
import { highlightTerms } from "../src/lib/highlightTerms";

describe("highlightTerms", () => {
  it("returns the whole text unhighlighted when there are no skills", () => {
    expect(highlightTerms("We need a Python engineer.", [])).toEqual([
      { text: "We need a Python engineer.", highlighted: false },
    ]);
  });

  it("returns empty for an empty description", () => {
    expect(highlightTerms("", ["Python"])).toEqual([{ text: "", highlighted: false }]);
    expect(highlightTerms(null, ["Python"])).toEqual([{ text: "", highlighted: false }]);
  });

  it("highlights a whole-word match case-insensitively", () => {
    const segments = highlightTerms("We need a python engineer.", ["Python"]);
    expect(segments).toEqual([
      { text: "We need a ", highlighted: false },
      { text: "python", highlighted: true },
      { text: " engineer.", highlighted: false },
    ]);
  });

  it("does not match a skill as a substring of a longer word", () => {
    const segments = highlightTerms("Javascript and TypeScript experience.", ["Script"]);
    // "Script" is a substring of both "Javascript" and "TypeScript" but not a whole word.
    expect(segments.every((s) => !s.highlighted)).toBe(true);
  });

  it("matches punctuation-bearing skills without word boundaries", () => {
    for (const skill of ["node.js", "CI/CD", "front-end"]) {
      const text = `Experience with ${skill} required.`;
      const segments = highlightTerms(text, [skill]);
      const highlighted = segments.filter((s) => s.highlighted).map((s) => s.text);
      expect(highlighted).toEqual([skill]);
    }
  });

  it("prefers the longest match when skills overlap", () => {
    const segments = highlightTerms("We use React Native heavily.", ["React", "React Native"]);
    const highlighted = segments.filter((s) => s.highlighted).map((s) => s.text);
    expect(highlighted).toEqual(["React Native"]);
  });

  it("highlights every occurrence of a matched skill", () => {
    const segments = highlightTerms("Python, then more Python.", ["Python"]);
    const highlighted = segments.filter((s) => s.highlighted).map((s) => s.text);
    expect(highlighted).toEqual(["Python", "Python"]);
  });

  it("skips empty/falsy skill entries", () => {
    const segments = highlightTerms("Python engineer.", ["", "Python"]);
    expect(segments.some((s) => s.highlighted && s.text === "Python")).toBe(true);
  });
});
