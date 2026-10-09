// The quality sweep. One test per route × role × width × theme; each visits
// every state of the route and checks the rules in rules.ts plus axe-core.
// Findings are written as JSON per test; report.mjs merges them.
import { test, expect, type Page, type BrowserContext } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { ROUTES, discoverAppRoutes, type SweepContext, type SweepRoute, type SweepState } from "./routes";
import { lintText, loadTermList, type Role, type RuleId, type Severity, type TextItem } from "./rules";
import { sweepPaths, sweepTarget } from "./target";

const target = sweepTarget();
const paths = sweepPaths(target);
const probeSource = readFileSync(path.join(__dirname, "probe-page.js"), "utf8").replace(/^\s*\/\/.*$/gm, "");
const research = loadTermList("research-notes.txt");
const jargon = loadTermList("member-jargon.txt");

const WIDTHS = (process.env.SWEEP_WIDTHS ?? "390,1440").split(",").map(Number);
const THEMES = (process.env.SWEEP_THEMES ?? "light,dark").split(",") as Array<"light" | "dark">;
const ROLES = (process.env.SWEEP_ROLES ?? "owner,member,anonymous").split(",") as Role[];
const ONLY = process.env.SWEEP_ROUTES?.split(",");
const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

export interface Finding {
  rule: RuleId;
  severity: Severity;
  surface: string;
  route: string;
  state: string;
  role: Role;
  width: number;
  theme: string;
  url: string;
  text: string;
  selector: string;
  context?: string;
  note?: string;
  screenshot?: string;
}

interface StateRecord {
  surface: string;
  route: string;
  state: string;
  role: Role;
  width: number;
  theme: string;
  url: string;
  status: "checked" | "skipped" | "error";
  reason?: string;
  ms: number;
}

const slug = (value: string) => value.replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-|-$/g, "").toLowerCase();

function readContext() {
  return JSON.parse(readFileSync(paths.context, "utf8")) as {
    base: string;
    setupBase?: string;
    now?: number;
    pictureWorkflow?: string;
    inviteToken?: string;
  };
}

async function settle(page: Page) {
  await page.waitForLoadState("domcontentloaded");
  await page.waitForLoadState("networkidle", { timeout: 8_000 }).catch(() => {});
  await page
    .waitForFunction(
      () => {
        const shown = (el: Element) => {
          const style = getComputedStyle(el);
          return style.display !== "none" && style.visibility !== "hidden" && el.getBoundingClientRect().width > 0;
        };
        return ![...document.querySelectorAll('[aria-busy="true"], [data-slot="skeleton"]')].some(shown);
      },
      null,
      { timeout: 10_000 },
    )
    .catch(() => {});
  await page.evaluate(() => document.fonts.ready).catch(() => {});
  await page.waitForTimeout(300);
}

const stateRoles = (route: SweepRoute, state: SweepState) =>
  (state.roles ?? route.roles).filter((role) => route.roles.includes(role));

test("every app route has a sweep entry", () => {
  const covered = new Set(ROUTES.map((route) => route.appRoute));
  const discovered = discoverAppRoutes();
  const missing = discovered.filter((route) => !covered.has(route));
  const stale = [...covered].filter((route) => !discovered.includes(route));
  expect(missing, `Add these routes to e2e/sweep/routes.ts: ${missing.join(", ")}`).toEqual([]);
  expect(stale, `These sweep entries no longer exist in src/app: ${stale.join(", ")}`).toEqual([]);
});

for (const route of ROUTES) {
  if (ONLY && !ONLY.some((item) => route.appRoute === item || route.surface === item || route.path.includes(item))) continue;
  for (const role of ROLES) {
    if (!route.roles.includes(role)) continue;
    for (const width of WIDTHS) {
      for (const theme of THEMES) {
        const title = `${route.surface} ${route.path} · ${role} · ${width} · ${theme}`;
        test(title, async ({ browser }) => {
          const ctx = readContext();
          const findings: Finding[] = [];
          const states: StateRecord[] = [];
          const phone = width < 640;
          const sweepCtx: SweepContext = {
            role,
            phone,
            base: ctx.base,
            setupBase: ctx.setupBase,
            inviteToken: ctx.inviteToken,
            pictureWorkflow: ctx.pictureWorkflow,
          };
          const storageState =
            role === "owner" ? paths.ownerState : role === "member" ? paths.memberState : paths.anonymousState;
          const contexts: BrowserContext[] = [];
          const newContext = async (fresh: boolean) => {
            const context = await browser.newContext({
              storageState: fresh ? paths.anonymousState : storageState,
              viewport: { width, height: phone ? 844 : 900 },
              colorScheme: theme,
              reducedMotion: "reduce",
              locale: "en-US",
              timezoneId: "America/Chicago",
            });
            // The household fixture freezes its clock; the browser must agree or "x ago" goes stale.
            if (target.name === "fixtures" && ctx.now) await context.clock.setFixedTime(new Date(ctx.now * 1000));
            await context.addInitScript((value) => localStorage.setItem("theme", value), theme);
            contexts.push(context);
            return context;
          };
          const signedIn = await newContext(false);
          let firstRunContext: BrowserContext | undefined;

          for (const state of route.states) {
            if (!stateRoles(route, state).includes(role)) continue;
            const started = Date.now();
            const record: StateRecord = {
              surface: route.surface,
              route: route.path,
              state: state.id,
              role,
              width,
              theme,
              url: "",
              status: "checked",
              ms: 0,
            };
            states.push(record);
            const relative = state.url ? state.url(sweepCtx) : route.path;
            if (relative === null) {
              record.status = "skipped";
              record.reason = "no data for this state on this target";
              continue;
            }
            const base = state.firstRun ? ctx.setupBase : ctx.base;
            if (!base) {
              record.status = "skipped";
              record.reason = "no first-run gateway on this target";
              continue;
            }
            let context = signedIn;
            if (state.firstRun) context = firstRunContext ??= await newContext(true);
            const page = await context.newPage();
            const crashes: string[] = [];
            const apiUnknown: string[] = [];
            page.on("pageerror", (error) => crashes.push(`${error.name}: ${error.message}`.slice(0, 300)));
            page.on("response", async (response) => {
              const url = response.url();
              if (!/\/(me|admin|auth)\/api\/|\/api\//.test(url)) return;
              if (!(response.headers()["content-type"] ?? "").includes("json")) return;
              const body = await response.text().catch(() => "");
              if (/"unknown"/i.test(body)) apiUnknown.push(new URL(url).pathname);
            });
            try {
              await page.goto(`${base}${relative}`);
              await settle(page);
              if (state.act) {
                const applied = await state.act(page, sweepCtx);
                if (applied !== true) {
                  record.status = "skipped";
                  record.reason = applied;
                  if (!state.optional && role === "owner") record.status = "error";
                  continue;
                }
                await settle(page);
              }
              record.url = page.url().replace(base, "");
              // A member sent elsewhere (an owner page redirecting Home) sees the destination,
              // which is swept on its own; checking it again here would double count.
              const landed = new URL(page.url()).pathname;
              const asked = new URL(`${base}${relative}`).pathname;
              if (landed !== asked) {
                record.status = "skipped";
                record.reason = `redirected to ${landed}`;
                continue;
              }
              const shot = path.join(
                paths.shots,
                `${slug(route.surface)}__${slug(route.path)}__${state.id}__${role}_${width}_${theme}.jpg`,
              );
              const relShot = path.relative(paths.dir, shot);
              const before = findings.length;
              const add = (finding: Omit<Finding, "surface" | "route" | "state" | "role" | "width" | "theme" | "url">) =>
                findings.push({
                  surface: route.surface,
                  route: route.path,
                  state: state.id,
                  role,
                  width,
                  theme,
                  url: record.url,
                  ...finding,
                });

              type ProbeHit = { selector: string; ref: number; text: string; context?: string };
              const probe = (await page.evaluate(`(${probeSource.trim().replace(/;$/, "")})(${JSON.stringify({ phone })})`)) as {
                overflow: ProbeHit[];
                bottomNav: ProbeHit[];
                texts: Array<TextItem & { context?: string }>;
              };
              const flaggedRefs = new Set<number>();
              for (const hit of probe.overflow) {
                add({ rule: "overflow", severity: "fail", text: hit.text, selector: hit.selector, context: hit.context });
                flaggedRefs.add(hit.ref);
              }
              for (const hit of probe.bottomNav) {
                add({ rule: "bottom-nav", severity: "fail", text: hit.text, selector: hit.selector, context: hit.context });
                flaggedRefs.add(hit.ref);
              }
              const contextByRef = new Map(probe.texts.map((item) => [item.ref, item.context]));
              const textRole = route.dev ? "owner" : role === "anonymous" ? "member" : role;
              for (const hit of lintText(probe.texts, { role: textRole, research, jargon })) {
                if (route.dev && !["raw-error", "path"].includes(hit.rule)) continue;
                const note =
                  hit.rule === "unknown"
                    ? apiUnknown.length
                      ? `an API on this page also said "unknown" (${[...new Set(apiUnknown)].slice(0, 3).join(", ")})`
                      : 'no API response on this page said "unknown": the UI chose this word'
                    : hit.note;
                add({ rule: hit.rule, severity: hit.severity, text: hit.text, selector: hit.selector, context: contextByRef.get(hit.ref), note });
                flaggedRefs.add(hit.ref);
              }
              for (const crash of crashes) add({ rule: "page-crash", severity: "fail", text: crash, selector: "window" });

              if (process.env.SWEEP_AXE !== "0") {
                // While a modal layer is open, Radix hides the rest of the page with aria-hidden and
                // traps focus, so axe's aria-hidden-focus fires on every control behind it. Not a defect.
                const overlay = await page.locator("[role=dialog]:visible, [role=alertdialog]:visible, [role=listbox]:visible, [role=menu]:visible").count();
                let builder = new AxeBuilder({ page }).withTags(AXE_TAGS);
                if (overlay) builder = builder.disableRules(["aria-hidden-focus"]);
                const axe = await builder.analyze().catch((error) => {
                  add({ rule: "axe", severity: "warn", text: `axe could not run: ${String(error).slice(0, 200)}`, selector: "" });
                  return null;
                });
                for (const violation of axe?.violations ?? []) {
                  for (const node of violation.nodes.slice(0, 6)) {
                    const html = node.html.replace(/\s+/g, " ").slice(0, 140);
                    add({
                      rule: "axe",
                      severity: violation.impact === "minor" ? "warn" : "fail",
                      text: `${violation.id} (${violation.impact}): ${violation.help}. ${(node.failureSummary ?? "").replace(/\s+/g, " ").slice(0, 200)} — ${html}`,
                      selector: node.target.join(" "),
                    });
                  }
                  if (violation.nodes.length > 6)
                    add({ rule: "axe", severity: "warn", text: `${violation.id}: ${violation.nodes.length - 6} more nodes`, selector: "" });
                }
              }

              if (findings.length > before || process.env.SWEEP_SHOTS === "all") {
                await page.evaluate((refs) => {
                  for (const ref of refs) {
                    const el = document.querySelector(`[data-sweep-ref="${ref}"]`) as HTMLElement | null;
                    if (el) el.style.setProperty("outline", "3px solid #ff00ff", "important");
                  }
                }, [...flaggedRefs]);
                // Findings only, as small JPEGs: these runs are frequent and disks are finite.
                await page
                  .screenshot({ path: shot, fullPage: true, type: "jpeg", quality: 50, animations: "disabled", timeout: 20_000 })
                  .catch(() => {});
                for (const finding of findings.slice(before)) finding.screenshot = relShot;
              }
            } catch (error) {
              record.status = "error";
              record.reason = String(error).split("\n")[0].slice(0, 300);
            } finally {
              record.ms = Date.now() - started;
              await page.close();
            }
          }
          for (const context of contexts) await context.close();

          const file = path.join(paths.findings, `${slug(title)}.json`);
          writeFileSync(file, JSON.stringify({ title, findings, states }, null, 1));
          const failing = findings.filter((finding) => finding.severity === "fail");
          const errors = states.filter((state) => state.status === "error");
          const summary = failing
            .slice(0, 12)
            .map((finding) => `  [${finding.rule}] ${finding.state}: ${finding.text.slice(0, 140)}`)
            .join("\n");
          expect.soft(errors.map((state) => `${state.state}: ${state.reason}`), "states that could not be swept").toEqual([]);
          expect(failing.length, `${failing.length} sweep findings\n${summary}`).toBe(0);
        });
      }
    }
  }
}
