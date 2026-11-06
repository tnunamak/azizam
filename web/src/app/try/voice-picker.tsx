"use client";

import { useId, useState } from "react";
import { Input } from "@/components/ui/input";

export type { SpeechVoiceCatalog as SpeechVoices } from "@/lib/product-api-types";
import type { SpeechVoiceCatalog as SpeechVoices } from "@/lib/product-api-types";

export function selectedVoice(
  choices: SpeechVoices | undefined,
  value?: string,
) {
  if (
    value !== undefined &&
    (choices?.allow_other ||
      choices?.voices.some((voice) => voice.id === value))
  )
    return value;
  return choices?.default_voice ?? choices?.voices[0]?.id ?? "";
}

export function VoicePicker({
  choices,
  value,
  onChange,
  loading,
}: {
  choices?: SpeechVoices;
  value: string;
  onChange: (voice: string) => void;
  loading: boolean;
}) {
  const voices = choices?.voices ?? [];
  const [customEntry, setCustomEntry] = useState(() =>
    Boolean(choices?.allow_other && !voices.some((voice) => voice.id === value)),
  );
  const other = Boolean(
    choices?.allow_other &&
      (customEntry || !voices.some((voice) => voice.id === value)),
  );
  const fallbackId = useId();
  const defaultVoiceOnly = !loading && !voices.length && !choices?.allow_other;
  return (
    <div className="space-y-2">
      <label className="block space-y-2">
        <span className="text-sm font-medium">Voice</span>
        <select
          className="border-input bg-card rounded-control min-h-11 min-w-11 w-full border px-3 text-base"
          value={other ? "__other__" : value}
          disabled={loading || (!voices.length && !choices?.allow_other)}
          aria-busy={loading}
          aria-describedby={defaultVoiceOnly ? fallbackId : undefined}
          onChange={(event) => {
            const custom = event.target.value === "__other__";
            setCustomEntry(custom);
            onChange(custom ? "" : event.target.value);
          }}
        >
          {loading ? (
            <option value="">Loading voices…</option>
          ) : voices.length ? (
            voices.map((voice) => (
              <option key={voice.id} value={voice.id}>
                {voice.name}
              </option>
            ))
          ) : (
            <option value="">No voices listed</option>
          )}
          {!loading && choices?.allow_other ? (
            <option value="__other__">Other voice</option>
          ) : null}
        </select>
      </label>
      {defaultVoiceOnly ? (
        <p id={fallbackId} className="text-muted-foreground text-sm">
          No voices are listed for this model. Your request will use its default
          voice.
        </p>
      ) : null}
      {other && !loading ? (
        <label className="block space-y-2">
          <span className="text-sm font-medium">Voice ID</span>
          <Input
            className="min-h-11 min-w-11"
            value={value}
            onChange={(event) => {
              setCustomEntry(true);
              onChange(event.target.value);
            }}
          />
        </label>
      ) : null}
    </div>
  );
}
