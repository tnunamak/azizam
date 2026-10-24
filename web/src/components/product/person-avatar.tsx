"use client";

import * as React from "react";
import { Avatar as AvatarPrimitive, RadioGroup as RadioGroupPrimitive } from "radix-ui";
import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

/**
 * The household's avatars: the picture the person's sign-in provider shared, a picture the gateway made for
 * them, or a generated disc with the person's initial. The picture is the default when there is one; a disc
 * the person chose stays. Everything here is drawn from the stable person id, never the display name, so a
 * rename keeps the colour.
 */

// Palette values are image artwork, independent of the surrounding theme.
// Every sRGB gradient stop has >= 4.9:1 contrast against the white initials.
// The ids are the words the gateway stores: "v1" to "v8".
export const generatedAvatarPalettes = [
  { id: "v1", label: "Tide", from: "#244759", to: "#326D78" },
  { id: "v2", label: "Fern", from: "#294A40", to: "#417363" },
  { id: "v3", label: "Iris", from: "#45436B", to: "#706A9D" },
  { id: "v4", label: "Rose", from: "#673D52", to: "#93627A" },
  { id: "v5", label: "Clay", from: "#634633", to: "#8E6846" },
  { id: "v6", label: "Dusk", from: "#3D4E75", to: "#5D7093" },
  { id: "v7", label: "Plum", from: "#5C3F61", to: "#845D86" },
  { id: "v8", label: "Slate", from: "#3B5050", to: "#586F6C" },
] as const;

export type GeneratedAvatarVariant =
  (typeof generatedAvatarPalettes)[number]["id"];

/** What the gateway stores as `avatar_choice`. "generated" is the picture the gateway made for the person. */
export type AvatarChoice = "auto" | "picture" | "generated" | GeneratedAvatarVariant;

export type AvatarSelection =
  | { kind: "auto" }
  | { kind: "generated"; variant: GeneratedAvatarVariant }
  | { kind: "sign-in" }
  | { kind: "made" };

export function selectionFromChoice(choice: string | null | undefined): AvatarSelection {
  if (choice === "picture") return { kind: "sign-in" };
  if (choice === "generated") return { kind: "made" };
  const palette = generatedAvatarPalettes.find((entry) => entry.id === choice);
  return palette
    ? { kind: "generated", variant: palette.id }
    : { kind: "auto" };
}

export function choiceFromSelection(selection: AvatarSelection): AvatarChoice {
  if (selection.kind === "sign-in") return "picture";
  if (selection.kind === "made") return "generated";
  return selection.kind === "generated" ? selection.variant : "auto";
}

/**
 * The first letter or digit of the name, upper-cased, or "?". One letter at every
 * size, so the account button, the picker, People and Library read the same;
 * two letters crowd a 16 to 28 px disc.
 */
export function getAvatarInitials(name: string): string {
  const first = name
    .normalize("NFC")
    .match(/[\p{L}\p{N}][\p{M}]*/u)?.[0];
  return first ? first.toLocaleUpperCase() : "?";
}

/** Never seed with a display name or email. The id stays the same across renames. */
export function getGeneratedAvatar(
  seed: string,
  variant?: GeneratedAvatarVariant,
) {
  let hash = 2166136261;
  for (let index = 0; index < seed.length; index += 1) {
    hash = Math.imul(hash ^ seed.charCodeAt(index), 16777619) >>> 0;
  }
  const palette =
    generatedAvatarPalettes.find((entry) => entry.id === variant) ??
    generatedAvatarPalettes[hash % generatedAvatarPalettes.length] ??
    generatedAvatarPalettes[0];
  const angle = (hash >>> 3) % 360;
  return {
    ...palette,
    angle,
    foreground: "#FFFFFF",
    // Explicit sRGB interpolation preserves the audited contrast bound.
    background: `linear-gradient(${angle}deg in srgb, ${palette.from}, ${palette.to})`,
  };
}

/** The one place the gateway serves a person's picture from (gateway/me_avatar.py). */
export const AVATAR_PICTURE_PATH = "/me/api/profile/avatar/picture";

/**
 * The browser loads a picture only from the gateway's own path. A provider's
 * address (https or not) is refused here, so a picture is never hot-linked
 * from another site even if the payload were wrong.
 */
export function getAvatarPictureUrl(value?: string | null): string | undefined {
  if (!value || /[\s\\\u0000-\u001f]/u.test(value)) return undefined;
  const path = value.split(/[?#]/u, 1)[0];
  return path === AVATAR_PICTURE_PATH ? value : undefined;
}

const sizes = {
  /** 16: the maker's mark on a media tile (craft PR 5). */
  "2xs": "size-4 text-2xs",
  xs: "size-5 text-2xs",
  sm: "size-6 text-xs",
  md: "size-8 text-sm",
  lg: "size-10 text-base",
  /** 64: the preview in the avatar picker. */
  xl: "size-16 text-xl",
} as const;
export type PersonAvatarSize = keyof typeof sizes;

export type PersonAvatarProps = {
  /** The stable person id the colour is drawn from. Without one, the name is used. */
  personId?: string | null;
  name: string;
  selection?: AvatarSelection | null;
  /** The gateway path of the picture. It is shown unless the person chose a generated disc. */
  pictureUrl?: string | null;
  size?: PersonAvatarSize;
  className?: string;
  /** Use when an adjacent label or containing control already names the person. */
  decorative?: boolean;
  /** Overrides the accessible name; implies not decorative. */
  label?: string;
  /** Wire `data-slot`, so the household's marks stay findable. */
  slot?: string;
};

export function PersonAvatar({
  personId,
  name,
  selection,
  pictureUrl,
  size = "md",
  className,
  decorative = false,
  label,
  slot = "person-avatar",
}: PersonAvatarProps) {
  const seed = personId || `name:${name.trim().toLowerCase()}`;
  const generated = getGeneratedAvatar(
    seed,
    selection?.kind === "generated" ? selection.variant : undefined,
  );
  // The default (no choice, or automatic) is the picture when the gateway gave one; the gateway withholds
  // another person's picture unless they chose it, so that case shows the disc.
  const imageUrl =
    selection?.kind === "generated" ? undefined : getAvatarPictureUrl(pictureUrl);
  const hidden = decorative && !label;
  const initials = getAvatarInitials(name);

  return (
    <AvatarPrimitive.Root
      data-slot={slot}
      data-avatar-selection={
        imageUrl ? (selection?.kind === "made" ? "made" : "sign-in") : "generated"
      }
      className={cn(
        "relative inline-flex shrink-0 overflow-hidden rounded-full font-semibold select-none",
        sizes[size],
        className,
      )}
      {...(hidden
        ? { "aria-hidden": true }
        : { role: "img", "aria-label": label ?? (name.trim() || "Unknown person") })}
    >
      {imageUrl && (
        <AvatarPrimitive.Image
          key={imageUrl}
          src={imageUrl}
          alt=""
          referrerPolicy="no-referrer"
          className="aspect-square size-full object-cover"
        />
      )}
      {/* Also what shows while the picture loads, and when it cannot be fetched. */}
      <AvatarPrimitive.Fallback
        className="flex size-full items-center justify-center rounded-full"
        style={{
          color: generated.foreground,
          backgroundColor: generated.from,
          backgroundImage: generated.background,
        }}
      >
        {initials}
      </AvatarPrimitive.Fallback>
    </AvatarPrimitive.Root>
  );
}

/**
 * A static sample of the style the gateway makes pictures in. It sits beside the make button, so the person
 * sees the kind of picture before they press. It is a file in the repo, never a picture made live.
 */
export const AVATAR_STYLE_SAMPLE_PATH = "/avatar-style-sample.svg";

/**
 * The make-my-avatar flow the picker shows. The caller owns the job, the candidate and the saving; the picker
 * only draws the state it is given. Nothing replaces the current avatar until `onKeep` runs.
 */
export type AvatarGenerationView = {
  /** This gateway has an image route to make a picture with, so the offer is shown. */
  available: boolean;
  /** A picture is being made: by the person's request, or as their automatic default. */
  making: boolean;
  /** The picture the last attempt made. It is not the person's avatar until they use it. */
  candidate: { assetId: string; src: string } | null;
  /** The last attempt did not make a picture. */
  failed: boolean;
  /** Start an attempt: the first one, another over the candidate, or a new one that replaces the picture. */
  onMake: () => void;
  /** Use the candidate as the person's avatar. */
  onKeep: (assetId: string) => void;
  /** Remove the picture the gateway made. */
  onRemove: () => void;
};

export type AvatarPickerProps = {
  personId: string;
  name: string;
  value?: AvatarSelection | null;
  /** The sign-in picture's gateway path, when the provider shared one. */
  signInPictureUrl?: string | null;
  /** The gateway path of the picture the gateway made, when the person has kept one. */
  madePictureUrl?: string | null;
  /** The make-my-avatar flow. Without it the picker offers the palette and the pictures the person has. */
  generation?: AvatarGenerationView | null;
  onChange: (selection: AvatarSelection) => void;
  disabled?: boolean;
  pending?: boolean;
  error?: string | null;
  className?: string;
};

// A source row. Standalone rows add the border (boxedRowClass); the generated row sits inside a bordered card.
const rowClass =
  "rounded-control flex min-h-14 w-full items-center gap-3 px-3 py-2 text-left text-sm cursor-pointer focus-visible:outline-ring focus-visible:outline-2 focus-visible:outline-offset-2 data-[state=checked]:border-primary data-[state=checked]:ring-2 data-[state=checked]:ring-primary disabled:pointer-events-none disabled:opacity-50";
const boxedRowClass = `border-border border ${rowClass}`;

// A colour swatch is a tall card. Its own class: `cn` does not merge Tailwind, so it must not inherit rowClass's
// height, gap and padding and then fight them.
const swatchClass =
  "border-border rounded-control flex min-h-16 cursor-pointer flex-col items-center justify-center gap-1 border p-2 text-center text-sm focus-visible:outline-ring focus-visible:outline-2 focus-visible:outline-offset-2 data-[state=checked]:border-primary data-[state=checked]:ring-2 data-[state=checked]:ring-primary disabled:pointer-events-none disabled:opacity-50";

/** The check mark on the selected row. Radix mounts the indicator only while the row is checked. */
function CheckMark() {
  return (
    <RadioGroupPrimitive.Indicator
      aria-hidden
      className="text-primary ml-auto flex size-5 shrink-0 items-center justify-center"
    >
      <Check className="size-4" />
    </RadioGroupPrimitive.Indicator>
  );
}

/** Controlled UI only. The caller owns persistence, pending state, and failures. */
export function AvatarPicker({
  personId,
  name,
  value,
  signInPictureUrl,
  madePictureUrl,
  generation,
  onChange,
  disabled = false,
  pending = false,
  error,
  className,
}: AvatarPickerProps) {
  const signInAvailable = Boolean(getAvatarPictureUrl(signInPictureUrl));
  const madeAvailable = Boolean(getAvatarPictureUrl(madePictureUrl));
  // The sign-in picture is the default, so it is the checked source unless the person picked another.
  const pictureSelected =
    signInAvailable && value?.kind !== "generated" && value?.kind !== "made";
  const madeSelected = madeAvailable && value?.kind === "made";
  const source = pictureSelected ? "sign-in" : madeSelected ? "made" : "initials";
  const selectedVariant = getGeneratedAvatar(
    personId,
    value?.kind === "generated" ? value.variant : undefined,
  ).id;
  const generatedOffered = Boolean(generation && (generation.available || madeAvailable));
  const candidate = generation?.candidate ?? null;
  const making = generation?.making === true;
  const failed = generation?.failed === true && !making;

  function chooseSource(next: string) {
    if (next === "sign-in") onChange({ kind: "sign-in" });
    else if (next === "made") onChange({ kind: "made" });
    else onChange({ kind: "generated", variant: selectedVariant });
  }

  return (
    <fieldset
      className={cn("min-w-0 space-y-4", className)}
      disabled={disabled || pending}
      aria-busy={pending}
    >
      {/* The dialog names the task; the group still needs a name for a screen reader. */}
      <legend className="sr-only">Choose your avatar</legend>
      <RadioGroupPrimitive.Root
        value={source}
        onValueChange={chooseSource}
        aria-label="Where your avatar comes from"
        className="grid gap-3"
      >
        {signInAvailable && (
          <RadioGroupPrimitive.Item value="sign-in" className={boxedRowClass}>
            <PersonAvatar
              personId={personId}
              name={name}
              selection={{ kind: "sign-in" }}
              pictureUrl={signInPictureUrl}
              size="md"
              decorative
            />
            <span className="flex min-w-0 flex-col">
              <span className="font-medium">Sign-in picture</span>
              <span className="text-muted-foreground text-xs">
                Azizam gets this picture from your sign-in provider.
              </span>
            </span>
            <CheckMark />
          </RadioGroupPrimitive.Item>
        )}
        {generatedOffered && (
          <div className="border-border rounded-control border">
            <RadioGroupPrimitive.Item
              value="made"
              disabled={!madeAvailable}
              className={rowClass}
            >
              {madeAvailable ? (
                <PersonAvatar
                  personId={personId}
                  name={name}
                  selection={{ kind: "made" }}
                  pictureUrl={madePictureUrl}
                  size="md"
                  decorative
                />
              ) : (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={AVATAR_STYLE_SAMPLE_PATH}
                  alt=""
                  className="size-8 shrink-0 rounded-full object-cover"
                />
              )}
              <span className="font-medium">Generated</span>
              {madeAvailable && <CheckMark />}
            </RadioGroupPrimitive.Item>
            <div className="space-y-3 px-3 pb-3">
              {candidate && (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-3">
                    {/* A signed link to a picture the person made, drawn as the Library draws its own. */}
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={candidate.src}
                      alt="New avatar"
                      className="size-16 rounded-full object-cover"
                    />
                    <div className="flex flex-wrap gap-2">
                      <Button type="button" size="md" onClick={() => generation?.onKeep(candidate.assetId)}>
                        Use this
                      </Button>
                      <Button type="button" variant="outline" size="md" onClick={generation?.onMake}>
                        Make another
                      </Button>
                    </div>
                  </div>
                  <p className="text-muted-foreground text-xs">
                    Use this to make it your avatar. Your current one stays until then.
                  </p>
                </div>
              )}
              {!candidate && madeAvailable && !making && generation?.available && (
                <div className="flex flex-wrap gap-2">
                  <Button type="button" variant="outline" size="md" onClick={generation.onMake}>
                    Make a new one
                  </Button>
                  <Button type="button" variant="outline" size="md" onClick={generation?.onRemove}>
                    Delete generated avatar
                  </Button>
                </div>
              )}
              {!candidate && madeAvailable && !making && (
                <p className="text-muted-foreground text-xs">
                  Switching to another choice is free. Your generated avatar is kept.
                </p>
              )}
              {!candidate && !madeAvailable && !making && !failed && generation?.available && (
                <div className="space-y-2">
                  <div className="flex items-center gap-3">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={AVATAR_STYLE_SAMPLE_PATH}
                      alt=""
                      className="size-14 shrink-0 rounded-full object-cover"
                    />
                    <Button type="button" size="md" onClick={generation.onMake}>
                      Make an avatar
                    </Button>
                  </div>
                  <p className="text-muted-foreground text-xs">
                    Takes about 20 seconds. Your current picture stays until you pick a new one.
                  </p>
                  <p className="text-muted-foreground text-xs">
                    It uses a fixed style. Nothing about you goes into it.
                  </p>
                </div>
              )}
              <div aria-live="polite">
                {making && (
                  <p className="text-muted-foreground text-sm" role="status">
                    Making your avatar…
                  </p>
                )}
                {failed && (
                  <div className="space-y-2">
                    <p className="text-destructive text-sm">
                      Couldn&apos;t make an avatar just now. Your current one is unchanged.
                    </p>
                    <Button type="button" size="md" onClick={generation?.onMake}>
                      Try again
                    </Button>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
        <div>
          <RadioGroupPrimitive.Item value="initials" className={boxedRowClass}>
            <PersonAvatar
              personId={personId}
              name={name}
              selection={{ kind: "generated", variant: selectedVariant }}
              size="md"
              decorative
            />
            <span className="font-medium">Initials</span>
            <CheckMark />
          </RadioGroupPrimitive.Item>
        </div>
      </RadioGroupPrimitive.Root>
      {/* The colour palette belongs to Initials: it shows only while Initials is the source. */}
      {source === "initials" && (
        <div className="space-y-2">
          <RadioGroupPrimitive.Root
            value={selectedVariant}
            onValueChange={(variant) =>
              onChange({ kind: "generated", variant: variant as GeneratedAvatarVariant })
            }
            aria-label="Colour of your initials"
            className="grid grid-cols-4 gap-2"
          >
            {generatedAvatarPalettes.map((palette) => (
              <RadioGroupPrimitive.Item
                key={palette.id}
                value={palette.id}
                className={swatchClass}
              >
                <PersonAvatar
                  personId={personId}
                  name={name}
                  selection={{ kind: "generated", variant: palette.id }}
                  decorative
                />
                <span className="flex items-center gap-1 text-xs">
                  {palette.label}
                  <RadioGroupPrimitive.Indicator aria-hidden className="text-primary">
                    <Check className="size-3" />
                  </RadioGroupPrimitive.Indicator>
                </span>
              </RadioGroupPrimitive.Item>
            ))}
          </RadioGroupPrimitive.Root>
          <p className="text-muted-foreground text-xs">
            Your avatar keeps its colour when you change your name.
          </p>
        </div>
      )}
      {value?.kind === "sign-in" && !signInAvailable && (
        <p className="text-muted-foreground text-sm" role="status">
          Your sign-in picture is not available. Your generated avatar is shown.
        </p>
      )}
      <p className="text-muted-foreground text-xs">People on this gateway see the picture you choose.</p>
      {pending && (
        <p className="text-muted-foreground text-sm" role="status">
          Saving…
        </p>
      )}
      {error && (
        <p className="text-destructive text-sm" role="alert">
          {error}
        </p>
      )}
    </fieldset>
  );
}
