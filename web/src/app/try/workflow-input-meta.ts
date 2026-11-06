import type { WorkflowInput } from "./workflow-inputs";
import {
  MEDIA_DESCRIPTION,
  MEDIA_NOUN,
  mediaKindOf,
  withArticle,
} from "@/lib/workflow-media-input";

/**
 * Plain words and checks for workflow inputs on Try.
 *
 * The gateway describes every input the way an API client needs it (name,
 * type, request field). A member needs different things: a label, one helper
 * line, a placeholder that is clearly an example, and a clear message when
 * something is missing. This file holds those; it has no UI.
 */

export type OutputKind = "video" | "image" | "audio" | "other";

/** Largest file the picker turns into a data URI; larger ones need a link. */
export const MAX_MEDIA_BYTES = 10 * 1024 * 1024;
export const MAX_MEDIA_LABEL = "10 MB";

const OUTPUT_NOUN: Record<Exclude<OutputKind, "other">, string> = {
  video: "video",
  image: "picture",
  audio: "sound",
};

type OutputLike = { kind?: string; type?: string; media_type?: string };

export function outputKindOf(outputs: OutputLike[] | undefined): OutputKind {
  const first = outputs?.[0];
  const text = [first?.kind, first?.type, first?.media_type]
    .filter((value): value is string => typeof value === "string")
    .join(" ")
    .toLowerCase();
  if (/\bvideo\b|video\//.test(text)) return "video";
  if (/\bimage\b|image\//.test(text)) return "image";
  if (/\baudio\b|audio\//.test(text)) return "audio";
  return "other";
}

function gatewayBoilerplate(description: string | undefined) {
  return Boolean(description && MEDIA_DESCRIPTION.test(description));
}

/**
 * The manifest's label as a field label: "a face photo" -> "Face photo". The
 * catalog writes labels as nouns for the Workflows summary ("Needs: a face
 * photo and text"); a form label drops the article.
 */
function manifestLabel(input: WorkflowInput): string | null {
  const label = input.label?.trim().replace(/^(?:an?|the)\s+/i, "");
  return label ? label[0].toUpperCase() + label.slice(1) : null;
}

/** A field name a person can read: "batch_size" -> "Batch size". */
export function humanizeInputName(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : name;
}

export function inputLabel(input: WorkflowInput, output: OutputKind): string {
  const declared = manifestLabel(input);
  if (declared) return declared;
  if (input.request_field === "input_reference" || input.name === "image") {
    return "Starting picture";
  }
  switch (input.name) {
    case "prompt":
      return output === "other"
        ? "Prompt"
        : `Describe the ${OUTPUT_NOUN[output]}`;
    case "negative_prompt":
      return "Avoid (optional)";
    case "image_strength":
      return "Picture strength";
    case "seconds":
      return "Length in seconds";
    case "size":
      return "Size";
    case "seed":
      return "Seed";
    default:
      // Never the raw field name ("batch_size"): the words it is made of.
      return humanizeInputName(input.name);
  }
}

export function inputHint(
  input: WorkflowInput,
  output: OutputKind,
): string | undefined {
  // The manifest's one plain sentence comes first; research notes and the
  // technical description stay out of the form.
  if (input.help?.trim()) return input.help.trim();
  if (input.request_field === "input_reference" || input.name === "image") {
    return "The picture the video starts from.";
  }
  switch (input.name) {
    case "prompt":
      if (output === "video") return "What happens in the clip.";
      if (output === "image") return "What the picture shows.";
      if (output === "audio") return "What it should sound like.";
      return undefined;
    case "negative_prompt":
      return "Things to keep out of the result.";
    case "image_strength":
      return "Higher keeps the video closer to your picture.";
    case "seed":
      return "Reuse this number to help repeat a result with the same model and settings.";
    case "seconds":
      return "How long the clip runs.";
    case "size":
      return "Width by height, such as 960x512.";
    default:
      return gatewayBoilerplate(input.description)
        ? undefined
        : input.description;
  }
}

export function inputPlaceholder(
  input: WorkflowInput,
  output: OutputKind,
): string | undefined {
  if (input.name === "prompt") {
    if (output === "video")
      return "A paper boat drifts across a pond as the camera slowly moves in.";
    if (output === "image")
      return "A teal desk lamp on a wooden table in soft morning light.";
    if (output === "audio") return "Rain on a tin roof, then distant thunder.";
    return undefined;
  }
  if (input.name === "negative_prompt" && output !== "audio") {
    return "blurry, distorted, text, watermark";
  }
  if (input.name === "size") return "960x512";
  return undefined;
}

/** What to say when a required input has no value. */
export function missingMessage(input: WorkflowInput, output: OutputKind): string {
  if (input.request_field === "input_reference" || input.name === "image") {
    return "Choose a picture to start from";
  }
  if (input.name === "prompt") {
    return output === "other"
      ? "Write a prompt first"
      : `Describe the ${OUTPUT_NOUN[output]} first`;
  }
  const media = mediaKindOf(input);
  if (media) return `Choose ${withArticle(MEDIA_NOUN[media])} first`;
  return `Add ${inputLabel(input, output)} to continue`;
}

const HEX_ID = /^(?:asset:)?(?:img|vid|aud)_[0-9a-f]{64}$/;
const BASE64 = /^[A-Za-z0-9+/_-]{64,}={0,2}$/;

/** True for a string the gateway can resolve as media; false for "value". */
export function isPlausibleMediaReference(value: string): boolean {
  const text = value.trim();
  if (!text) return false;
  if (/^data:[a-z]+\/[a-z0-9.+-]+;base64,/i.test(text)) return true;
  if (/^https?:\/\/[^\s/]+/i.test(text)) return true;
  return HEX_ID.test(text) || BASE64.test(text);
}

const ASSET_IN_LINK = /(?:\/media\/|[?&]asset(?:_id)?=)((?:img|vid|aud)_[0-9a-f]{64})/;

/** A pasted Library link or id becomes the asset id; any other text stays. */
export function assetIdFromPasted(text: string): string {
  const trimmed = text.trim();
  if (HEX_ID.test(trimmed)) return trimmed;
  const found = ASSET_IN_LINK.exec(trimmed);
  return found ? found[1] : trimmed;
}
