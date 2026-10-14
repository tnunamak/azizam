"use client";

/**
 * The one model of a custom-path speech server, and its voices. Labels, help
 * and the example names come from the definition's fields.
 */
import { useState } from "react";
import { X } from "lucide-react";
import type { EngineDefinition } from "@/lib/engines-api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fieldOf } from "../add-engine/draft";
import { FormField, NativeSelect, Tag } from "./form";

export interface SpeechDraft {
  public_name: string;
  server_model: string;
  voices: string[];
  default_voice: string;
  /** The public name is still the definition's suggestion. */
  suggested: boolean;
}

export function speechDraft(definition: EngineDefinition, values?: Partial<SpeechDraft>): SpeechDraft {
  const fallback = String(fieldOf(definition, "public_name")?.default ?? "");
  return {
    public_name: values?.public_name ?? fallback,
    server_model: values?.server_model ?? "",
    voices: values?.voices ?? [],
    default_voice: values?.default_voice ?? "",
    suggested: values?.public_name === undefined || values.public_name === fallback,
  };
}

export function speechValues(definition: EngineDefinition, draft: SpeechDraft) {
  const out: Record<string, unknown> = { public_name: draft.public_name.trim() };
  if (fieldOf(definition, "server_model")) out.server_model = draft.server_model.trim();
  if (fieldOf(definition, "voices")) {
    out.voices = draft.voices;
    if (draft.voices.length) out.default_voice = draft.voices.includes(draft.default_voice) ? draft.default_voice : draft.voices[0];
  }
  return out;
}

export function SpeechFields({
  definition,
  draft,
  onChange,
  readOnly,
  showSuggested = true,
}: {
  definition: EngineDefinition;
  draft: SpeechDraft;
  onChange: (next: SpeechDraft) => void;
  readOnly?: boolean;
  showSuggested?: boolean;
}) {
  const [voiceIn, setVoiceIn] = useState("");
  const pub = fieldOf(definition, "public_name");
  const server = fieldOf(definition, "server_model");
  const voices = fieldOf(definition, "voices");
  const defaultVoice = fieldOf(definition, "default_voice");

  function addVoice() {
    const v = voiceIn.trim();
    if (!v || draft.voices.includes(v)) return;
    onChange({ ...draft, voices: [...draft.voices, v] });
    setVoiceIn("");
  }

  return (
    <div className="flex flex-col gap-4">
      {pub && (
        <FormField
          label={pub.label}
          required
          help={pub.help}
          tag={showSuggested && draft.suggested ? <Tag tone="suggest">Suggested</Tag> : undefined}
        >
          {(control) => (
            <Input
              {...control}
              value={draft.public_name}
              disabled={readOnly}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => onChange({ ...draft, public_name: event.target.value, suggested: false })}
            />
          )}
        </FormField>
      )}
      {server && (
        <FormField label={server.label} help={server.help}>
          {(control) => (
            <Input
              {...control}
              className="font-mono"
              placeholder={server.placeholder}
              value={draft.server_model}
              disabled={readOnly}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => onChange({ ...draft, server_model: event.target.value })}
            />
          )}
        </FormField>
      )}
      {voices && (
        <div className="flex flex-col gap-1.5">
          <p className="text-meta font-medium">{voices.label}</p>
          {draft.voices.length ? (
            <ul className="flex flex-wrap gap-2">
              {draft.voices.map((v) => (
                <li key={v} className="bg-muted rounded-control text-meta flex items-center gap-1 py-0.5 pr-0.5 pl-2 font-mono">
                  {v}
                  {!readOnly && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      aria-label={`Remove voice ${v}`}
                      onClick={() => onChange({ ...draft, voices: draft.voices.filter((x) => x !== v) })}
                    >
                      <X aria-hidden="true" />
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-meta text-muted-foreground">No voices listed. Azizam sends no voice name, and the server uses its own.</p>
          )}
        </div>
      )}
      {voices && !readOnly && (
        <div className="flex flex-wrap items-end gap-2">
          <FormField className="min-w-0 flex-1" label="Add a voice by name" help={voices.help}>
            {(control) => (
              <Input
                {...control}
                placeholder="The name the server uses"
                value={voiceIn}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => setVoiceIn(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    addVoice();
                  }
                }}
              />
            )}
          </FormField>
          <Button type="button" variant="outline" size="md" className="mb-6" onClick={addVoice}>
            Add voice
          </Button>
        </div>
      )}
      {defaultVoice && draft.voices.length > 1 && (
        <FormField label={defaultVoice.label}>
          {(control) => (
            <NativeSelect
              {...control}
              value={draft.voices.includes(draft.default_voice) ? draft.default_voice : draft.voices[0]}
              disabled={readOnly}
              onChange={(event) => onChange({ ...draft, default_voice: event.target.value })}
            >
              {draft.voices.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </NativeSelect>
          )}
        </FormField>
      )}
    </div>
  );
}
