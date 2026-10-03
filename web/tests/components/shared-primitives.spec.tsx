import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  CurrentAdvisory,
  DataTable,
  SectionActionHeader,
  type DataTableColumn,
  TrendLineChart,
} from "@/components/ui";

type Row = { id: string; name: string; detail: string };

const rows: Row[] = [
  { id: "fixture-row", name: "Fixture row", detail: "Live detail" },
];

function columns(onHeadAction: () => void, onDetailAction: () => void) {
  return [
    {
      id: "name",
      header: "Name",
      cell: (row: Row) => (
        <button type="button" onClick={onHeadAction}>
          Copy {row.name}
        </button>
      ),
    },
    {
      id: "detail",
      header: "Detail",
      cell: (row: Row) => (
        <a
          href={`#${row.id}`}
          onClick={(event) => {
            event.preventDefault();
            onDetailAction();
          }}
        >
          {row.detail}
        </a>
      ),
    },
  ] satisfies DataTableColumn<Row>[];
}

function CardFixture({ actionable }: { actionable: boolean }) {
  const onRowAction = vi.fn();
  const onHeadAction = vi.fn();
  const onDetailAction = vi.fn();
  const onMenuAction = vi.fn();

  return {
    onRowAction,
    onHeadAction,
    onDetailAction,
    onMenuAction,
    view: (
      <DataTable
        columns={columns(onHeadAction, onDetailAction)}
        rows={rows}
        rowKey={(row) => row.id}
        onRowClick={actionable ? onRowAction : undefined}
        mobileRowActionLabel={(row) => `Open ${row.name}`}
        rowMenu={
          actionable
            ? () => (
                <button type="button" onClick={onMenuAction}>
                  More actions
                </button>
              )
            : undefined
        }
      />
    ),
  };
}

afterEach(cleanup);

describe("shared operational primitives", () => {
  it("keeps actionable card controls above the primary action", async () => {
    const user = userEvent.setup();
    const fixture = CardFixture({ actionable: true });
    render(fixture.view);
    const card = document.querySelector<HTMLElement>(".vf-mobile-data-card");
    if (!card) throw new Error("Expected actionable mobile data card");
    const mobile = within(card);

    await user.click(mobile.getByRole("button", { name: "Copy Fixture row" }));
    await user.click(mobile.getByRole("link", { name: "Live detail" }));
    await user.click(mobile.getByRole("button", { name: "More actions" }));
    expect(fixture.onHeadAction).toHaveBeenCalledOnce();
    expect(fixture.onDetailAction).toHaveBeenCalledOnce();
    expect(fixture.onMenuAction).toHaveBeenCalledOnce();
    expect(fixture.onRowAction).not.toHaveBeenCalled();

    await user.click(mobile.getByRole("button", { name: "Open Fixture row" }));
    expect(fixture.onRowAction).toHaveBeenCalledOnce();
  });

  it("leaves a non-actionable card and its native controls independently operable", async () => {
    const user = userEvent.setup();
    const fixture = CardFixture({ actionable: false });
    render(fixture.view);
    const card = document.querySelector<HTMLElement>(".vf-mobile-data-card");
    if (!card) throw new Error("Expected non-actionable mobile data card");
    const mobile = within(card);

    expect(
      mobile.queryByRole("button", { name: "Open Fixture row" }),
    ).toBeNull();
    await user.click(mobile.getByRole("button", { name: "Copy Fixture row" }));
    await user.click(mobile.getByRole("link", { name: "Live detail" }));
    expect(fixture.onHeadAction).toHaveBeenCalledOnce();
    expect(fixture.onDetailAction).toHaveBeenCalledOnce();
    expect(fixture.onRowAction).not.toHaveBeenCalled();
  });

  it("renders chart meaning once and never invents a trend from two samples", () => {
    const { rerender } = render(
      <TrendLineChart
        series={[
          { x: "08:00", y: 12 },
          { x: "09:00", y: 18 },
          { x: "10:00", y: 15 },
        ]}
        accessibility={{ mode: "data-table", label: "Request latency samples" }}
      />,
    );
    expect(
      screen.getByRole("table", { name: "Request latency samples" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("group", { name: "Request latency samples" }),
    ).toBeNull();

    rerender(
      <TrendLineChart
        series={[
          { x: "08:00", y: 12 },
          { x: "09:00", y: 18 },
        ]}
        emptyLabel="No real trend data"
      />,
    );
    expect(screen.getByText("No real trend data")).toBeVisible();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("omits an empty CurrentAdvisory instead of presenting an invented all-clear state", () => {
    const { container } = render(
      <CurrentAdvisory tone="warn" title="Needs attention" items={[]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("uses an h2 by default without duplicate headings", () => {
    const { container } = render(
      <SectionActionHeader title="Service health" count={3} />,
    );

    expect(
      screen.getByRole("heading", { name: "Service health 3", level: 2 }),
    ).toBeInTheDocument();
    expect(container.querySelectorAll("h1, h2, h3")).toHaveLength(1);
  });

  it("renders a page-level h1 instead of nesting or duplicating the title", () => {
    const { container } = render(<SectionActionHeader as="h1" title="Audit" />);

    expect(
      screen.getByRole("heading", { name: "Audit", level: 1 }),
    ).toBeInTheDocument();
    expect(container.querySelectorAll("h1, h2, h3")).toHaveLength(1);
    expect(container.querySelector("h1 h1, h1 h2, h1 h3")).toBeNull();
  });
});
