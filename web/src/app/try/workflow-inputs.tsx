"use client";

import { AlertCircle } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

export type WorkflowInput = {
  name: string;
  type: string;
  request_field?: string;
  applies?: string;
  required?: boolean;
  default?: unknown;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
  accepted?: boolean;
  ignored?: boolean;
};

export type WorkflowInputsProps = {
  inputs: WorkflowInput[];
  json: string;
  onChange: (json: string) => void;
  mapping?: WorkflowInputMapping;
};

type JsonObject = Record<string, unknown>;
export type WorkflowInputMapping = Record<
  string,
  {
    in?: "body" | "form" | "extension" | "file" | "file+extension" | "none";
    field?: string;
    combine?: string[];
  }
>;

const FILE_TYPES = new Set([
  "asset",
  "file",
  "image",
  "audio",
  "video",
  "binary",
  "upload",
]);

export function parseWorkflowInputs(
  json: string,
  inputs: WorkflowInput[] = [],
  mapping: WorkflowInputMapping = {},
): JsonObject {
  let parsed: unknown;
  try {
    parsed = JSON.parse(json || "{}");
  } catch {
    throw new Error("Workflow inputs must be a JSON object.");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Workflow inputs must be a JSON object.");
  }
  const values = parsed as JsonObject;
  for (const input of inputs) {
    if (isFileInput(input) || input.ignored || input.accepted === false) {
      continue;
    }
    const value = readWorkflowInputValue(values, inputs, input, mapping);
    if (value === undefined || value === null || value === "") {
      if (input.required) {
        throw new Error(`${input.name} is required.`);
      }
      continue;
    }
    validateWorkflowInputValue(input, value);
  }
  return values;
}

export function workflowDefaults(
  inputs: WorkflowInput[],
  example: JsonObject = {},
  mapping: WorkflowInputMapping = {},
): JsonObject {
  const values: JsonObject = { ...example };
  for (const input of inputs) {
    if (isFileInput(input) || input.default === undefined) continue;
    const target = inputTarget(input, mapping);
    if (readWorkflowInputValue(values, inputs, input, mapping) === undefined) {
      setWorkflowInputValue(values, inputs, input, input.default, mapping);
    } else if (target.combine?.length) {
      setWorkflowInputValue(values, inputs, input, input.default, mapping);
    }
  }
  return values;
}

export function WorkflowInputs({
  inputs,
  json,
  onChange,
  mapping = {},
}: WorkflowInputsProps) {
  if (!inputs.length) return null;
  const values = safeObject(json);
  const parseError = values.ok ? null : values.error;
  const current = values.ok ? values.value : {};

  function update(input: WorkflowInput, value: unknown) {
    const next = cloneJsonObject(current);
    setWorkflowInputValue(next, inputs, input, value, mapping);
    onChange(JSON.stringify(next, null, 2));
  }

  return (
    <div className="rounded-lg border">
      <div className="border-b p-3">
        <h3 className="text-sm font-medium">Typed inputs</h3>
        <p className="text-muted-foreground mt-1 text-xs">
          Guided controls update the same JSON sent with the request.
        </p>
        {parseError && (
          <p className="text-destructive mt-2 flex items-center gap-2 text-xs">
            <AlertCircle aria-hidden="true" className="size-3.5" />
            {parseError}
          </p>
        )}
      </div>
      <div className="grid gap-3 p-3 md:grid-cols-2">
        {inputs.map((input) => (
          <WorkflowInputControl
            key={input.name}
            input={input}
            value={readWorkflowInputValue(current, inputs, input, mapping)}
            disabled={Boolean(parseError)}
            onChange={(value) => update(input, value)}
          />
        ))}
      </div>
    </div>
  );
}

function WorkflowInputControl({
  input,
  value,
  disabled: parentDisabled = false,
  onChange,
}: {
  input: WorkflowInput;
  value: unknown;
  disabled?: boolean;
  onChange: (value: unknown) => void;
}) {
  const id = `workflow-input-${input.name}`;
  const disabled =
    parentDisabled ||
    input.ignored ||
    input.accepted === false ||
    isFileInput(input) ||
    isUnsupportedValue(value);
  const explanation = inputExplanation(input);

  return (
    <label className="border-border/70 bg-card/30 flex min-w-0 flex-col gap-2 rounded-md border p-3 text-sm">
      <span className="flex items-center justify-between gap-3">
        <span className="min-w-0">
          <span className="font-medium">{input.name}</span>
          {input.required && (
            <span className="text-destructive ml-1">Required</span>
          )}
        </span>
        <span className="text-muted-foreground shrink-0 text-xs">
          {input.type}
        </span>
      </span>
      {controlFor(input, id, value, disabled, onChange)}
      <span className="text-muted-foreground text-xs">{explanation}</span>
    </label>
  );
}

function controlFor(
  input: WorkflowInput,
  id: string,
  value: unknown,
  disabled: boolean,
  onChange: (value: unknown) => void,
) {
  const type = normalizedType(input);
  if (isFileInput(input)) {
    return (
      <Input
        id={id}
        disabled
        value="Managed by the workflow file picker"
        aria-label={input.name}
        readOnly
      />
    );
  }
  if (isUnsupportedValue(value)) {
    return (
      <Textarea
        id={id}
        aria-label={input.name}
        disabled
        value={JSON.stringify(value, null, 2)}
        readOnly
      />
    );
  }
  if (input.enum?.length) {
    return (
      <Select
        value={selectValue(value)}
        disabled={disabled}
        onValueChange={(next) => onChange(enumValue(input, next))}
      >
        <SelectTrigger id={id} className="w-full" aria-label={input.name}>
          <SelectValue placeholder="Choose a value" />
        </SelectTrigger>
        <SelectContent>
          {input.enum.map((option) => (
            <SelectItem key={String(option)} value={String(option)}>
              {String(option)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }
  if (type === "boolean") {
    return (
      <span className="flex min-h-11 items-center gap-3">
        <Checkbox
          id={id}
          aria-label={input.name}
          checked={value === true}
          disabled={disabled}
          onCheckedChange={(checked) => onChange(checked === true)}
        />
        <span className="text-muted-foreground text-xs">
          {value === true ? "Enabled" : "Disabled"}
        </span>
      </span>
    );
  }
  if (type === "number" || type === "integer") {
    return (
      <Input
        id={id}
        aria-label={input.name}
        type="number"
        inputMode={type === "integer" ? "numeric" : "decimal"}
        step={type === "integer" ? 1 : "any"}
        min={input.minimum}
        max={input.maximum}
        disabled={disabled}
        value={numberDisplay(value)}
        onChange={(event) => {
          const next = event.currentTarget.value;
          onChange(next === "" ? undefined : Number(next));
        }}
      />
    );
  }
  if (String(value ?? "").length > 80) {
    return (
      <Textarea
        id={id}
        aria-label={input.name}
        disabled={disabled}
        value={String(value ?? "")}
        onChange={(event) => onChange(event.currentTarget.value)}
      />
    );
  }
  return (
    <Input
      id={id}
      aria-label={input.name}
      disabled={disabled}
      value={String(value ?? "")}
      onChange={(event) => onChange(event.currentTarget.value)}
    />
  );
}

function validateWorkflowInputValue(input: WorkflowInput, value: unknown) {
  const type = normalizedType(input);
  if (input.enum && !input.enum.includes(value)) {
    throw new Error(`${input.name} must be one of: ${input.enum.join(", ")}.`);
  }
  if (type === "string" && typeof value !== "string") {
    throw new Error(`${input.name} must be a string.`);
  }
  if (type === "boolean" && typeof value !== "boolean") {
    throw new Error(`${input.name} must be a boolean.`);
  }
  if (type === "number" || type === "integer") {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw new Error(`${input.name} must be a number.`);
    }
    if (type === "integer" && !Number.isInteger(value)) {
      throw new Error(`${input.name} must be an integer.`);
    }
    if (input.minimum !== undefined && value < input.minimum) {
      throw new Error(`${input.name} must be >= ${input.minimum}.`);
    }
    if (input.maximum !== undefined && value > input.maximum) {
      throw new Error(`${input.name} must be <= ${input.maximum}.`);
    }
  }
}

export function setWorkflowInputValue(
  object: JsonObject,
  inputs: WorkflowInput[],
  input: WorkflowInput,
  value: unknown,
  mapping: WorkflowInputMapping = {},
) {
  const target = inputTarget(input, mapping);
  if (!target.field || target.in === "none" || target.in === "file") return;
  const root = requestRoot(object);
  if (target.in === "extension" || target.in === "file+extension") {
    setExtensionValue(root, target.field, value);
    return;
  }
  if (target.combine?.length || hasSharedRequestField(inputs, input, mapping)) {
    setCombinedValue(root, inputs, input, value, target, mapping);
    return;
  }
  if (value === undefined) {
    delete root[target.field];
    return;
  }
  root[target.field] = value;
}

export function readWorkflowInputValue(
  object: JsonObject,
  inputs: WorkflowInput[],
  input: WorkflowInput,
  mapping: WorkflowInputMapping = {},
) {
  const target = inputTarget(input, mapping);
  if (!target.field) return undefined;
  const root = requestRoot(object);
  if (target.in === "extension" || target.in === "file+extension") {
    return readExtension(root)[target.field];
  }
  if (target.combine?.length || hasSharedRequestField(inputs, input, mapping)) {
    return readCombinedPart(root[target.field], input.name);
  }
  return root[target.field];
}

function safeObject(json: string):
  | { ok: true; value: JsonObject }
  | { ok: false; error: string } {
  try {
    return { ok: true, value: parseWorkflowInputs(json) };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : "Invalid workflow JSON.",
    };
  }
}

function inputTarget(input: WorkflowInput, mapping: WorkflowInputMapping) {
  const mapped = mapping[input.name];
  const applies = String(input.applies ?? "").toLowerCase();
  const location =
    mapped?.in ??
    (input.ignored || input.accepted === false
      ? "none"
      : applies.includes("extension")
        ? "extension"
        : isFileInput(input)
          ? "file"
          : "body");
  return {
    in: location,
    field: mapped?.field ?? input.request_field ?? input.name,
    combine: mapped?.combine,
  };
}

function normalizedType(input: WorkflowInput) {
  return input.type.toLowerCase();
}

function isFileInput(input: WorkflowInput) {
  return FILE_TYPES.has(normalizedType(input));
}

function inputExplanation(input: WorkflowInput) {
  if (isFileInput(input)) {
    return "File value is handled by the existing workflow file picker.";
  }
  if (input.ignored) {
    return "Known input, but this route ignores it for the selected workflow.";
  }
  if (input.accepted === false) {
    return "Known input, but not available for this route.";
  }
  const parts = [`Writes ${input.request_field || input.name}`];
  if (input.default !== undefined) parts.push(`default ${valueText(input.default)}`);
  if (input.minimum !== undefined || input.maximum !== undefined) {
    parts.push(
      `range ${input.minimum ?? "-∞"} to ${input.maximum ?? "∞"}`,
    );
  }
  return `${parts.join(" · ")}.`;
}

function cloneJsonObject(value: JsonObject): JsonObject {
  return JSON.parse(JSON.stringify(value)) as JsonObject;
}

function requestRoot(object: JsonObject): JsonObject {
  if (
    object.request &&
    typeof object.request === "object" &&
    !Array.isArray(object.request)
  ) {
    return object.request as JsonObject;
  }
  return object;
}

function setExtensionValue(root: JsonObject, field: string, value: unknown) {
  const extensions = extensionContainer(root);
  if (value === undefined) {
    delete extensions[field];
  } else {
    extensions[field] = value;
  }
  writeExtensionContainer(root, extensions);
}

function readExtension(root: JsonObject): JsonObject {
  const value = root.extensions;
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return namespaceObject(parsed);
    } catch {
      return {};
    }
  }
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return namespaceObject(value as JsonObject);
  }
  return {};
}

function extensionEnvelope(root: JsonObject): JsonObject {
  const value = root.extensions;
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed)
        ? (parsed as JsonObject)
        : {};
    } catch {
      return {};
    }
  }
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as JsonObject;
  }
  return {};
}

function extensionContainer(root: JsonObject) {
  return namespaceObject(extensionEnvelope(root));
}

function writeExtensionContainer(root: JsonObject, workflowExtension: JsonObject) {
  const envelope = {
    ...extensionEnvelope(root),
    "com.vividfish.workflow": workflowExtension,
  };
  root.extensions =
    typeof root.extensions === "string" ? JSON.stringify(envelope) : envelope;
}

function namespaceObject(value: JsonObject) {
  const namespace = value["com.vividfish.workflow"];
  if (namespace && typeof namespace === "object" && !Array.isArray(namespace)) {
    return namespace as JsonObject;
  }
  return {};
}

function setCombinedValue(
  root: JsonObject,
  inputs: WorkflowInput[],
  input: WorkflowInput,
  value: unknown,
  target: { field: string; combine?: string[] },
  mapping: WorkflowInputMapping,
) {
  const parts = target.combine ?? sharedParts(inputs, input, mapping);
  if (target.field === "size" && parts.length >= 2) {
    const current = parseSize(root[target.field]);
    const widthName = parts[0];
    const heightName = parts[1];
    const width =
      input.name === widthName
        ? value
        : current.width ?? defaultFor(inputs, widthName);
    const height =
      input.name === heightName
        ? value
        : current.height ?? defaultFor(inputs, heightName);
    if (width === undefined || height === undefined) {
      delete root[target.field];
    } else {
      root[target.field] = `${width}x${height}`;
    }
  }
}

function hasSharedRequestField(
  inputs: WorkflowInput[],
  input: WorkflowInput,
  mapping: WorkflowInputMapping,
) {
  return sharedParts(inputs, input, mapping).length > 1;
}

function sharedParts(
  inputs: WorkflowInput[],
  input: WorkflowInput,
  mapping: WorkflowInputMapping,
) {
  const target = inputTarget(input, mapping);
  return inputs
    .filter((item) => inputTarget(item, mapping).field === target.field)
    .map((item) => item.name);
}

function parseSize(value: unknown) {
  if (typeof value !== "string") return {};
  const [width, height] = value.split("x");
  return { width, height };
}

function defaultFor(inputs: WorkflowInput[], name: string) {
  return inputs.find((input) => input.name === name)?.default;
}

function readCombinedPart(value: unknown, name: string) {
  if (typeof value !== "string") return undefined;
  const { width, height } = parseSize(value);
  if (name === "width") return Number(width);
  if (name === "height") return Number(height);
  return value;
}

function isUnsupportedValue(value: unknown) {
  return value !== null && typeof value === "object";
}

function enumValue(input: WorkflowInput, value: string) {
  return input.enum?.find((option) => String(option) === value) ?? value;
}

function selectValue(value: unknown) {
  return value === undefined || value === null ? "" : String(value);
}

function numberDisplay(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "";
}

function valueText(value: unknown) {
  if (value === undefined) return "none";
  if (typeof value === "string") return value || "empty string";
  return JSON.stringify(value);
}
