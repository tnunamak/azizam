/**
 * A host path in free text: a folder under a home root (home, Users) or a mount root (mnt, media, srv, opt).
 * It starts at a text boundary, so a path inside a URL ("https://example.com/home/alex") is never one: there the
 * slash follows the host's last letter.
 */
const HOST_PATH = /(?<![\w:/.~-])\/(?:home|Users|mnt|media|srv|opt)(?:\/[^\s"'`<>()[\]{},;]+)?/g;

/**
 * The text with every host path cut to its file name: "/srv/models/GGUF/x.gguf" reads "x.gguf". A path too short
 * to have a file name below its owner's folder ("/home/alex", "/mnt/disk") reads "[folder]", so no user or disk
 * name stays.
 */
export function hideHostPaths(text: string): string {
  return text.replace(HOST_PATH, (match) => {
    // A sentence's last mark ("on /mnt/disk2.") is not part of the path.
    const [, path, tail] = /^(.*?)([.!?:]*)$/.exec(match) ?? [match, match, ""];
    const parts = path.split("/").filter(Boolean);
    return (parts.length > 2 ? parts[parts.length - 1] : "[folder]") + tail;
  });
}
