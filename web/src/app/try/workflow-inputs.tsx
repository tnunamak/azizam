"use client";

import { useEffect, useRef } from "react";
import { AlertCircle } from "lucide-react";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
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
import {
  inputHint,
  inputLabel,
  inputPlaceholder,
  isPlausibleMediaReference,
  missingMessage,
  type OutputKind,
} from "./workflow-input-meta";
import { MediaInput } from "./workflow-media-input";
import { isMediaInput, mediaKindOf } from "@/lib/workflow-media-input";

export type WorkflowInput = {
  name: string;
  type: string;
  description?: string;
  request_field?: string;
  applies?: string;
  required?: boolean;
  default?: unknown;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
  accepted?: boolean;
  ignored?: boolean;
  /** The gateway marks media inputs; their `type` is still "string". */
  asset?: boolean;
  /** The media kind the catalog declares for an asset input, such as "image/*". */
  media_type?: string;
  accepted_media_types?: string[];
  /** The manifest's short name for the input, as a noun ("a face photo"). */
  label?: string;
  /** The manifest's one plain sentence about the input. */
  help?: string;
};

export type WorkflowInputsProps = {
  inputs: WorkflowInput[];
  json: string;
  onChange: (json: string) => void;
  mapping?: WorkflowInputMapping;
  /** What the workflow makes; picks the words ("Describe the video"). */
  outputKind?: OutputKind;
  /** Plain messages by input name, shown under the field. */
  problems?: Record<string, string>;
  /** Files chosen for multipart inputs, keyed by input name (fields may repeat). */
  files?: Record<string, File | null>;
  onFileChange?: (inputName: string, file: File | null) => void;
  /** Show the technical mapping (field names, types) in a disclosure: owners only. */
  technical?: boolean;
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
  validateExtensionEnvelope(requestRoot(values));
  for (const input of inputs) {
    if (
      isFileInput(input, mapping) ||
      input.ignored ||
      input.accepted === false ||
      isUnsupportedCombinedInput(input, inputs, mapping)
    ) {
      continue;
    }
    const value = readWorkflowInputValue(values, inputs, input, mapping);
    if (
      value === undefined ||
      value === null ||
      (typeof value === "string" && !value.trim())
    ) {
      if (input.required) {
        throw new Error(`${named(input)} is required.`);
      }
      continue;
    }
    validateWorkflowInputValue(input, value, mapping);
  }
  return values;
}

export function workflowDefaults(
  inputs: WorkflowInput[],
  example: JsonObject = {},
  mapping: WorkflowInputMapping = {},
): JsonObject {
  const values = cloneJsonObject(example);
  // The catalog's example body shows the request's shape. Its values are
  // made up ("value", seed 0, a one-second clip), so a form that starts from
  // them sends text the person never wrote. Only what the manifest declares,
  // a default or a fixed choice, is a starting value.
  for (const input of inputs) {
    if (isFileInput(input, mapping)) {
      const target = inputTarget(input, mapping);
      const root = requestRoot(values);
      delete root[target.field];
      if (target.in === "file+extension") {
        setExtensionValue(root, target.field, undefined);
      }
      continue;
    }
    if (input.ignored || input.accepted === false) continue;
    // A single enum value is fixed protocol; every editable example is cleared.
    if (input.enum?.length === 1 && input.default === undefined) continue;
    setWorkflowInputValue(values, inputs, input, undefined, mapping);
  }
  for (const input of inputs) {
    if (isFileInput(input, mapping) || input.default === undefined) continue;
    if (readWorkflowInputValue(values, inputs, input, mapping) === undefined) {
      setWorkflowInputValue(values, inputs, input, input.default, mapping);
    }
  }
  return values;
}

/**
 * What is missing or unusable before Run sends anything, by input name, in
 * words a member can act on. Unreadable JSON is reported by the form itself.
 */
export function workflowInputProblems(
  json: string,
  inputs: WorkflowInput[],
  mapping: WorkflowInputMapping = {},
  output: OutputKind = "other",
  files: Record<string, File | null> = {},
): Record<string, string> {
  let values: JsonObject;
  try {
    values = parseWorkflowInputs(json);
  } catch {
    return {};
  }
  const problems: Record<string, string> = {};
  for (const input of inputs) {
    if (input.ignored || input.accepted === false) continue;
    if (isFileInput(input, mapping)) {
      if (input.required && !files[input.name]) {
        problems[input.name] = missingMessage(input, output);
      }
      continue;
    }
    if (isUnsupportedCombinedInput(input, inputs, mapping)) continue;
    const value = readWorkflowInputValue(values, inputs, input, mapping);
    const empty =
      value === undefined ||
      value === null ||
      (typeof value === "string" && !value.trim());
    if (empty) {
      if (input.required) problems[input.name] = missingMessage(input, output);
    } else if (
      isMediaBodyInput(input, mapping) &&
      !(typeof value === "string" && isPlausibleMediaReference(value))
    ) {
      problems[input.name] = missingMessage(input, output);
    }
  }
  return problems;
}

export function WorkflowInputs({
  inputs,
  json,
  onChange,
  mapping = {},
  outputKind = "other",
  problems = {},
  files = {},
  onFileChange,
  technical = false,
}: WorkflowInputsProps) {
  const currentJson = useRef(json);
  useEffect(() => {
    currentJson.current = json;
  }, [json]);
  if (!inputs.length) return null;
  const values = safeObject(json);
  const parseError = values.ok ? null : values.error;
  const current = values.ok ? values.value : {};
  // A member sees what they can set. Inputs the gateway fills in or ignores
  // are listed with the rest under Technical details.
  const settable = inputs.filter(
    (input) => !input.ignored && input.accepted !== false,
  );
  // What is needed comes first. The optional inputs wait under "More options",
  // so Run sits right after what the workflow needs. With nothing required,
  // the options are the form and stay open.
  const required = settable.filter((input) => input.required);
  const optional = settable.filter((input) => !input.required);
  const folded = required.length > 0 ? optional : [];
  const visible = required.length > 0 ? required : optional;
  const foldedProblem = folded.some((input) => problems[input.name]);

  function update(input: WorkflowInput, value: unknown) {
    const latest = safeObject(currentJson.current);
    if (!latest.ok) return;
    const next = cloneJsonObject(latest.value);
    setWorkflowInputValue(next, inputs, input, value, mapping);
    currentJson.current = JSON.stringify(next, null, 2);
    onChange(currentJson.current);
  }

  function control(input: WorkflowInput) {
    return (
      <WorkflowInputControl
        key={input.name}
        input={input}
        value={readWorkflowInputValue(current, inputs, input, mapping)}
        disabled={
          Boolean(parseError) ||
          isUnsupportedCombinedInput(input, inputs, mapping)
        }
        fileInput={isFileInput(input, mapping)}
        file={files[input.name] ?? null}
        onFileChange={
          onFileChange &&
          ((file) => onFileChange(input.name, file))
        }
        mediaInput={isMediaBodyInput(input, mapping)}
        outputKind={outputKind}
        problem={problems[input.name]}
        unsupportedCombined={isUnsupportedCombinedInput(
          input,
          inputs,
          mapping,
        )}
        onChange={(value) => update(input, value)}
      />
    );
  }

  return (
    <div className="rounded-lg border">
      <div className="border-b p-3">
        <h3 className="text-sm font-medium">Inputs</h3>
        {parseError && (
          <p className="text-destructive mt-2 flex items-center gap-2 text-xs">
            <AlertCircle aria-hidden="true" className="size-3.5" />
            {technical ? parseError : UNLOADED_INPUTS}
          </p>
        )}
      </div>
      <div className="grid gap-3 p-3 md:grid-cols-2">
        {visible.map(control)}
      </div>
      {folded.length > 0 && (
        <details className="border-t px-3" open={foldedProblem || undefined}>
          <summary className="min-h-11 cursor-pointer py-3 text-sm font-medium">
            More options
          </summary>
          <div className="grid gap-3 pb-3 md:grid-cols-2">
            {folded.map(control)}
          </div>
        </details>
      )}
      {technical && (
      <details className="border-t px-3">
        <summary className="text-muted-foreground min-h-11 cursor-pointer py-3 text-xs">
          Input details
        </summary>
        <dl className="space-y-3 pb-3 text-xs">
          {inputs.map((input) => (
            <div key={input.name} data-technical-input={input.name}>
              <dt className="font-medium">
                {input.name}
                <span className="text-muted-foreground ml-2 font-normal">
                  {input.type}
                  {input.required ? " · required" : ""}
                </span>
              </dt>
              <dd className="text-muted-foreground">
                {inputExplanation(input, mapping)}
                {input.applies ? ` ${input.applies}.` : ""}
                {input.description ? ` ${input.description}` : ""}
              </dd>
            </div>
          ))}
        </dl>
      </details>
      )}
    </div>
  );
}

function WorkflowInputControl({
  input,
  value,
  disabled: parentDisabled = false,
  fileInput = false,
  file = null,
  onFileChange,
  mediaInput = false,
  outputKind,
  problem,
  unsupportedCombined = false,
  onChange,
}: {
  input: WorkflowInput;
  value: unknown;
  disabled?: boolean;
  fileInput?: boolean;
  file?: File | null;
  onFileChange?: (file: File | null) => void;
  mediaInput?: boolean;
  outputKind: OutputKind;
  problem?: string;
  unsupportedCombined?: boolean;
  onChange: (value: unknown) => void;
}) {
  const id = `workflow-input-${input.name}`;
  const text = inputLabel(input, outputKind);
  const label = input.required ? `${text} Required` : text;
  const hint = unsupportedCombined
    ? "This setting cannot be changed here yet."
    : inputHint(input, outputKind);
  const errorId = problem ? `${id}-error` : undefined;
  const disabled = parentDisabled || isUnsupportedValue(value);
  // An asset the catalog does not type is a picture, as for body inputs
  // below; a generic file ("binary") gets a plain file chooser.
  const kind =
    mediaKindOf(input) ??
    (input.asset === true || normalizedType(input) === "asset" ? "image" : null);
  const header = (
    <span className="flex items-center justify-between gap-3">
      <span className="min-w-0">
        <span className="font-medium" id={`${id}-label`}>
          {text}
        </span>
        {input.required && (
          <span className="text-muted-foreground ml-1">Required</span>
        )}
      </span>
    </span>
  );
  const hintLine = hint ? (
    <span className="text-muted-foreground text-xs">{hint}</span>
  ) : null;
  const problemLine = problem ? (
    <span
      id={errorId}
      role="alert"
      className="text-destructive flex items-center gap-1.5 text-xs"
    >
      <AlertCircle aria-hidden="true" className="size-3.5 shrink-0" />
      {problem}
    </span>
  ) : null;
  const wrapper = `flex min-w-0 flex-col gap-2 text-sm ${
    (input.type === "string" && !input.enum?.length) || mediaInput || fileInput
      ? "md:col-span-2"
      : ""
  }`;

  // One control per picture: a file part gets the same picker as a picture
  // sent inside the request, never a text box plus a second upload control.
  if (fileInput && onFileChange && !parentDisabled) {
    return (
      <div role="group" aria-labelledby={`${id}-label`} className={wrapper}>
        {header}
        {hintLine}
        {kind ? (
          <MediaInput
            id={id}
            label={text}
            kind={kind}
            file={file}
            invalid={Boolean(problem)}
            describedBy={errorId}
            onFileChange={onFileChange}
          />
        ) : (
          <Input
            id={id}
            type="file"
            aria-labelledby={`${id}-label`}
            aria-required={input.required || undefined}
            aria-invalid={Boolean(problem) || undefined}
            aria-describedby={errorId}
            onChange={(event) =>
              onFileChange(event.currentTarget.files?.[0] ?? null)
            }
          />
        )}
        {problemLine}
      </div>
    );
  }

  if (mediaInput && !disabled) {
    return (
      <div role="group" aria-labelledby={`${id}-label`} className={wrapper}>
        {header}
        {hintLine}
        <MediaInput
          id={id}
          label={text}
          // A media input the catalog does not type is a picture: the
          // only kind Try's workflows take without saying so.
          kind={mediaKindOf(input) ?? "image"}
          value={typeof value === "string" ? value : undefined}
          invalid={Boolean(problem)}
          describedBy={errorId}
          onChange={onChange}
        />
        {problemLine}
      </div>
    );
  }

  return (
    <label className={wrapper}>
      {header}
      {controlFor(input, id, label, value, disabled || fileInput, fileInput, onChange, {
        placeholder: inputPlaceholder(input, outputKind),
        invalid: Boolean(problem),
        describedBy: errorId,
      })}
      {hintLine}
      {problemLine}
    </label>
  );
}

// Short one-line values keep the compact field and submit on Enter. Long or
// multi-line values (a pasted paragraph) get a growing field so no line is hidden.
function isShortSingleLine(value: string) {
  return value.length <= 80 && !value.includes("\n");
}

const UNLOADED_INPUTS =
  "This workflow's inputs could not be loaded. Ask the owner to check the workflow.";

// An example in a field must not read as something already typed.
const PLACEHOLDER_CLASS = "placeholder:italic placeholder:opacity-80";

type ControlExtras = {
  placeholder?: string;
  invalid?: boolean;
  describedBy?: string;
};

function controlFor(
  input: WorkflowInput,
  id: string,
  label: string,
  value: unknown,
  disabled: boolean,
  fileInput: boolean,
  onChange: (value: unknown) => void,
  extras: ControlExtras = {},
) {
  const type = normalizedType(input);
  const aria = {
    "aria-label": label,
    "aria-required": input.required || undefined,
    "aria-invalid": extras.invalid || undefined,
    "aria-describedby": extras.describedBy,
  };
  if (fileInput) {
    // Only when no picker is wired up (the page always wires one).
    return <Input id={id} disabled value="" {...aria} readOnly />;
  }
  if (isUnsupportedValue(value)) {
    return (
      <div id={id} className="space-y-1.5">
        <p className="text-muted-foreground text-xs">
          {UNLOADED_INPUTS}
        </p>
        <details className="text-xs">
          <summary className="min-h-11 cursor-pointer py-3">
            Technical details
          </summary>
          <pre className="bg-muted/40 overflow-x-auto rounded-md p-2">
            {JSON.stringify(value, null, 2)}
          </pre>
        </details>
      </div>
    );
  }
  if (input.enum?.length) {
    return (
      <Select
        value={selectValue(value)}
        disabled={disabled}
        onValueChange={(next) => {
          // Radix can emit an empty value when its hidden form input resets.
          if (next === UNSET) onChange(undefined);
          else if (next) onChange(enumValue(input, next));
        }}
      >
        <SelectTrigger id={id} className="w-full" {...aria}>
          <SelectValue placeholder="Choose a value" />
        </SelectTrigger>
        <SelectContent>
          {!input.required && <SelectItem value={UNSET}>Not set</SelectItem>}
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
          {...aria}
          checked={value === true}
          disabled={disabled}
          onCheckedChange={(checked) => onChange(checked === true)}
        />
        <span className="text-muted-foreground text-xs">
          {value === true ? "Enabled" : value === false ? "Disabled" : "Not set"}
        </span>
        {!input.required && typeof value === "boolean" && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={disabled}
            aria-label={`Clear ${label}`}
            onClick={() => onChange(undefined)}
          >
            Clear
          </Button>
        )}
      </span>
    );
  }
  if (type === "number" || type === "integer") {
    return (
      <Input
        id={id}
        {...aria}
        type="number"
        inputMode={type === "integer" ? "numeric" : "decimal"}
        step={type === "integer" ? 1 : "any"}
        min={input.minimum}
        max={input.maximum}
        placeholder={extras.placeholder}
        className={PLACEHOLDER_CLASS}
        disabled={disabled}
        value={numberDisplay(value)}
        onChange={(event) => {
          const next = event.currentTarget.value;
          onChange(next === "" ? undefined : Number(next));
        }}
      />
    );
  }
  return (
    <Textarea
      id={id}
      rows={1}
      className={cn(
        PLACEHOLDER_CLASS,
        isShortSingleLine(String(value ?? "")) &&
          "h-11 min-h-11 resize-none field-sizing-fixed py-1 shadow-none",
      )}
      {...aria}
      placeholder={extras.placeholder}
      disabled={disabled}
      value={String(value ?? "")}
      onChange={(event) => onChange(event.currentTarget.value)}
      onKeyDown={(event) => {
        if (
          event.key === "Enter" &&
          !event.nativeEvent.isComposing &&
          isShortSingleLine(event.currentTarget.value)
        ) {
          event.preventDefault();
          event.currentTarget.form
            ?.querySelector<HTMLButtonElement>('button[type="submit"]')
            ?.click();
        }
      }}
    />
  );
}

function validateWorkflowInputValue(
  input: WorkflowInput,
  value: unknown,
  mapping: WorkflowInputMapping,
) {
  const type = normalizedType(input);
  if (input.enum && !input.enum.includes(value)) {
    throw new Error(`${named(input)} must be one of: ${input.enum.join(", ")}.`);
  }
  if (type === "string" && typeof value !== "string") {
    throw new Error(`${named(input)} must be text.`);
  }
  if (
    isMediaBodyInput(input, mapping) &&
    !(typeof value === "string" && isPlausibleMediaReference(value))
  ) {
    throw new Error(`${named(input)} needs a file, a link or a Library picture.`);
  }
  if (type === "boolean" && typeof value !== "boolean") {
    throw new Error(`${named(input)} must be on or off.`);
  }
  if (type === "number" || type === "integer") {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw new Error(`${named(input)} must be a number.`);
    }
    if (type === "integer" && !Number.isInteger(value)) {
      throw new Error(`${named(input)} must be a whole number.`);
    }
    if (input.minimum !== undefined && value < input.minimum) {
      throw new Error(`${named(input)} must be at least ${input.minimum}.`);
    }
    if (input.maximum !== undefined && value > input.maximum) {
      throw new Error(`${named(input)} must be at most ${input.maximum}.`);
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
    if (target.in === "file+extension" && isFileInput(input, mapping)) return;
    setExtensionValue(root, target.field, value);
    return;
  }
  if (input.name === target.field && !target.combine?.length) {
    if (value === undefined) {
      delete root[target.field];
    } else {
      root[target.field] = value;
    }
    return;
  }
  if (isUnsupportedCombinedInput(input, inputs, mapping)) return;
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
    if (target.in === "file+extension" && isFileInput(input, mapping)) {
      return undefined;
    }
    return readExtension(root)[target.field];
  }
  if (input.name === target.field && !target.combine?.length) {
    return root[target.field];
  }
  if (isUnsupportedCombinedInput(input, inputs, mapping)) return undefined;
  if (target.combine?.length || hasSharedRequestField(inputs, input, mapping)) {
    return readCombinedPart(root[target.field], input.name);
  }
  return root[target.field];
}

function safeObject(
  json: string,
): { ok: true; value: JsonObject } | { ok: false; error: string } {
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
        : isFileInput(input, mapping)
          ? "file"
          : "body");
  return {
    in: location,
    field: mapped?.field ?? input.request_field ?? input.name,
    combine: mapped?.combine,
  };
}

/** The input's name as a person reads it, for messages. */
function named(input: WorkflowInput) {
  return inputLabel(input, "other");
}

function normalizedType(input: WorkflowInput) {
  return input.type.toLowerCase();
}

/**
 * An input that travels as a multipart file part. Whether it is a picture (or
 * other media) at all is the shared catalog rule (`isMediaInput`: the asset
 * flag, the declared media type, the type), never the input's name; the
 * gateway calls an asset `type: "string"` and flags it `asset: true`. Where it
 * travels is the route's field mapping, else the catalog's "applies" words.
 */
export function isFileInput(
  input: WorkflowInput,
  mapping: WorkflowInputMapping = {},
) {
  const type = normalizedType(input);
  if (FILE_TYPES.has(type)) return true;
  const mapped = mapping[input.name];
  if (mapped?.in === "file" || mapped?.in === "file+extension") return true;
  if (!isMediaInput(input)) return false;
  if (mapped?.in) return false;
  const applies = String(input.applies ?? "").toLowerCase();
  if (applies.includes("file upload") || applies.includes("multipart")) {
    return true;
  }
  if (
    applies.includes("request body") ||
    applies.includes("workflow extension")
  ) {
    return false;
  }
  return type === "asset";
}

/** The multipart part name a file input is sent as. */
export function fileFieldOf(
  input: WorkflowInput,
  mapping: WorkflowInputMapping = {},
): string {
  return inputTarget(input, mapping).field;
}

/**
 * A media input that travels inside the request (a data URI, a link or an
 * asset id) rather than as a multipart file. Whether it is media at all is
 * the shared catalog rule (`isMediaInput`), the same one the Workflows
 * summaries use, never the input's name.
 */
export function isMediaBodyInput(
  input: WorkflowInput,
  mapping: WorkflowInputMapping = {},
): boolean {
  if (input.ignored || input.accepted === false) return false;
  if (isFileInput(input, mapping)) return false;
  const location = inputTarget(input, mapping).in;
  if (location === "none" || location === "file" || location === "form") {
    return false;
  }
  const applies = String(input.applies ?? "").toLowerCase();
  if (applies.includes("file upload") || applies.includes("multipart")) {
    return false;
  }
  return isMediaInput(input);
}

function inputExplanation(
  input: WorkflowInput,
  mapping: WorkflowInputMapping = {},
) {
  if (isFileInput(input, mapping)) {
    return `Sent as the file part ${fileFieldOf(input, mapping)}.`;
  }
  if (normalizedType(input) === "asset") {
    return "Use a data URI, http(s) URL, gateway asset id, or signed asset URL.";
  }
  if (input.ignored) {
    return "Known input, but this route ignores it for the selected workflow.";
  }
  if (input.accepted === false) {
    return "Known input, but not available for this route.";
  }
  const parts = [`Writes ${input.request_field || input.name}`];
  if (input.default !== undefined)
    parts.push(`default ${valueText(input.default)}`);
  if (input.minimum !== undefined || input.maximum !== undefined) {
    parts.push(`range ${input.minimum ?? "-∞"} to ${input.maximum ?? "∞"}`);
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
    const parsed = parseExtensionString(value);
    return namespaceObject(parsed);
  }
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return namespaceObject(value as JsonObject);
  }
  return {};
}

function extensionEnvelope(root: JsonObject): JsonObject {
  const value = root.extensions;
  if (typeof value === "string") {
    return parseExtensionString(value);
  }
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as JsonObject;
  }
  return {};
}

function extensionContainer(root: JsonObject) {
  return namespaceObject(extensionEnvelope(root));
}

function writeExtensionContainer(
  root: JsonObject,
  workflowExtension: JsonObject,
) {
  const envelope = {
    ...extensionEnvelope(root),
    "com.azizam.workflow": workflowExtension,
  };
  root.extensions =
    typeof root.extensions === "string" ? JSON.stringify(envelope) : envelope;
}

function validateExtensionEnvelope(root: JsonObject) {
  const value = root.extensions;
  if (value === undefined || value === null) return;
  const envelope =
    typeof value === "string"
      ? parseExtensionString(value)
      : value && typeof value === "object" && !Array.isArray(value)
        ? (value as JsonObject)
        : null;
  if (!envelope) {
    throw new Error("extensions must be a JSON object.");
  }
  const namespace = envelope["com.azizam.workflow"];
  if (
    namespace !== undefined &&
    namespace !== null &&
    (typeof namespace !== "object" || Array.isArray(namespace))
  ) {
    throw new Error("extensions.com.azizam.workflow must be a JSON object.");
  }
}

function parseExtensionString(value: string): JsonObject {
  try {
    const parsed = JSON.parse(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("extensions must be a JSON object.");
    }
    return parsed as JsonObject;
  } catch {
    throw new Error(
      "extensions must be valid JSON before using guided controls.",
    );
  }
}

function namespaceObject(value: JsonObject) {
  const namespace = value["com.azizam.workflow"];
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
        : (current.width ?? defaultFor(inputs, widthName));
    const height =
      input.name === heightName
        ? value
        : (current.height ?? defaultFor(inputs, heightName));
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

function isUnsupportedCombinedInput(
  input: WorkflowInput,
  inputs: WorkflowInput[],
  mapping: WorkflowInputMapping,
) {
  if (isFileInput(input, mapping)) return false;
  const target = inputTarget(input, mapping);
  if (input.name === target.field && !target.combine?.length) return false;
  const combined =
    Boolean(target.combine?.length) ||
    hasSharedRequestField(inputs, input, mapping);
  return combined && target.field !== "size";
}

function sharedParts(
  inputs: WorkflowInput[],
  input: WorkflowInput,
  mapping: WorkflowInputMapping,
) {
  const target = inputTarget(input, mapping);
  return inputs
    .filter((item) => inputTarget(item, mapping).field === target.field)
    .filter((item) => item.name !== target.field)
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

// Select item for "omit this optional field"; never a real enum value.
const UNSET = "__unset__";

function selectValue(value: unknown) {
  return value === undefined || value === null ? "" : String(value);
}

function numberDisplay(value: unknown) {
  return typeof value === "number" && Number.isFinite(value)
    ? String(value)
    : "";
}

function valueText(value: unknown) {
  if (value === undefined) return "none";
  if (typeof value === "string") return value || "empty string";
  return JSON.stringify(value);
}
