import axe from "axe-core";
import { expect } from "vitest";

/**
 * Runs axe-core at WCAG 2.2 AA over a rendered subtree. jsdom has no layout engine, so colour
 * contrast cannot be computed here; that is covered by `scripts/check-contrast.mjs` over the
 * tokens, and by the Playwright run in a real browser engine.
 */
export async function expectNoAccessibilityViolations(container: Element): Promise<void> {
  const results = await axe.run(container, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
    rules: { "color-contrast": { enabled: false }, region: { enabled: false } },
  });
  const summary = results.violations.map(
    (v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(" ")).join(", ")})`,
  );
  expect(summary).toEqual([]);
}
