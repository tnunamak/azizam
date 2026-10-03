import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const root = new URL("../..", import.meta.url);
const source = (path) => readFileSync(new URL(path, root), "utf8");

const charts = source("src/components/ui/charts.tsx");
const dataTable = source("src/components/ui/data-table.tsx");
const surface = source("src/components/ui/surface.tsx");
const layout = source("src/app/admin/layout.tsx");
const operational = source("src/components/ui/operational.tsx");
const browserFixture = source(
  "src/components/acceptance/shared-primitives-browser.tsx",
);
const browserTest = source("tests/playwright/shared-primitives.spec.ts");
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
  assert.match(
    layout,
    /Overview[\s\S]*Status[\s\S]*Services[\s\S]*Apps & keys/,
  );
  assert.match(layout, /const moreItems = NAV_ITEMS\.slice\(4\)/);
  assert.match(
    layout,
    /<MobileTabAction[\s\S]*label="More"[\s\S]*aria-haspopup="dialog"/,
  );
  assert.match(layout, /function MobileNavigationSheet/);
  assert.match(layout, /Choose an admin section or account action\./);
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

test("browser evidence mounts shared React output instead of a hand-authored DOM mirror", () => {
  assert.match(browserFixture, /import \{[\s\S]*DataTable/);
  assert.match(browserFixture, /<DataTable/);
  assert.match(browserTest, /p1v-fixture/);
  assert.match(browserTest, /scrollWidth/);
  assert.match(browserTest, /cardDescendants/);
  assert.match(browserTest, /stage\?\.height/);
  assert.doesNotMatch(browserTest, /shared-primitives\.html/);
});
