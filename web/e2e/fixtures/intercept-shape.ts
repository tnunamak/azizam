// Structure of a JSON body: what a real gateway response looks like, and a
// check that a stub a Playwright test fulfils has the same structure. Pure
// functions, so the checker has unit tests of its own (tests/lib/intercept-shape.spec.ts).
//
// A shape is derived from one or more real responses: its keys and the type of
// each value. A key is required only where the endpoint's registry entry lists
// its path (a stub is a slice of the real response; the listed keys are the ones
// the product reads), but a key the real response never has always fails. Three holes
// are deliberate and counted by `check`: a real null says nothing about the type
// (any value passes), an empty real array says nothing about its items (any item
// passes), and a stub null passes for any type (stubs use null for "absent").
export type Shape =
  | { t: "string" | "number" | "boolean" | "null" | "any" }
  | { t: "array"; item: Shape | null }
  | { t: "object"; keys: Record<string, { shape: Shape; required: boolean }> }
  | { t: "map"; value: Shape | null }
  | { t: "union"; of: Shape[] };

export interface ShapeOptions {
  // Paths of objects whose keys are data (ids, names), not field names.
  open?: string[];
  // Paths of keys a stub must carry; "[]" stands for any array index.
  required?: string[];
}

const typeOf = (value: unknown): "string" | "number" | "boolean" | "null" | "array" | "object" =>
  value === null ? "null" : Array.isArray(value) ? "array" : (typeof value as "string" | "number" | "boolean" | "object");

export function derive(value: unknown, options: ShapeOptions = {}, at = ""): Shape {
  const type = typeOf(value);
  if (type === "array") {
    const items = (value as unknown[]).map((item) => derive(item, options, `${at}/[]`));
    return { t: "array", item: items.length ? items.reduce(merge) : null };
  }
  if (type === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (options.open?.includes(at)) {
      const values = entries.map(([, item]) => derive(item, options, `${at}/*`));
      return { t: "map", value: values.length ? values.reduce(merge) : null };
    }
    const keys: Record<string, { shape: Shape; required: boolean }> = {};
    for (const [key, item] of entries) {
      keys[key] = {
        shape: derive(item, options, `${at}/${key}`),
        required: Boolean(options.required?.includes(`${at}/${key}`)),
      };
    }
    return { t: "object", keys };
  }
  return { t: type as "string" | "number" | "boolean" | "null" };
}

const isAny = (shape: Shape) => shape.t === "any";

/** Fold two real observations of the same place into one shape. */
export function merge(a: Shape, b: Shape): Shape {
  if (isAny(a) || isAny(b)) return { t: "any" };
  if (a.t === "null") return b;
  if (b.t === "null") return a;
  if (a.t === "union" || b.t === "union") {
    const members = [...(a.t === "union" ? a.of : [a]), ...(b.t === "union" ? b.of : [b])];
    const folded: Shape[] = [];
    for (const member of members) {
      const same = folded.findIndex((other) => other.t === member.t);
      if (same === -1) folded.push(member);
      else folded[same] = merge(folded[same], member);
    }
    return folded.length === 1 ? folded[0] : { t: "union", of: folded };
  }
  if (a.t !== b.t) return { t: "union", of: [a, b] };
  if (a.t === "array" && b.t === "array")
    return { t: "array", item: a.item && b.item ? merge(a.item, b.item) : (a.item ?? b.item) };
  if (a.t === "map" && b.t === "map")
    return { t: "map", value: a.value && b.value ? merge(a.value, b.value) : (a.value ?? b.value) };
  if (a.t === "object" && b.t === "object") {
    const keys: Record<string, { shape: Shape; required: boolean }> = {};
    for (const key of new Set([...Object.keys(a.keys), ...Object.keys(b.keys)])) {
      const left = a.keys[key];
      const right = b.keys[key];
      keys[key] = {
        shape: left && right ? merge(left.shape, right.shape) : (left ?? right).shape,
        required: Boolean(left?.required || right?.required),
      };
    }
    return { t: "object", keys };
  }
  return a;
}

export interface Report {
  problems: string[];
  // Places the real response could not constrain (null or empty array there).
  unchecked: number;
}

/** Problems with `stub` against `shape`; an empty list means the stub has the real structure. */
export function check(shape: Shape, stub: unknown, at = ""): Report {
  const report: Report = { problems: [], unchecked: 0 };
  walk(shape, stub, at || "/", report);
  return report;
}

function walk(shape: Shape, value: unknown, at: string, report: Report) {
  if (shape.t === "any") {
    report.unchecked += 1;
    return;
  }
  const type = typeOf(value);
  if (type === "null") return;
  if (shape.t === "union") {
    const trial = shape.of.map((member) => {
      const inner: Report = { problems: [], unchecked: 0 };
      walk(member, value, at, inner);
      return inner;
    });
    const fit = trial.find((inner) => inner.problems.length === 0);
    if (fit) report.unchecked += fit.unchecked;
    else report.problems.push(`${at}: matches none of ${shape.of.map((member) => member.t).join(" | ")} (got ${type})`);
    return;
  }
  if (shape.t === "null") {
    report.unchecked += 1;
    return;
  }
  if (shape.t === "array") {
    if (type !== "array") return void report.problems.push(`${at}: expected array, got ${type}`);
    if (!shape.item) report.unchecked += 1;
    else (value as unknown[]).forEach((item, index) => walk(shape.item as Shape, item, `${at.replace(/\/$/, "")}/${index}`, report));
    return;
  }
  if (shape.t === "map") {
    if (type !== "object") return void report.problems.push(`${at}: expected object, got ${type}`);
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (!shape.value) report.unchecked += 1;
      else walk(shape.value, item, `${at.replace(/\/$/, "")}/${key}`, report);
    }
    return;
  }
  if (shape.t === "object") {
    if (type !== "object") return void report.problems.push(`${at}: expected object, got ${type}`);
    const object = value as Record<string, unknown>;
    const prefix = at.replace(/\/$/, "");
    for (const key of Object.keys(object))
      if (!(key in shape.keys)) report.problems.push(`${prefix}/${key}: key the real response never has`);
    for (const [key, { required }] of Object.entries(shape.keys))
      if (required && !(key in object)) report.problems.push(`${prefix}/${key}: required key missing`);
    for (const [key, item] of Object.entries(object))
      if (key in shape.keys) walk(shape.keys[key].shape, item, `${prefix}/${key}`, report);
    return;
  }
  if (type !== shape.t) report.problems.push(`${at}: expected ${shape.t}, got ${type}`);
}

/** Listed paths the real response does not have: the registry names a key the server dropped or renamed. */
export function absentPaths(shape: Shape, paths: string[]): string[] {
  const reach = (at: Shape | null, parts: string[]): boolean => {
    if (parts.length === 0 || at === null || at.t === "any") return true;
    const [head, ...rest] = parts;
    if (at.t === "union") return at.of.some((member) => reach(member, parts));
    if (at.t === "array") return head === "[]" ? reach(at.item, rest) : false;
    if (at.t === "map") return reach(at.value, rest);
    if (at.t === "object") return head in at.keys && reach(at.keys[head].shape, rest);
    return false;
  };
  return paths.filter((path) => !reach(shape, path.split("/").slice(1)));
}
