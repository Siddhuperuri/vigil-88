// Fails when any text colour in tokens.css is below WCAG AA (4.5:1) on a surface it can appear on.
// Runs in the quality gate (08 §9): contrast is verified by a script over the tokens, not by eye.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const TOKENS = fileURLToPath(new URL("../src/design/tokens.css", import.meta.url));
const MIN = 4.5;
const SURFACES = ["--vg-void", "--vg-panel", "--vg-raised"];
const TEXT = [
  "--vg-text",
  "--vg-text-2",
  "--vg-text-dim",
  "--vg-signal",
  "--vg-sev-info",
  "--vg-sev-low",
  "--vg-sev-moderate",
  "--vg-sev-high",
  "--vg-sev-critical",
];
// The focus ring is not text, but WCAG 1.4.11 wants 3:1 against what it sits on.
const NON_TEXT = [["--vg-focus", 3]];

function parseBlocks(css) {
  const blocks = new Map();
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = m[1].trim();
    const vars = {};
    for (const v of m[2].matchAll(/(--vg-[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;/g)) {
      vars[v[1]] = v[2];
    }
    if (Object.keys(vars).length > 0) blocks.set(selector, { ...blocks.get(selector), ...vars });
  }
  return blocks;
}

function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const lin = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

export function ratio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const withoutComments = readFileSync(TOKENS, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const blocks = parseBlocks(withoutComments);
const themes = [...blocks.entries()].filter(([, vars]) => SURFACES.every((s) => vars[s]));
const failures = [];
let checked = 0;
for (const [selector, vars] of themes) {
  for (const surface of SURFACES) {
    for (const name of TEXT) {
      checked += 1;
      const r = ratio(vars[name], vars[surface]);
      if (r < MIN)
        failures.push(`${selector}: ${name} on ${surface} is ${r.toFixed(2)}:1 (< ${MIN})`);
    }
    for (const [name, min] of NON_TEXT) {
      checked += 1;
      const r = ratio(vars[name], vars[surface]);
      if (r < min)
        failures.push(`${selector}: ${name} on ${surface} is ${r.toFixed(2)}:1 (< ${min})`);
    }
  }
}
if (themes.length < 3)
  failures.push(`expected dark, light and auto-light theme blocks, found ${themes.length}`);
for (const failure of failures) console.error(failure);
console.log(
  failures.length === 0
    ? `contrast: ${checked} pairs across ${themes.length} theme blocks, all at or above AA`
    : `contrast: ${failures.length} failure(s)`,
);
process.exit(failures.length === 0 ? 0 : 1);
