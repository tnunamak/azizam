/** Join class names, dropping falsy values. Conditional-class helper for the
 * ui components; intentionally not a tailwind-merge — components keep their
 * own utility sets conflict-free and callers append layout-only classes. */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
