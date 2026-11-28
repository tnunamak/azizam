import { describe, expect, it } from "vitest";
import {
  absentPaths,
  check,
  derive,
  merge,
} from "../../e2e/fixtures/intercept-shape";

const real = {
  revision: "r1",
  records: [{ id: "a", source: "api", cost: 1.5, tags: ["x"] }],
  next_cursor: null,
  counts: { total: 1 },
};

const required = [
  "/revision",
  "/records",
  "/records/[]/id",
  "/records/[]/source",
  "/counts",
];
const problems = (
  stub: unknown,
  server: unknown = real,
  options: object = { required },
) => check(derive(server, options), stub).problems;

describe("fulfilJson structure check", () => {
  it("accepts a stub with the real structure, with other values and other list lengths", () => {
    expect(problems({ ...real, revision: "r9", records: [] })).toEqual([]);
    expect(
      problems({
        ...real,
        records: [real.records[0], { ...real.records[0], id: "b" }],
      }),
    ).toEqual([]);
  });

  it("fails a stub that leaves out a key the server always sends", () => {
    const { revision: _gone, ...stub } = real;
    expect(problems(stub)).toEqual(["/revision: required key missing"]);
    const { source: _source, ...record } = real.records[0];
    expect(problems({ ...real, records: [record] })).toEqual([
      "/records/0/source: required key missing",
    ]);
  });

  it("fails a stub with a key the server never sends", () => {
    expect(problems({ ...real, extra: true })).toEqual([
      "/extra: key the real response never has",
    ]);
    expect(
      problems({ ...real, records: [{ ...real.records[0], note: "x" }] }),
    ).toEqual(["/records/0/note: key the real response never has"]);
  });

  it("fails a stub that still uses the name the server renamed", () => {
    const serverAfterRename = { ...real, revision: undefined, rev: "r1" };
    const stub = { ...real };
    const server = JSON.parse(JSON.stringify(serverAfterRename));
    expect(problems(stub, server)).toEqual([
      "/revision: key the real response never has",
    ]);
    // and the registry's own list of required keys now names a key the server lacks
    expect(absentPaths(derive(server), required)).toEqual(["/revision"]);
  });

  it("fails a stub with another type", () => {
    expect(problems({ ...real, counts: { total: "1" } })).toEqual([
      "/counts/total: expected number, got string",
    ]);
    expect(problems({ ...real, records: {} })).toEqual([
      "/records: expected array, got object",
    ]);
  });

  it("does not require a key the endpoint does not list, even if the server always sends it", () => {
    const { cost: _cost, ...record } = real.records[0];
    expect(
      problems({ ...real, records: [record], next_cursor: undefined }),
    ).toEqual([]);
  });

  it("folds two real responses: a key from either is known; the union of their types is allowed", () => {
    const merged = merge(derive({ a: 1, b: "x" }), derive({ a: "s", c: true }));
    expect(check(merged, { a: "t", b: "y", c: false }).problems).toEqual([]);
    expect(check(merged, { d: 1 }).problems).toEqual([
      "/d: key the real response never has",
    ]);
  });

  it("lets open maps carry any key", () => {
    expect(
      problems({ ...real, counts: { other: 3 } }, real, {
        required,
        open: ["/counts"],
      }),
    ).toEqual([]);
  });

  it("passes a null anywhere and counts the places the real response could not constrain", () => {
    const report = check(derive({ a: null, list: [] }), {
      a: "anything",
      list: [1, 2],
    });
    expect(report.problems).toEqual([]);
    expect(report.unchecked).toBeGreaterThan(0);
    expect(problems({ ...real, next_cursor: null, revision: null })).toEqual(
      [],
    );
  });

  it("lets a key the seed lacks pass when the gateway source names it, and fails any other name", () => {
    const known = new Set(["note"]);
    const withNote = { ...real, records: [{ ...real.records[0], note: "x" }] };
    expect(check(derive(real, { required }), withNote, known).problems).toEqual(
      [],
    );
    expect(
      check(derive(real, { required }), { ...real, extra: 1 }, known).problems,
    ).toEqual(["/extra: key the real response never has"]);
  });

  it("does not check operator-owned content listed as free", () => {
    const shape = derive({ doc: { a: 1 } }, { free: ["/doc"] });
    expect(check(shape, { doc: { anything: ["goes"] } }).problems).toEqual([]);
    expect(check(shape, { other: 1 }).problems).toEqual([
      "/other: key the real response never has",
    ]);
  });
});
