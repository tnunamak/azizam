"use client";

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
  const other = Boolean(
    choices?.allow_other && !voices.some((voice) => voice.id === value),
  );
  return (
    <div className="space-y-2">
      <label className="block space-y-2">
        <span className="text-sm font-medium">Voice</span>
        <select
          className="border-input bg-card rounded-control min-h-11 w-full border px-3 text-base"
          value={other ? "__other__" : value}
          disabled={loading || (!voices.length && !choices?.allow_other)}
          aria-busy={loading}
          onChange={(event) =>
            onChange(
              event.target.value === "__other__" ? "" : event.target.value,
            )
          }
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
      {!loading && !voices.length && !choices?.allow_other ? (
        <p className="text-muted-foreground text-sm">
          No voices are listed for this model. Your request will use its default
          voice.
        </p>
      ) : null}
      {other && !loading ? (
        <label className="block space-y-2">
          <span className="text-sm font-medium">Voice ID</span>
          <Input
            value={value}
            onChange={(event) => onChange(event.target.value)}
          />
        </label>
      ) : null}
    </div>
  );
}
