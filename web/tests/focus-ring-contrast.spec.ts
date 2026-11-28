// The keyboard focus ring of the media input and its Library dialog is 2 px in --focus-ring, the dark teal of Run and
// Make, with a 2 px gap. A ring must read at 3:1 or more (WCAG 1.4.11) against the page the dialog sits on and against
// the hover fill of the row or tile it rings, in both themes. This reads the shipped tokens, so a token edit that
// weakens the ring fails here.
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { FOCUS_RING } from "../src/components/media-input/focus-ring";

const css = readFileSync(path.join(__dirname, "../src/styles/tokens.css"), "utf8");

function block(selector: RegExp): Record<string, string> {
  const open = css.search(selector);
  const start = css.indexOf("{", open);
  const end = css.indexOf("\n}", start);
  const tokens: Record<string, string> = {};
  for (const [, name, value] of css.slice(start, end).matchAll(/--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) tokens[name] = value;
  return tokens;
}

const themes = { light: block(/^:root\s*\{/m), dark: block(/^\.dark\s*\{/m) };

function luminance(hex: string): number {
  const channel = (offset: number) => {
    const value = parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

export function contrast(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

describe.each(["light", "dark"] as const)("focus ring contrast, %s theme", (theme) => {
  const tokens = themes[theme];

  test("the ring is at least 3:1 against the page and the dialog", () => {
    expect(contrast(tokens["focus-ring"], tokens.background)).toBeGreaterThanOrEqual(3);
  });

  test("the ring is at least 3:1 against the hover fill (the scroll-edge tone)", () => {
    expect(contrast(tokens["focus-ring"], tokens.border)).toBeGreaterThanOrEqual(3);
  });

  test("the hover fill is visibly darker than the page, at the scroll-edge tone", () => {
    expect(contrast(tokens.border, tokens.background)).toBeGreaterThanOrEqual(1.2);
  });
});

test("the light ring is the dark teal of Run (--primary)", () => {
  expect(themes.light["focus-ring"].toLowerCase()).toBe(themes.light.primary.toLowerCase());
});

test("the ring is 2 px wide with a 2 px gap, drawn on keyboard focus only", () => {
  expect(FOCUS_RING).toContain("focus-visible:ring-2");
  expect(FOCUS_RING).toContain("focus-visible:ring-offset-2");
  expect(FOCUS_RING).toContain("focus-visible:ring-focus-ring");
  expect(FOCUS_RING).not.toMatch(/ring-\[|ring-1|\/50/);
});
