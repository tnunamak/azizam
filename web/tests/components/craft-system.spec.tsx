// Craft system PR 1 (docs/design/craft-system/PLAN.md): the shared parts carry
// the numbers in DESIGN "System numbers v2". jsdom has no layout, so these
// pin the classes and tokens that produce the sizes; craft_numbers.py
// measures the rendered pixels.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import path from "node:path";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { chartColors, ChartLegend, CHART_OTHER } from "@/components/ui/charts";
import { Input } from "@/components/ui/input";
import { SkeletonRows } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { EmptyState } from "@/components/product/empty-state";
import { KeyValueList } from "@/components/product/key-value";
import { ListCard, ListRow } from "@/components/product/list-card";
import { PageHeader } from "@/components/product/page-header";
import { SettingRow, SettingsSection } from "@/components/product/setting-row";
import { Stat, StatStrip } from "@/components/product/stat-strip";
import { StateBadge } from "@/components/product/status-ui";

afterEach(cleanup);

const classes = (element: Element) => element.className.split(/\s+/);

describe("tokens", () => {
  const css = readFileSync(
    path.join(__dirname, "../../src/styles/tokens.css"),
    "utf8",
  );
  it.each([
    ["--text-title", "1.5rem"],
    ["--text-verdict", "1.25rem"],
    ["--text-heading", "1rem"],
    ["--text-body", "0.9375rem"],
    ["--text-meta", "0.875rem"],
    ["--text-label", "0.8125rem"],
    ["--text-badge", "0.75rem"],
    ["--row-1", "2.5rem"],
    ["--row-1-dense", "2.25rem"],
    ["--row-2", "3.75rem"],
    ["--row-setting", "4rem"],
    ["--row-phone-1", "3rem"],
    ["--row-phone-2", "4rem"],
    ["--control-sm", "2rem"],
    ["--control-md", "2.5rem"],
    ["--control-touch", "2.75rem"],
    ["--radius-card", "8px"],
    ["--radius-badge", "4px"],
    ["--content-read", "720px"],
    ["--space-section", "2rem"],
    ["--space-heading", "0.75rem"],
    ["--space-title", "1.5rem"],
    ["--space-stack", "1rem"],
  ])("%s is %s", (token, value) => {
    expect(css).toMatch(new RegExp(`${token}:\\s*${value.replace(".", "\\.")};`));
  });

  it("steps the page padding down to 24 and 20 and the top to 24", () => {
    expect(css).toMatch(/--page-pad:\s*2rem;/);
    expect(css).toMatch(
      /@media \(max-width: 1023\.98px\)\s*\{\s*:root\s*\{\s*--page-pad:\s*1\.5rem;/,
    );
    expect(css).toMatch(
      /@media \(max-width: 639\.98px\)\s*\{\s*:root\s*\{\s*--page-pad:\s*1\.25rem;\s*--page-top:\s*1\.5rem;/,
    );
  });
});

describe("controls", () => {
  it("makes 32 the default button and 40 the md button, 44 on a phone", () => {
    render(
      <>
        <Button>Refresh</Button>
        <Button size="md">Save</Button>
        <Button size="icon" aria-label="More" />
      </>,
    );
    const refresh = classes(screen.getByRole("button", { name: "Refresh" }));
    expect(refresh).toEqual(expect.arrayContaining(["h-8", "text-meta"]));
    expect(refresh).toEqual(
      expect.arrayContaining(["max-sm:min-h-11", "max-sm:min-w-11"]),
    );
    expect(refresh).not.toContain("h-10");
    expect(classes(screen.getByRole("button", { name: "Save" }))).toEqual(
      expect.arrayContaining(["h-10", "text-body", "max-sm:min-h-11"]),
    );
    expect(classes(screen.getByRole("button", { name: "More" }))).toContain(
      "size-8",
    );
  });

  it("gives a form's submit the md height unless it names a size", () => {
    render(
      <form>
        <Button type="submit">Save</Button>
        <Button type="submit" size="sm">
          Apply
        </Button>
      </form>,
    );
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toHaveAttribute("data-size", "md");
    expect(save).toHaveClass("h-10");
    expect(screen.getByRole("button", { name: "Apply" })).toHaveClass("h-8");
  });

  it("sizes inputs at 40 in forms and 32 in toolbars, 44 on a phone", () => {
    render(
      <>
        <Input aria-label="Name" />
        <Input aria-label="Search" size="sm" />
      </>,
    );
    const name = screen.getByRole("textbox", { name: "Name" });
    expect(classes(name)).toEqual(
      expect.arrayContaining(["sm:h-10", "max-sm:h-11", "text-body"]),
    );
    expect(name).toHaveAttribute("data-size", "md");
    expect(classes(screen.getByRole("textbox", { name: "Search" }))).toEqual(
      expect.arrayContaining(["sm:h-8", "max-sm:h-11", "text-meta"]),
    );
  });

  it("lets a caller's own height or minimum replace the field defaults", () => {
    render(
      <>
        <Input aria-label="Tall" className="h-12" />
        <Textarea aria-label="Notes" />
        <Textarea aria-label="JSON" className="min-h-36 font-mono text-xs" />
      </>,
    );
    expect(classes(screen.getByRole("textbox", { name: "Tall" }))).not.toContain(
      "sm:h-10",
    );
    expect(classes(screen.getByRole("textbox", { name: "Notes" }))).toEqual(
      expect.arrayContaining(["min-h-20", "max-sm:min-h-24", "text-body"]),
    );
    const json = classes(screen.getByRole("textbox", { name: "JSON" }));
    expect(json).not.toContain("min-h-20");
    expect(json).not.toContain("text-body");
  });
});

describe("cards, badges and tables", () => {
  it("draws the card with a hairline, radius 8, padding 24 and no shadow", () => {
    render(
      <Card data-testid="card">
        <CardTitle>Receipts</CardTitle>
        <CardContent>Body</CardContent>
      </Card>,
    );
    const card = classes(screen.getByTestId("card"));
    expect(card).toEqual(
      expect.arrayContaining(["rounded-panel", "border", "p-6", "max-sm:p-4", "gap-4"]),
    );
    expect(card.some((name) => name.startsWith("shadow"))).toBe(false);
    expect(card).not.toContain("rounded-xl");
    expect(classes(screen.getByText("Receipts"))).toContain("text-heading");
  });

  it("draws a state badge 20 tall at radius 4 with its tint, icon and word", () => {
    render(
      <>
        <StateBadge state="degraded" />
        <StateBadge state="down" word="Failed" />
      </>,
    );
    const degraded = screen.getByText("Degraded");
    expect(classes(degraded)).toEqual(
      expect.arrayContaining([
        "h-5",
        "rounded-badge",
        "text-badge",
        "px-1.5",
        "bg-warning-tint",
        "text-warning",
      ]),
    );
    expect(degraded).toHaveAttribute("data-state-key", "degraded");
    expect(degraded.querySelector("svg")).toHaveClass("size-3.5");
    expect(screen.getByText("Failed")).toHaveClass("bg-danger-tint");
  });

  it("keeps plain badges on the badge radius instead of a pill", () => {
    render(<Badge variant="secondary">Owner</Badge>);
    expect(classes(screen.getByText("Owner"))).not.toContain("rounded-full");
  });

  it("gives tables a 36 label header, 40 rows, a dense mode and numeric cells", () => {
    render(
      <Table dense>
        <TableHeader>
          <TableRow>
            <TableHead>Model</TableHead>
            <TableHead numeric>Requests</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell>Default</TableCell>
            <TableCell numeric>12</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    );
    expect(screen.getByRole("table")).toHaveAttribute("data-dense");
    expect(classes(screen.getByText("Model"))).toEqual(
      expect.arrayContaining(["h-9", "text-label", "text-subtle-foreground"]),
    );
    expect(classes(screen.getByText("Requests"))).toEqual(
      expect.arrayContaining(["text-right", "figures"]),
    );
    expect(classes(screen.getByText("Default"))).toEqual(
      expect.arrayContaining(["h-10", "group-data-dense/table:h-9"]),
    );
    expect(classes(screen.getByText("12"))).toContain("text-right");
  });
});

describe("overlays", () => {
  it("keep a dialog's overlay under its content whatever the DOM order", () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogTitle>Go to</DialogTitle>
        </DialogContent>
      </Dialog>,
    );
    const content = screen.getByRole("dialog", { name: "Go to" });
    const overlay = document.querySelector("[data-slot=dialog-overlay]")!;
    expect(overlay).toHaveClass("z-40");
    expect(content).toHaveClass("z-50");
  });
});

describe("a table that scrolls sideways", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function renderTable() {
    render(
      <Table>
        <TableBody>
          <TableRow>
            <TableCell>Wide</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    );
    return screen.getByRole("table").parentElement!;
  }

  it("can be focused to scroll it from the keyboard, and only then", () => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(600);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(350);
    expect(renderTable()).toHaveAttribute("tabindex", "0");
    cleanup();
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(350);
    expect(renderTable()).not.toHaveAttribute("tabindex");
  });
});

describe("page parts", () => {
  it("sets the page title, count and subtitle in their roles", () => {
    render(<PageHeader title="Library" count={24} subtitle="Your creations" />);
    const title = screen.getByRole("heading", { level: 1 });
    expect(classes(title)).toContain("text-title");
    expect(classes(within(title).getByText("24"))).toContain("text-heading");
    expect(classes(screen.getByText("Your creations"))).toEqual(
      expect.arrayContaining(["text-meta", "mt-1"]),
    );
    expect(title.closest("header")).toHaveClass("mb-(--space-title)");
  });

  it("draws an empty state left-aligned at 160, dashed only outside a card", () => {
    render(
      <>
        <EmptyState title="No keys yet" description="Create one to start." />
        <EmptyState title="Nobody yet" inCard />
      </>,
    );
    const alone = screen.getByText("No keys yet").closest("[data-slot=empty-state]")!;
    expect(classes(alone)).toEqual(
      expect.arrayContaining(["min-h-40", "items-start", "border-dashed"]),
    );
    expect(classes(screen.getByText("No keys yet"))).toEqual(
      expect.arrayContaining(["text-body", "font-medium"]),
    );
    expect(classes(screen.getByText("Create one to start."))).toContain("text-meta");
    const inCard = screen.getByText("Nobody yet").closest("[data-slot=empty-state]")!;
    expect(classes(inCard)).not.toContain("border-dashed");
  });

  it("draws skeleton rows at the row heights", () => {
    const { container } = render(
      <>
        <SkeletonRows count={3} />
        <SkeletonRows count={2} lines={2} />
      </>,
    );
    const rows = container.querySelectorAll("[data-slot=skeleton-row]");
    expect(rows).toHaveLength(5);
    expect(rows[0]).toHaveClass("h-(--row-1)", "max-sm:h-(--row-phone-1)");
    expect(rows[3]).toHaveClass("h-(--row-2)", "max-sm:h-(--row-phone-2)");
  });
});

describe("ListCard and ListRow", () => {
  it("draws a header band and rows at 40 and 60, 48 and 64 on a phone", () => {
    render(
      <ListCard header="Person" label="People">
        <ListRow title="Sam" />
        <ListRow lines={2} title="Robin" meta="robin@example.test" />
      </ListCard>,
    );
    const list = screen.getByRole("list", { name: "People" });
    const header = list.parentElement!.querySelector("[data-slot=list-card-header]")!;
    expect(header).toHaveTextContent("Person");
    expect(classes(header)).toEqual(expect.arrayContaining(["h-9", "text-label"]));
    const [one, two] = within(list).getAllByRole("listitem");
    expect(classes(one)).toEqual(
      expect.arrayContaining(["min-h-(--row-1)", "max-sm:min-h-(--row-phone-1)", "px-4"]),
    );
    expect(classes(two)).toEqual(
      expect.arrayContaining(["min-h-(--row-2)", "max-sm:min-h-(--row-phone-2)"]),
    );
    expect(classes(screen.getByText("Sam"))).toEqual(
      expect.arrayContaining(["text-body", "font-medium", "truncate"]),
    );
    expect(classes(screen.getByText("robin@example.test"))).toEqual(
      expect.arrayContaining(["text-meta", "truncate"]),
    );
  });

  it("joins meta and, on a phone, the facts into one second line", () => {
    render(
      <ListCard label="People">
        <ListRow
          lines={2}
          title="Robin"
          meta={["robin@example.test", null]}
          facts={["Builder", "just now"]}
        />
      </ListCard>,
    );
    const meta = screen
      .getByRole("listitem")
      .querySelector("[data-slot=list-row-meta]")!;
    expect(meta.textContent).toBe("robin@example.test · Builder · just now");
    expect(meta.querySelector(".sm\\:hidden")).toHaveTextContent("Builder · just now");
    const facts = screen
      .getByRole("listitem")
      .querySelector("[data-slot=list-row-facts]")!;
    expect(classes(facts)).toEqual(expect.arrayContaining(["max-sm:hidden", "text-meta"]));
  });

  it("opens the row from its title and keeps the trailing action on top", () => {
    render(
      <ListCard label="Apps">
        <ListRow
          title="Home Assistant"
          href="/apps/?app=ha"
          trailing={<Button aria-label="App actions" size="icon" />}
        />
      </ListCard>,
    );
    const link = screen.getByRole("link", { name: "Home Assistant" });
    // next/link normalises the trailing slash in tests (no basePath here).
    expect(link.getAttribute("href")).toMatch(/^\/apps\/?\?app=ha$/);
    expect(link).toHaveClass("after:absolute", "after:inset-0");
    const trailing = screen
      .getByRole("button", { name: "App actions" })
      .closest("[data-slot=list-row-trailing]")!;
    expect(classes(trailing)).toEqual(expect.arrayContaining(["relative", "z-10"]));
  });
});

describe("SettingsSection and SettingRow", () => {
  it("names a card of 64 rows with a heading 12 above it", () => {
    render(
      <SettingsSection title="Sign-in">
        <SettingRow
          label="Passwords"
          description="People sign in with an email and password."
          control={<Button>Edit</Button>}
        />
      </SettingsSection>,
    );
    const section = screen.getByRole("region", { name: "Sign-in" });
    expect(classes(within(section).getByRole("heading", { name: "Sign-in" }))).toContain(
      "text-heading",
    );
    const row = section.querySelector<HTMLElement>("[data-slot=setting-row]")!;
    expect(classes(row)).toEqual(
      expect.arrayContaining(["min-h-(--row-setting)", "px-4", "py-2.5"]),
    );
    expect(classes(screen.getByText("Passwords"))).toEqual(
      expect.arrayContaining(["text-body", "font-medium"]),
    );
    expect(
      classes(screen.getByText("People sign in with an email and password.")),
    ).toEqual(expect.arrayContaining(["text-meta", "truncate"]));
    expect(within(row).getByRole("button", { name: "Edit" })).toHaveClass("h-8");
  });

  it("labels a form control when given its id", () => {
    render(
      <SettingsSection title="Gateway">
        <SettingRow
          label="Instance name"
          htmlFor="instance-name"
          control={<Input id="instance-name" size="sm" />}
        />
      </SettingsSection>,
    );
    expect(screen.getByLabelText("Instance name")).toHaveAttribute(
      "id",
      "instance-name",
    );
  });
});

describe("StatStrip and Stat", () => {
  it("draws up to four figures divided by hairlines, with no icon tiles", () => {
    render(
      <StatStrip label="This month">
        <Stat label="Requests" value="1,068" />
        <Stat label="Spend" value="USD 4.12" note="Receipts for 98%" />
      </StatStrip>,
    );
    const strip = screen.getByLabelText("This month");
    expect(strip.tagName).toBe("DL");
    expect(classes(strip)).toEqual(
      expect.arrayContaining(["gap-px", "bg-border", "rounded-panel", "border"]),
    );
    expect(classes(screen.getByText("1,068"))).toEqual(
      expect.arrayContaining(["text-figure", "figures"]),
    );
    expect(classes(screen.getByText("Requests"))).toContain("text-meta");
    expect(classes(screen.getByText("Receipts for 98%"))).toContain("text-meta");
    expect(strip.querySelector("svg")).toBeNull();
  });
});

describe("KeyValueList", () => {
  it("puts keys in a 160 column in meta ink and values in body text", () => {
    render(
      <KeyValueList
        items={[
          ["What's wrong", "The engine stopped answering"],
          ["Affects", "Video"],
        ]}
      />,
    );
    const list = screen.getByText("Affects").closest("dl")!;
    expect(classes(list)).toContain("sm:grid-cols-[10rem_minmax(0,1fr)]");
    expect(classes(screen.getByText("What's wrong"))).toEqual(
      expect.arrayContaining(["text-meta", "text-muted-foreground", "sm:min-h-8"]),
    );
    expect(classes(screen.getByText("Video"))).toContain("text-body");
  });
});

describe("charts", () => {
  it("gives three series their own hue and folds the rest into Other", () => {
    const { colors, shown, folded } = chartColors([
      "Chat",
      "Images",
      "Video",
      "Voice",
      "Music",
    ]);
    expect(shown).toEqual(["Chat", "Images", "Video"]);
    expect(folded).toEqual(["Voice", "Music"]);
    expect(colors.Chat).toBe("var(--chart-1)");
    expect(colors.Video).toBe("var(--chart-3)");
    expect(colors.Voice).toBe(CHART_OTHER);
    expect(new Set(Object.values(colors)).size).toBe(4);
  });

  it("draws legend entries as 8 px dots with their words", () => {
    render(<ChartLegend items={[{ label: "Requests", color: "var(--chart-1)" }]} />);
    const item = screen.getByText("Requests");
    expect(item.querySelector("[data-slot=chart-legend-dot]")).toHaveClass("size-2");
  });
});
