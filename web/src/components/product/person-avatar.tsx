"use client";

import * as React from "react";
import { Avatar as AvatarPrimitive } from "radix-ui";
import { cn } from "@/lib/cn";

/**
 * The household's avatars: a generated disc with the person's initial, or the
 * picture the person's sign-in provider shared. Everything here is drawn from
 * the stable person id, never the display name, so a rename keeps the colour.
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

/** What the gateway stores as `avatar_choice`. */
export type AvatarChoice = "auto" | "picture" | GeneratedAvatarVariant;

export type AvatarSelection =
  | { kind: "auto" }
  | { kind: "generated"; variant: GeneratedAvatarVariant }
  | { kind: "sign-in" };

export function selectionFromChoice(choice: string | null | undefined): AvatarSelection {
  if (choice === "picture") return { kind: "sign-in" };
  const palette = generatedAvatarPalettes.find((entry) => entry.id === choice);
  return palette
    ? { kind: "generated", variant: palette.id }
    : { kind: "auto" };
}

export function choiceFromSelection(selection: AvatarSelection): AvatarChoice {
  if (selection.kind === "sign-in") return "picture";
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

/** The one place the gateway serves a sign-in picture from (gateway/me_avatar.py). */
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
  /** The gateway path of the picture. It loads only after an explicit sign-in selection. */
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
  const imageUrl =
    selection?.kind === "sign-in" ? getAvatarPictureUrl(pictureUrl) : undefined;
  const hidden = decorative && !label;
  const initials = getAvatarInitials(name);

  return (
    <AvatarPrimitive.Root
      data-slot={slot}
      data-avatar-selection={imageUrl ? "sign-in" : "generated"}
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

export type AvatarPickerProps = {
  personId: string;
  name: string;
  value?: AvatarSelection | null;
  pictureUrl?: string | null;
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
  pictureUrl,
  onChange,
  disabled = false,
  pending = false,
  error,
  className,
}: AvatarPickerProps) {
  const groupId = React.useId();
  const pictureAvailable = Boolean(getAvatarPictureUrl(pictureUrl));
  const pictureSelected = value?.kind === "sign-in" && pictureAvailable;
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
              checked={!pictureSelected && selectedVariant === palette.id}
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
      {pictureAvailable && (
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
              pictureUrl={pictureUrl}
              size="sm"
              decorative
            />
            Use my sign-in picture
          </span>
        </label>
      )}
      {pictureAvailable && (
        <p className="text-muted-foreground text-xs">
          Azizam gets this picture from your sign-in provider. Only you and the
          people who run this gateway can see it.
        </p>
      )}
      {value?.kind === "sign-in" && !pictureAvailable && (
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
