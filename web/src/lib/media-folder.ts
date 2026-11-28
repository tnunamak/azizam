// The media folder's confirmation state and the plain words about it, shared by the owner's Remove missing
// dialog (Library) and the owner's Media folder action (Settings). Only the owner receives where the folder
// is and what it holds; every number here is about the whole Library unless the sentence says "your".
export interface MediaFolderView {
  state: "confirmed" | "unconfirmed" | "mismatch";
  path?: string;
  library_items?: number;
  items_here?: number;
  uncheckable_items?: number;
}

export function checkedItems(count: number) {
  return `${count} checked ${count === 1 ? "item" : "items"}`;
}

// The owner has something to confirm only when the folder is not confirmed and says what it holds.
export function folderToConfirm(folder: MediaFolderView | null | undefined) {
  return folder && folder.state !== "confirmed" ? folder : null;
}

export function folderChangedSentence(path: string) {
  return `The media folder looks different from last time (${path}). If you moved your media or changed disks, confirm this is the right folder before removing anything.`;
}

// The whole-Library evidence: how many checked items have no file in the folder, and when to go ahead.
export function libraryEvidenceSentence(path: string, checked: number, here: number) {
  const missing = Math.max(checked - here, 0);
  if (missing === 0) return null;
  return `Across the whole Library, ${missing} of ${checkedItems(checked)} ${missing === 1 ? "is" : "are"} missing from ${path}, so only do this if your media disk is connected and ${path} is where your photos, videos and audio are kept.`;
}

export function noneHereSentence(path: string, checked: number) {
  return `Azizam can't find any of the Library's ${checkedItems(checked)} in ${path}. Check that your media disk is connected.`;
}
