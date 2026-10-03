import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const root = new URL("../..", import.meta.url);
const source = (path) => readFileSync(new URL(path, root), "utf8");

const charts = source("src/components/ui/charts.tsx");
const dataTable = source("src/components/ui/data-table.tsx");
const surface = source("src/components/ui/surface.tsx");
const operational = source("src/components/ui/operational.tsx");
const legacyCss = source("src/styles/legacy-components.css");

test("mobile navigation is route-agnostic and delegates current state to the shell", () => {
  const mobileTabBar = surface.slice(
    surface.indexOf("export function MobileTabBar"),
    surface.indexOf("export function SectionKicker"),
  );
  assert.match(
    surface,
    /export interface MobileNavItem[\s\S]*id: string;[\s\S]*current\?: boolean/,
  );
  assert.match(surface, /export const MobileTabAction = forwardRef/);
  assert.match(
    mobileTabBar,
    /aria-current=\{item\.current \? "page" : undefined\}/,
  );
  assert.doesNotMatch(
    mobileTabBar,
    /usePathname|pathname|startsWith\(|["']\/admin/,
  );
});

test("charts refuse invented two-sample trends and expose one accessible equivalent", () => {
  assert.match(charts, /validChartSeries\(series\)\.length >= 3/);
  assert.match(charts, /mode: "summary"/);
  assert.match(charts, /mode: "data-table"/);
  assert.match(charts, /className="vf-chart-visual" aria-hidden/);
  assert.match(charts, /<caption>\{accessibility\.label\}<\/caption>/);
  assert.doesNotMatch(charts, /role: "img"/);
});

test("mobile cards use a sibling primary control and empty advisories remain absent", () => {
  assert.match(dataTable, /vf-mobile-data-card-primary/);
  assert.match(dataTable, /mobileRowActionLabel/);
  assert.match(dataTable, /vf-mobile-data-card-menu/);
  assert.ok(
    dataTable.indexOf("vf-mobile-data-card-primary") <
      dataTable.indexOf("vf-mobile-data-card-head"),
  );
  assert.doesNotMatch(
    dataTable,
    /vf-mobile-data-card[\s\S]{0,700}role: "button"/,
  );
  assert.match(operational, /if \(props\.items\.length === 0\) return null/);
  assert.match(
    legacyCss,
    /vf-mobile-data-card-actionable \.vf-mobile-data-card-head,[\s\S]*pointer-events: none/,
  );
});
