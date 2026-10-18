/** Write-only drafts carry stable identities, never stored secret bytes. */
export interface SecretRow {
  key: string;
  id?: string;
  name: string;
  originalName: string;
  value: string;
  prefix?: string;
  configured?: boolean;
  secret: boolean;
  edited?: boolean;
  removed?: boolean;
}
export interface SecretDraft {
  carrier: string;
  label: string;
  kind: "headers" | "env" | "argv" | "url" | "key";
  rows: SecretRow[];
  initial: string;
  ids: string[];
}
export type SecretCommand = Record<string, unknown> & {
  carrier: string;
  operation: string;
};
const containers = [
  "process",
  "process.profile_apply",
  "process.legacy_lifecycle.driver_config",
  "process.legacy_lifecycle.driver_config.profile_apply",
  "lifecycle.driver_config",
  "lifecycle.driver_config.profile_apply",
  "config.lifecycle.driver_config",
  "config.lifecycle.driver_config.profile_apply",
  "config.process",
  "config.process.profile_apply",
];
const urlFields = [
  "config.host",
  "config.url",
  "config.address",
  "config.base_url",
  "config.api_base",
  "config.default_base_url",
  "config.health_endpoint",
  "config.key_url",
  "config.docs_url",
  "config.gateway_lifecycle_base_url",
  "config.context_window_probe.endpoint",
  "process.health_endpoint",
  "process.legacy_lifecycle.driver_config.health_endpoint",
  "lifecycle.driver_config.health_endpoint",
  "config.lifecycle.driver_config.health_endpoint",
  "config.process.health_endpoint",
];
function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
function get(record: unknown, path: string): unknown {
  return path.split(".").reduce((node, key) => object(node)[key], record);
}
function row(value: unknown, name: string, index: number): SecretRow {
  const entry = object(value);
  const id = typeof entry.id === "string" ? entry.id : undefined;
  const publicValue =
    typeof value === "string"
      ? value
      : entry.public === true && typeof entry.value === "string"
        ? entry.value
        : "";
  return {
    key: id ?? `literal-${index}`,
    id,
    name,
    originalName: name,
    value: publicValue,
    secret: typeof value !== "string" && entry.public !== true,
    ...(typeof entry.prefix === "string" ? { prefix: entry.prefix } : {}),
    configured: entry.configured === true,
  };
}
export function secretDrafts(
  record: unknown,
  key?: { state: string; entry_id?: string },
): SecretDraft[] {
  const drafts: SecretDraft[] = [];
  const canonical = object(get(record, "process"));
  const canonicalProcess = ["command", "process"].includes(
    String(canonical.driver),
  );
  const activeContainers = canonicalProcess
    ? ["process", "process.profile_apply"]
    : containers;
  function add(
    carrier: string,
    label: string,
    kind: SecretDraft["kind"],
    rows: SecretRow[],
  ) {
    drafts.push({
      carrier,
      label,
      kind,
      rows,
      initial: JSON.stringify(rows),
      ids: rows.flatMap((r) => (r.id ? [r.id] : [])),
    });
  }
  if (key)
    add("config.api_key", "Inference API key", "key", [
      {
        key: "inference-key",
        id: key.entry_id,
        name: "",
        originalName: "",
        value: "",
        secret: true,
        configured: key.state === "configured",
      },
    ]);
  const headers = get(record, "config.headers");
  add(
    "config.headers",
    "Request headers",
    "headers",
    Array.isArray(headers)
      ? headers.map((h, i) => row(h, String(object(h).name ?? ""), i))
      : [],
  );
  for (const container of [...activeContainers, "config"]) {
    const env = get(record, `${container}.env`);
    if (env && typeof env === "object" && !Array.isArray(env))
      add(
        `${container}.env`,
        `${container} environment`,
        "env",
        Object.entries(env).map(([name, value], i) => row(value, name, i)),
      );
  }
  for (const container of activeContainers) {
    const node = object(get(record, container));
    const commandKeys = container.endsWith("profile_apply")
      ? ["reload"]
      : ["start", "stop", "status"];
    for (const key of commandKeys) {
      const argv = node[key];
      if (Array.isArray(argv))
        add(
          `${container}.${key}`,
          `${container} ${key} command`,
          "argv",
          argv.map((v, i) => row(v, "", i)),
        );
      else if (
        canonicalProcess &&
        container === "process" &&
        argv === undefined
      )
        add(`${container}.${key}`, `${container} ${key} command`, "argv", []);
    }
    if (
      container === "process" &&
      ["process", "command"].includes(String(node.driver)) &&
      !node.env
    )
      add("process.env", "Process environment", "env", []);
  }
  for (const carrier of urlFields) {
    const value = get(record, carrier);
    const entries = object(get(record, carrier + "_entries"));
    if ((typeof value === "string" && value) || Object.keys(entries).length)
      add(carrier, `${carrier} credentials`, "url", [
        {
          key: carrier,
          name: "",
          originalName: "",
          value: "",
          secret: true,
          configured: Object.keys(entries).length > 0,
        },
      ]);
  }
  return drafts;
}
export function secretCommands(drafts: SecretDraft[]): SecretCommand[] {
  const commands: SecretCommand[] = [];
  for (const draft of drafts) {
    if (JSON.stringify(draft.rows) === draft.initial) continue;
    const carrier = draft.carrier;
    const rows = draft.rows.filter((r) => !r.removed);
    if (draft.kind === "argv") {
      const retained = rows
        .filter((r) => r.id && !r.value && !r.edited && r.secret)
        .map((r) => r.id!);
      commands.push({
        carrier,
        operation: "set",
        items: rows.map((r) =>
          retained.includes(r.id ?? "")
            ? { retain: r.id }
            : r.secret
              ? { secret: r.value, ...(r.prefix ? { prefix: r.prefix } : {}) }
              : { literal: r.value },
        ),
        clear: draft.ids.filter((id) => !retained.includes(id)),
      });
    } else if (draft.kind === "url") {
      const r = draft.rows[0];
      if (r.removed) commands.push({ carrier, operation: "clear_credentials" });
      else if (r.edited || r.value)
        commands.push({ carrier, operation: "set_credentials", url: r.value });
    } else {
      for (const r of draft.rows) {
        if (r.removed) {
          if (r.id)
            commands.push({ carrier, operation: "clear", entry_id: r.id });
          continue;
        }
        if (r.id && r.name !== r.originalName)
          commands.push({
            carrier,
            operation: "rename",
            entry_id: r.id,
            name: r.name,
          });
        if (r.edited || (r.value && (!r.id || r.secret)))
          commands.push(
            r.id
              ? {
                  carrier,
                  operation: "replace",
                  entry_id: r.id,
                  value: r.value,
                }
              : {
                  carrier,
                  operation: "add",
                  ...(draft.kind === "key" ? {} : { name: r.name }),
                  value: r.value,
                },
          );
      }
    }
  }
  return commands;
}
export function secretProblem(drafts: SecretDraft[]): string | null {
  for (const draft of drafts) {
    if (JSON.stringify(draft.rows) === draft.initial) continue;
    const rows = draft.rows.filter((r) => !r.removed);
    if (
      draft.kind === "argv" &&
      draft.carrier.endsWith(".start") &&
      !rows.length
    )
      return `${draft.label}: the program is required.`;
    if (
      draft.kind === "argv" &&
      rows.length &&
      (rows[0].secret || !rows[0].value.trim())
    )
      return `${draft.label}: the program must be a non-empty public executable.`;
    for (const r of rows) {
      if (
        r.value &&
        ["[redacted]", "<redacted>", "<configured>"].includes(r.value.trim())
      )
        return "That text is reserved. Enter a new value.";
      if ((r.edited || (!r.id && draft.kind !== "url")) && !r.value)
        return "Enter a non-empty value, or remove the entry explicitly.";
      if (["env", "headers"].includes(draft.kind) && !r.name.trim())
        return "Every header or variable needs a name.";
      if (
        draft.kind === "headers" &&
        r.id &&
        r.name !== r.originalName &&
        r.edited
      )
        return "Save the header rename before replacing its value.";
      if (draft.kind === "env" && r.id && r.name !== r.originalName)
        return "To rename a variable, remove it and add the new variable.";
    }
  }
  return null;
}

/** The endpoint input is public. Credential-bearing URL parts use their write-only carrier. */
export function endpointProblem(address: string): string | null {
  try {
    const url = new URL(address);
    if (url.username || url.password || url.search || url.hash)
      return "Enter only the public endpoint here. Use the URL credentials field for credentials, query values or fragments.";
  } catch {
    /* The ordinary connection check explains malformed endpoints. */
  }
  return null;
}
