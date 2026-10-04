export type WorkflowMediaKind = "image" | "audio" | "video" | "file";

export type WorkflowMediaItem = {
  kind: WorkflowMediaKind;
  url: string;
  label: string;
  mediaType?: string;
};

export type WorkflowMedia = WorkflowMediaItem;

export type WorkflowJob = {
  id: string;
  status: string;
  requestId?: string;
  pollPath: string;
  contentPath: string;
  error?: string;
};

export type WorkflowJobRoutes = {
  poll?: { path?: string };
  content?: { path?: string };
};

type MediaRecord = Record<string, unknown>;

const DATA_IMAGE_PNG_PREFIX = "data:image/png;base64,";
const JOB_PATH_PREFIX = "/gateway/v1/media/jobs/";
const VIDEO_PATH_PREFIX = "/v1/videos/";

export function mediaFrom(
  payload: unknown,
  outputKind?: string,
): WorkflowMediaItem[] {
  const record = objectRecord(payload);
  if (!record) return [];

  const fallbackKind = explicitMediaKind(outputKind);
  const items: WorkflowMediaItem[] = [];
  const data = arrayRecords(record.data);
  data.forEach((item, index) => {
    const mediaType =
      stringValue(item.media_type) ?? stringValue(item.mime_type);
    const url = stringValue(item.url) ?? stringValue(item.asset_url);
    const kind =
      explicitMediaKind(stringValue(item.kind)) ??
      mediaKind(mediaType, fallbackKind) ??
      mediaKindFromUrl(url) ??
      "file";
    const label = mediaLabel(kind, index);
    const pushed = pushUrl(items, {
      kind: mediaKindFromUrl(url) ?? kind,
      url,
      label,
      mediaType,
    });

    const b64 = stringValue(item.b64_json);
    if (!pushed && b64) {
      items.push({
        kind: "image",
        url: `${DATA_IMAGE_PNG_PREFIX}${b64}`,
        label: mediaLabel("image", index),
        mediaType: mediaType ?? "image/png",
      });
    }
  });

  const assets = arrayRecords(record.assets);
  assets.forEach((asset, index) => {
    const mediaType =
      stringValue(asset.media_type) ?? stringValue(asset.mime_type);
    const explicitKind = explicitMediaKind(stringValue(asset.kind));
    const url = stringValue(asset.url) ?? stringValue(asset.signed_url);
    const kind =
      explicitKind ??
      mediaKind(mediaType, fallbackKind) ??
      mediaKindFromUrl(url) ??
      "file";
    const label =
      stringValue(asset.label) ??
      stringValue(asset.name) ??
      stringValue(asset.filename) ??
      mediaLabel(kind, index);
    pushUrl(items, {
      kind,
      url,
      label,
      mediaType,
    });
  });

  const topLevelMediaType =
    stringValue(record.media_type) ?? stringValue(record.mime_type);
  const topLevelKind = mediaKind(topLevelMediaType, fallbackKind) ?? "file";
  for (const key of ["url", "audio_url", "image_url", "video_url"] as const) {
    const url = stringValue(record[key]);
    const kind = mediaKindForKey(key) ?? mediaKindFromUrl(url) ?? topLevelKind;
    pushUrl(items, {
      kind,
      url,
      label: mediaLabel(kind, 0),
      mediaType: topLevelMediaType,
    });
  }

  return items;
}

export function workflowJob(
  payload: unknown,
  routes?: WorkflowJobRoutes,
): WorkflowJob | null {
  const record = objectRecord(payload);
  if (!record) return null;
  const object = stringValue(record.object);
  const isMediaJob =
    object === "media.job" || Boolean(stringValue(record.job_id));
  const isDeclaredJob = Boolean(routes && stringValue(record.id));
  const isVideoJob = object === "video" || (isDeclaredJob && !isMediaJob);
  if (!isMediaJob && !isVideoJob) {
    return null;
  }

  const id = stringValue(record.job_id) ?? stringValue(record.id);
  const status = stringValue(record.status);
  if (!id || !status) return null;

  const encodedId = encodeURIComponent(id);
  const pollPath =
    pathFromTemplate(routes?.poll?.path, id) ??
    (isVideoJob
      ? `${VIDEO_PATH_PREFIX}${encodedId}`
      : `${JOB_PATH_PREFIX}${encodedId}`);
  const contentPath =
    pathFromTemplate(routes?.content?.path, id) ??
    (isVideoJob
      ? `${VIDEO_PATH_PREFIX}${encodedId}/content`
      : `${JOB_PATH_PREFIX}${encodedId}/content`);

  return {
    id,
    status,
    requestId: stringValue(record.request_id),
    pollPath,
    contentPath,
    error: errorMessage(record.error),
  };
}

function pushUrl(
  items: WorkflowMediaItem[],
  item: Omit<WorkflowMediaItem, "url"> & { url?: string },
): boolean {
  if (!item.url || !safeMediaUrl(item.url)) return false;
  items.push({
    kind: item.kind,
    url: item.url,
    label: item.label,
    mediaType: item.mediaType,
  });
  return true;
}

function objectRecord(value: unknown): MediaRecord | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as MediaRecord)
    : null;
}

function arrayRecords(value: unknown): MediaRecord[] {
  return Array.isArray(value)
    ? value.filter((item): item is MediaRecord => objectRecord(item) !== null)
    : [];
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function mediaKind(
  value: string | undefined,
  fallback: WorkflowMediaKind | undefined,
): WorkflowMediaKind | undefined {
  const explicit = explicitMediaKind(value);
  if (explicit) return explicit;
  return fallback;
}

function explicitMediaKind(
  value: string | undefined,
): WorkflowMediaKind | undefined {
  if (value?.startsWith("image/") || value === "image") return "image";
  if (value?.startsWith("audio/") || value === "audio") return "audio";
  if (value?.startsWith("video/") || value === "video") return "video";
  if (value === "file") return "file";
  return undefined;
}

function mediaKindFromUrl(
  url: string | undefined,
): WorkflowMediaKind | undefined {
  if (url?.startsWith("data:image/")) return "image";
  if (url?.startsWith("data:audio/")) return "audio";
  if (url?.startsWith("data:video/")) return "video";
  return undefined;
}

function mediaKindForKey(key: string): WorkflowMediaKind | undefined {
  if (key === "image_url") return "image";
  if (key === "audio_url") return "audio";
  if (key === "video_url") return "video";
  return undefined;
}

function mediaLabel(kind: WorkflowMediaKind, index: number) {
  const suffix = index > 0 ? ` ${index + 1}` : "";
  if (kind === "file") return `Download${suffix}`;
  return `${kind[0].toUpperCase()}${kind.slice(1)}${suffix}`;
}

function safeMediaUrl(url: string) {
  if (url.startsWith("/")) return !url.startsWith("//");
  if (url.startsWith(DATA_IMAGE_PNG_PREFIX)) return true;

  try {
    const parsed = new URL(url);
    return ["http:", "https:", "blob:"].includes(parsed.protocol);
  } catch {
    return false;
  }
}

function errorMessage(error: unknown): string | undefined {
  if (typeof error === "string" && error.trim()) return error;
  const record = objectRecord(error);
  return record ? stringValue(record.message) : undefined;
}

function pathFromTemplate(template: string | undefined, id: string) {
  if (!template) return undefined;
  const encodedId = encodeURIComponent(id);
  const path = template
    .replaceAll("{video_id}", encodedId)
    .replaceAll("{job_id}", encodedId);
  if (!isCanonicalJobPath(path, encodedId)) return undefined;
  return path;
}

function isCanonicalJobPath(path: string, encodedId: string) {
  const gatewayBase = `${JOB_PATH_PREFIX}${encodedId}`;
  const videoBase = `${VIDEO_PATH_PREFIX}${encodedId}`;
  return (
    path === gatewayBase ||
    path === `${gatewayBase}/content` ||
    path === videoBase ||
    path === `${videoBase}/content`
  );
}
