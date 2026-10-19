import {
  PersonAvatar,
  selectionFromChoice,
  type PersonAvatarSize,
} from "@/components/product/person-avatar";

export type PersonAvatarState = {
  choice?: string | null;
  picture_url?: string | null;
};

/**
 * The household's visual unit: a disc with the person's initial, or their
 * sign-in picture when they chose it. The colour comes from the stable person
 * id (`personId`), so a rename keeps it; callers that only have a name pass no
 * id and get a colour from the name. Decorative by default; pass `label` when
 * the mark is the only name shown.
 */
export function PersonMark({
  name,
  personId,
  avatar,
  size = "sm",
  label,
  className,
}: {
  name: string;
  personId?: string | null;
  avatar?: PersonAvatarState | null;
  size?: Exclude<PersonAvatarSize, "xl">;
  label?: string;
  className?: string;
}) {
  return (
    <PersonAvatar
      slot="person-mark"
      personId={personId}
      name={name}
      selection={selectionFromChoice(avatar?.choice)}
      pictureUrl={avatar?.picture_url}
      size={size}
      label={label}
      decorative={!label}
      className={className}
    />
  );
}
