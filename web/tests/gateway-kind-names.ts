/**
 * The kind words the gateway sends with every Library listing (gateway/asset_types.py KIND_NAMES, as
 * `kind_names()` returns them). The screens keep no table of their own, so specs start from this copy of the
 * gateway's answer, the way a signed-in screen starts from the first listing.
 */
export const GATEWAY_KIND_NAMES = [
  { kind: "image", label: "Image", plural: "Images", untitled: "Picture", input: "picture" },
  { kind: "video", label: "Video", plural: "Video", untitled: "Video", input: "clip" },
  { kind: "audio", label: "Audio", plural: "Audio", untitled: "Audio", input: "recording" },
  { kind: "model", label: "3D model", plural: "3D models", untitled: "3D model", input: "3D model" },
  { kind: "text", label: "Text", plural: "Text", untitled: "Text file", input: "text file" },
  { kind: "file", label: "File", plural: "Files", untitled: "File", input: "file" },
];

/** The gateway's `kinds` list for the named kinds, as a listing answer carries it. A kind with no row is named from its type. */
export function kindRows(...kinds: string[]) {
  return kinds.map(
    (kind) =>
      GATEWAY_KIND_NAMES.find((row) => row.kind === kind) ?? {
        kind,
        label: kind[0].toUpperCase() + kind.slice(1),
        plural: kind[0].toUpperCase() + kind.slice(1),
        untitled: kind[0].toUpperCase() + kind.slice(1),
        input: kind,
      },
  );
}
