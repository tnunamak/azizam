"use client";

import * as React from "react";
import { Avatar as AvatarPrimitive } from "radix-ui";
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
 * The make-my-avatar flow the picker shows. The caller owns the job, the results and the saving; the picker
 * only draws the state it is given.
 */
export type AvatarGenerationView = {
  /** This gateway has an image route to make a picture with, so the button is offered. */
  available: boolean;
  /** A picture is being made. The palette initial stays until one is kept. */
  making: boolean;
  /** Pictures from the last attempt, ready to choose from. */
  results: { assetId: string; src: string }[];
  /** The last attempt did not make a picture. */
  failed: boolean;
  /** Start an attempt. After results, this is "Try another". */
  onMake: () => void;
  /** Keep one of the results as the person's avatar. */
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

const choiceClass =
  "border-border hover:bg-accent peer-checked:border-primary peer-checked:ring-primary peer-focus-visible:outline-ring peer-disabled:pointer-events-none peer-disabled:opacity-50 cursor-pointer rounded-lg border peer-checked:ring-2 peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2";

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
  const groupId = React.useId();
  const signInAvailable = Boolean(getAvatarPictureUrl(signInPictureUrl));
  const madeAvailable = Boolean(getAvatarPictureUrl(madePictureUrl));
  // The sign-in picture is the default, so it is the checked choice unless the person picked another.
  const pictureSelected =
    signInAvailable && value?.kind !== "generated" && value?.kind !== "made";
  const madeSelected = madeAvailable && value?.kind === "made";
  const selectedVariant = getGeneratedAvatar(
    personId,
    value?.kind === "generated" ? value.variant : undefined,
  ).id;

  return (
    <fieldset
      className={cn("min-w-0 space-y-3", className)}
      disabled={disabled || pending}
      aria-busy={pending}
      aria-describedby={`${groupId}-help`}
    >
      {/* The dialog names the task; the group still needs a name for a screen reader. */}
      <legend className="sr-only">Choose your avatar</legend>
      <p id={`${groupId}-help`} className="text-muted-foreground text-sm">
        Your avatar keeps its colour when you change your name.
      </p>
      <div className="grid grid-cols-4 gap-2">
        {generatedAvatarPalettes.map((palette) => (
          <label key={palette.id} className="relative">
            <input
              className="peer sr-only"
              type="radio"
              name={groupId}
              value={palette.id}
              checked={!pictureSelected && !madeSelected && selectedVariant === palette.id}
              onChange={() =>
                onChange({ kind: "generated", variant: palette.id })
              }
            />
            <span
              className={cn(
                choiceClass,
                "flex min-h-16 flex-col items-center justify-center gap-1 p-2",
              )}
            >
              <PersonAvatar
                personId={personId}
                name={name}
                selection={{ kind: "generated", variant: palette.id }}
                decorative
              />
              <span className="text-xs">{palette.label}</span>
            </span>
          </label>
        ))}
      </div>
      {signInAvailable && (
        <label className="relative block">
          <input
            className="peer sr-only"
            type="radio"
            name={groupId}
            value="sign-in"
            checked={pictureSelected}
            onChange={() => onChange({ kind: "sign-in" })}
          />
          <span
            className={cn(
              choiceClass,
              "flex min-h-11 items-center gap-3 px-3 py-2 text-sm",
            )}
          >
            <PersonAvatar
              personId={personId}
              name={name}
              selection={{ kind: "sign-in" }}
              pictureUrl={signInPictureUrl}
              size="sm"
              decorative
            />
            Use my sign-in picture
          </span>
        </label>
      )}
      {madeAvailable && (
        <label className="relative block">
          <input
            className="peer sr-only"
            type="radio"
            name={groupId}
            value="generated"
            checked={madeSelected}
            onChange={() => onChange({ kind: "made" })}
          />
          <span
            className={cn(
              choiceClass,
              "flex min-h-11 items-center gap-3 px-3 py-2 text-sm",
            )}
          >
            <PersonAvatar
              personId={personId}
              name={name}
              selection={{ kind: "made" }}
              pictureUrl={madePictureUrl}
              size="sm"
              decorative
            />
            Your generated avatar
          </span>
        </label>
      )}
      {generation && (generation.available || madeAvailable) && (
        <div className="border-border space-y-3 border-t pt-3">
          {generation.results.length > 0 && (
            <ul aria-label="Pictures you can use" className="grid grid-cols-2 gap-2">
              {generation.results.map((result, index) => (
                <li
                  key={result.assetId}
                  className="border-border flex flex-col items-center gap-2 rounded-lg border p-2"
                >
                  {/* A signed link to a picture the person made, drawn as the Library draws its own. */}
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={result.src}
                    alt={`New avatar ${index + 1}`}
                    className="size-16 rounded-full object-cover"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => generation.onKeep(result.assetId)}
                  >
                    Use this picture
                  </Button>
                </li>
              ))}
            </ul>
          )}
          {generation.making && (
            <p className="text-muted-foreground text-sm" role="status">
              Making your avatar. Your current avatar stays until it is ready.
            </p>
          )}
          {generation.failed && !generation.making && (
            <p className="text-destructive text-sm" role="alert">
              We couldn&apos;t make your avatar. Try again.
            </p>
          )}
          {generation.available && !generation.making && (
            <p className="text-muted-foreground text-xs">
              Azizam makes it from a fixed style. Nothing about you goes into it.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {generation.available && !generation.making && (
              <Button type="button" variant="outline" size="md" onClick={generation.onMake}>
                {generation.results.length > 0 ? "Try another" : "Make me an avatar"}
              </Button>
            )}
            {madeAvailable && (
              <Button type="button" variant="outline" size="md" onClick={generation.onRemove}>
                Remove generated avatar
              </Button>
            )}
          </div>
        </div>
      )}
      {signInAvailable && (
        <p className="text-muted-foreground text-xs">
          Azizam gets this picture from your sign-in provider. Only you and the
          people who run this gateway can see it.
        </p>
      )}
      {value?.kind === "sign-in" && !signInAvailable && (
        <p className="text-muted-foreground text-sm" role="status">
          Your sign-in picture is not available. Your generated avatar is shown.
        </p>
      )}
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
