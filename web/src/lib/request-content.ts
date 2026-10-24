/**
 * What a kept request reads as, by its kind (docs/design/REQUEST_RETENTION.md section 17, slice 5b1 r2). The History
 * drawer draws what this returns. It reads the kept body as the gateway stored it: a removed value is a marker there,
 * and is not shown as one. The raw body stays for Technical details.
 */
import {
  hasRemovedPart,
  requestKind,
  sizeWords,
  statusWords,
  withoutRemovedParts,
  type RequestHistoryRecord,
  type RequestShape,
} from "./request-history";

/** A label and a value, as a detail page shows its facts. */
export type Fact = [label: string, value: string];

/** One line of a conversation: a message's words, or a labelled line for a tool's call or result. */
export interface Turn {
  role: "user" | "assistant" | "tool";
  /** The words of the message; empty for a tool's line. */
  text: string;
  /** Labelled lines, such as "Tool call: get_weather" or "Tool result". */
  lines: string[];
}

/** What came back for a conversation. `kept` is false when the gateway did not keep the reply. */
export interface Reply {
  kept: boolean;
  text: string;
  lines: string[];
  /** The failure the gateway gave, when the status was not a success. */
  failure: string | null;
}

export type RequestContent =
  | { shape: "conversation"; turns: Turn[]; reply: Reply }
  | { shape: "media"; prompt: string | null; facts: Fact[]; failure: string | null }
  | { shape: "transcription"; file: Fact[]; transcript: string | null; failure: string | null }
  | { shape: "embeddings"; inputs: string[]; numbers: number | null; failure: string | null }
  | { shape: "other"; facts: Fact[]; failure: string | null };

type Json = Record<string, unknown>;

/** The settings a picture, sound or video request names, in the order a reader looks for them. */
const MEDIA_SETTINGS: Array<[key: string, label: string]> = [
  ["size", "Size"],
  ["quality", "Quality"],
  ["style", "Style"],
  ["n", "Count"],
  ["seconds", "Length"],
  ["duration", "Length"],
  ["aspect_ratio", "Aspect ratio"],
  ["resolution", "Resolution"],
  ["voice", "Voice"],
  ["speed", "Speed"],
  ["response_format", "Format"],
  ["output_format", "Format"],
  ["language", "Language"],
];

/** How many settings a request's facts show; the rest stay in Technical details. */
const MAX_FACTS = 12;

function isObject(value: unknown): value is Json {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/** A message's words: its string content, or the text parts of a content list. */
function textOf(content: unknown): string {
  if (typeof content === "string") return withoutRemovedParts(content).trim();
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => (typeof part === "string" ? part : isObject(part) && typeof part.text === "string" ? part.text : ""))
    .map((text) => withoutRemovedParts(text).trim())
    .filter(Boolean)
    .join("\n");
}

/** The labelled lines for the tools a message called or returned: chat's tool_calls and Anthropic's tool blocks. */
function toolLinesOf(message: Json): string[] {
  const lines: string[] = [];
  for (const call of Array.isArray(message.tool_calls) ? message.tool_calls : []) {
    if (isObject(call) && isObject(call.function) && typeof call.function.name === "string")
      lines.push(`Tool call: ${call.function.name}`);
  }
  for (const block of Array.isArray(message.content) ? message.content : []) {
    if (!isObject(block)) continue;
    if (block.type === "tool_use" && typeof block.name === "string") lines.push(`Tool call: ${block.name}`);
    if (block.type === "tool_result") lines.push("Tool result");
  }
  return lines;
}

/** The message of a failure the gateway gave, with any removed part taken out. Null when it gave none. */
function failureOf(body: unknown): string | null {
  if (!isObject(body) || !isObject(body.error) || typeof body.error.message !== "string") return null;
  return withoutRemovedParts(body.error.message).trim() || null;
}

/** The failure to show for a reply's status: the gateway's own message when it has one, else the status in words. */
function failureWords(status: number | null | undefined, body: unknown): string | null {
  if (typeof status !== "number" || (status >= 200 && status < 300)) return null;
  return failureOf(body) ?? statusWords(status);
}

/** The kept body of the request, when the gateway kept one: the JSON object, or the form's fields and files. */
function bodyOf(accepted: unknown): Json | null {
  return isObject(accepted) && isObject(accepted.body) ? accepted.body : null;
}

/** The conversation as the person and the model wrote it. A system or developer message is not shown. */
function turnsOf(body: Json): Turn[] {
  const items: unknown[] = Array.isArray(body.messages)
    ? body.messages
    : typeof body.input === "string"
      ? [{ role: "user", content: body.input }]
      : Array.isArray(body.input)
        ? body.input
        : [];
  const turns: Turn[] = [];
  for (const item of items) {
    if (!isObject(item)) continue;
    if (item.type === "function_call" && typeof item.name === "string") {
      turns.push({ role: "tool", text: "", lines: [`Tool call: ${item.name}`] });
    } else if (item.type === "function_call_output" || item.role === "tool") {
      turns.push({ role: "tool", text: "", lines: ["Tool result"] });
    } else if (item.role === "user" || item.role === "assistant") {
      const role = item.role;
      const text = textOf(item.content);
      const lines = toolLinesOf(item);
      if (text || lines.length) turns.push({ role, text, lines });
    }
  }
  return turns;
}

/** What came back for a conversation: the model's words and the tools it called, from the reply's own shape. */
function replyOf(received: unknown): Reply {
  const reply = isObject(received) ? received : {};
  const body = reply.body;
  const status = typeof reply.status === "number" ? reply.status : null;
  // A failure is said even when the body was not kept: the status is kept either way.
  const failure = failureWords(status, body);
  if (!isObject(body) || "omitted" in body) return { kept: false, text: "", lines: [], failure };
  let text = "";
  const lines: string[] = [];
  if (Array.isArray(body.choices) && isObject(body.choices[0]) && isObject(body.choices[0].message)) {
    const message = body.choices[0].message;
    text = textOf(message.content);
    lines.push(...toolLinesOf(message));
  } else if (Array.isArray(body.output)) {
    for (const item of body.output) {
      if (!isObject(item)) continue;
      if (item.type === "function_call" && typeof item.name === "string") lines.push(`Tool call: ${item.name}`);
      else text = [text, textOf(item.content)].filter(Boolean).join("\n");
    }
  } else {
    text = textOf(body.content);
    lines.push(...toolLinesOf(body));
  }
  return { kept: true, text, lines, failure };
}

/** The scalar settings of a body, in the words a reader knows. Nested values stay in Technical details. */
function settingsOf(source: Json): Fact[] {
  const facts: Fact[] = [];
  const seen = new Set<string>();
  for (const [key, label] of MEDIA_SETTINGS) {
    const value = source[key];
    const words =
      typeof value === "string" ? withoutRemovedParts(value).trim()
        : typeof value === "number" || typeof value === "boolean" ? String(value)
          : "";
    if (!words || seen.has(label)) continue;
    seen.add(label);
    facts.push([label, words]);
  }
  return facts;
}

/** A readable fact for each top-level scalar of a body, for a request whose shape the screens do not know. */
function factsOf(source: Json): Fact[] {
  const facts: Fact[] = [];
  for (const [key, value] of Object.entries(source)) {
    if (key === "headers" || key === "omitted") continue;
    const words =
      typeof value === "string" ? withoutRemovedParts(value).trim()
        : typeof value === "number" || typeof value === "boolean" ? String(value)
          : null;
    if (words) facts.push([labelOf(key), words]);
    if (facts.length === MAX_FACTS) break;
  }
  return facts;
}

/** "max_tokens" reads "Max tokens". */
function labelOf(key: string): string {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** What the gateway says was left out of a body, in words. */
function omittedFact(body: Json): Fact[] {
  return typeof body.omitted === "string" ? [["Body", "Not kept, because it was too large or couldn’t be read"]] : [];
}

/**
 * The record's content as its kind reads it. A kind the screens do not know reads as a plain summary of its fields.
 */
export function contentOf(record: RequestHistoryRecord): RequestContent {
  const shape: RequestShape = requestKind(record.summary.route).shape;
  const status = record.summary.status;
  const body = bodyOf(record.parts.accepted) ?? {};
  const fields = isObject(body.fields) ? body.fields : null;
  const files = Array.isArray(body.files) ? body.files.filter(isObject) : [];
  const received = record.parts.received;
  const receivedBody = isObject(received) && isObject(received.body) ? received.body : undefined;

  if (shape === "conversation") {
    return { shape, turns: turnsOf(body), reply: replyOf(received) };
  }
  if (shape === "media") {
    const prompt =
      typeof body.prompt === "string" ? body.prompt
        : fields && typeof fields.prompt === "string" ? fields.prompt
          : typeof body.input === "string" ? body.input
            : null;
    const words = prompt ? withoutRemovedParts(prompt).trim() : null;
    const facts: Fact[] = [];
    if (words) facts.push(["Prompt", words]);
    facts.push(...settingsOf(fields ?? body));
    return { shape, prompt: words, facts, failure: failureWords(status, receivedBody) };
  }
  if (shape === "transcription") {
    const file = files[0];
    const fileFacts: Fact[] = file
      ? [["File", `${typeof file.media_type === "string" ? file.media_type : "file"} · ${sizeWords(Number(file.bytes) || 0)}`]]
      : [["File", "Not kept"]];
    const transcript = isObject(receivedBody) && typeof receivedBody.text === "string"
      ? withoutRemovedParts(receivedBody.text).trim() || null
      : null;
    return { shape, file: fileFacts, transcript, failure: failureWords(status, receivedBody) };
  }
  if (shape === "embeddings") {
    const input = body.input;
    const inputs = typeof input === "string" ? [input] : Array.isArray(input) ? input.filter((item): item is string => typeof item === "string") : [];
    const vectors = isObject(receivedBody) && Array.isArray(receivedBody.data) ? receivedBody.data : null;
    const numbers = vectors
      ? vectors.reduce<number | null>((total, item) => {
          const vector = isObject(item) ? item.embedding : undefined;
          return Array.isArray(vector) && total !== null ? total + vector.length : null;
        }, 0)
      : null;
    return { shape, inputs: inputs.map((text) => withoutRemovedParts(text).trim()).filter(Boolean), numbers, failure: failureWords(status, receivedBody) };
  }
  const facts = "omitted" in body ? omittedFact(body) : factsOf(fields ?? body);
  return { shape: "other", facts, failure: failureWords(status, receivedBody) };
}

/** True when the record's kept parts have a removed part: the drawer then says so once, in one quiet line. */
export function hasRemovedDetails(record: RequestHistoryRecord): boolean {
  return hasRemovedPart(record.parts);
}
