/**
 * The member Library as a media input sees it: the list route, the download route that answers with a
 * short-lived signed link, and the signed link that serves the bytes. A spec passes one item and names the
 * routes it handles itself in `otherwise`.
 */
export const PNG_BYTES = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4];
export const WAV_BYTES = [0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0, 0x57, 0x41, 0x56, 0x45];

export type StubbedItem = {
  assetId: string;
  kind: string;
  mediaType: string;
  bytes: number[];
  prompt: string;
  /** The Library's length for the item, in seconds. */
  seconds?: number;
  /** When the item was made, in unix seconds. */
  createdAt?: number;
};

export const PICTURE_ITEM: StubbedItem = {
  assetId: `img_${"a".repeat(64)}`,
  kind: "image",
  mediaType: "image/png",
  bytes: PNG_BYTES,
  prompt: "A teal fish",
};

export const RECORDING_ITEM: StubbedItem = {
  assetId: `aud_${"b".repeat(64)}`,
  kind: "audio",
  mediaType: "audio/wav",
  bytes: WAV_BYTES,
  prompt: "A short spoken note",
};

export function stubbedLibrary(
  items: StubbedItem[],
  otherwise: (url: string, init?: RequestInit) => Response | Promise<Response> = () =>
    Response.json({}, { status: 404 }),
) {
  const signed = (item: StubbedItem) => `http://gateway.test/v1/assets/signed-${item.assetId}`;
  const listed: string[] = [];
  const downloaded: string[] = [];
  const impl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    if (url.startsWith("/me/api/media?")) {
      listed.push(url);
      const kind = new URL(url, "http://gateway.test").searchParams.get("kind");
      return Response.json({
        assets: items
          .filter((item) => !kind || item.kind === kind)
          .map((item) => ({
            asset_id: item.assetId,
            kind: item.kind,
            media_type: item.mediaType,
            prompt: item.prompt,
            ...(item.seconds ? { duration_s: item.seconds } : {}),
            ...(item.createdAt ? { created_at: item.createdAt } : {}),
            available: true,
            thumbnail_url: signed(item),
            download_url: signed(item),
          })),
      });
    }
    for (const item of items) {
      if (url.startsWith(`/me/api/media/${item.assetId}/download`)) {
        return Response.json({ asset_id: item.assetId, download_url: signed(item) });
      }
      if (url === signed(item)) {
        downloaded.push(item.assetId);
        return new Response(new Uint8Array(item.bytes), { headers: { "content-type": item.mediaType } });
      }
    }
    return otherwise(url, init);
  };
  return { impl, listed, downloaded };
}
