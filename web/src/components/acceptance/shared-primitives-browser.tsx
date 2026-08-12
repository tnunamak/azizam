"use client";

import { useState } from "react";

import { DataTable, type DataTableColumn } from "@/components/ui";

type FixtureRow = {
  id: string;
  name: string;
  detail: string;
};

const LONG_VALUE =
  "https://gateway.example.invalid/v1/models/this-is-an-intentionally-long-route-value-that-must-wrap-without-clipping";

const rows: FixtureRow[] = [
  { id: "actionable", name: "Actionable card", detail: LONG_VALUE },
  { id: "plain", name: "Non-actionable card", detail: LONG_VALUE },
];

/** Browser-only acceptance surface. It composes the real shared React
 * primitives beneath the real admin shell; it is not a DOM or CSS mirror. */
export function SharedPrimitivesBrowserFixture() {
  const [event, setEvent] = useState("none");
  const columns: DataTableColumn<FixtureRow>[] = [
    {
      id: "name",
      header: "Name",
      cell: (row) => (
        <button type="button" onClick={() => setEvent(`${row.id}-head`)}>
          Nested action: {row.name}
        </button>
      ),
    },
    {
      id: "detail",
      header: "Detail",
      cell: (row) => (
        <a
          href={`#${row.id}`}
          onClick={(event) => {
            event.preventDefault();
            setEvent(`${row.id}-link`);
          }}
        >
          {row.detail}
        </a>
      ),
    },
  ];

  return (
    <section data-testid="shared-primitives-stage" className="min-w-0">
      <p data-testid="event-status" className="break-all">
        {event}
      </p>
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        label="Shared primitives fixture"
        mobileRowActionLabel={(row) => `Open ${row.name}`}
        onRowClick={(row) => setEvent(`${row.id}-primary`)}
        rowMenu={(row) => (
          <button type="button" onClick={() => setEvent(`${row.id}-menu`)}>
            More actions for {row.name}
          </button>
        )}
      />
    </section>
  );
}
