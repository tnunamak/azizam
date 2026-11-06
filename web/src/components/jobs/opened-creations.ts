/** Session-scoped acknowledgement shared by every route into the viewer. */
export const CREATION_OPENED = "azizam:creation-opened";
const key = (personId: string) => `azizam:opened-creations:v1:${personId}`;

export function openedCreations(personId: string): Set<string> {
  try {
    return new Set(JSON.parse(sessionStorage.getItem(key(personId)) ?? "[]"));
  } catch {
    return new Set();
  }
}

export function markCreationOpened(personId: string, assetId: string) {
  const opened = openedCreations(personId);
  opened.add(assetId);
  try {
    sessionStorage.setItem(key(personId), JSON.stringify([...opened]));
  } catch {
    // Notices still receive the event when browser storage is disabled.
  }
  window.dispatchEvent(
    new CustomEvent(CREATION_OPENED, { detail: { personId, assetId } }),
  );
}
