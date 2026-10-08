import { describe, expect, it } from "vitest";

import { mediaFrom, workflowJob } from "@/app/try/workflow-media";

describe("workflow media helpers", () => {
  it("extracts OpenAI image urls and base64 images", () => {
    expect(
      mediaFrom({
        data: [
          { url: "https://example.test/image.png", media_type: "image/png" },
          { b64_json: "abc123" },
        ],
      }),
    ).toEqual([
      {
        kind: "image",
        url: "https://example.test/image.png",
        label: "Image",
        mediaType: "image/png",
      },
      {
        kind: "image",
        url: "data:image/png;base64,abc123",
        label: "Image 2",
        mediaType: "image/png",
      },
    ]);
  });

  it("uses asset_url and prefers a safe url over duplicate b64 data", () => {
    expect(
      mediaFrom({
        data: [
          {
            asset_url: "/gateway/v1/media/assets/img_1",
            b64_json: "duplicate",
            kind: "image",
          },
        ],
      }),
    ).toEqual([
      {
        kind: "image",
        url: "/gateway/v1/media/assets/img_1",
        label: "Image",
      },
    ]);
  });

  it("accepts string output kinds and item kinds", () => {
    expect(
      mediaFrom(
        {
          data: [
            { url: "https://example.test/audio" },
            { url: "https://example.test/preview", kind: "video" },
          ],
        },
        "audio",
      ),
    ).toEqual([
      {
        kind: "audio",
        url: "https://example.test/audio",
        label: "Audio",
      },
      {
        kind: "video",
        url: "https://example.test/preview",
        label: "Video 2",
      },
    ]);
  });

  it("infers images from safe image data urls without media_type", () => {
    expect(
      mediaFrom({
        data: [{ url: "data:image/png;base64,abc123" }],
      }),
    ).toEqual([
      {
        kind: "image",
        url: "data:image/png;base64,abc123",
        label: "Image",
      },
    ]);
  });

  it("extracts audio, video, and multiple signed assets", () => {
    expect(
      mediaFrom({
        assets: [
          {
            signed_url: "/gateway/v1/media/jobs/job_1/content?asset=a",
            media_type: "audio/mpeg",
          },
          {
            url: "https://example.test/video.mp4",
            media_type: "video/mp4",
            label: "Preview clip",
          },
        ],
      }),
    ).toEqual([
      {
        kind: "audio",
        url: "/gateway/v1/media/jobs/job_1/content?asset=a",
        label: "Audio",
        mediaType: "audio/mpeg",
      },
      {
        kind: "video",
        url: "https://example.test/video.mp4",
        label: "Preview clip",
        mediaType: "video/mp4",
      },
    ]);
  });

  it("keeps unknown outputs as downloads", () => {
    expect(
      mediaFrom({
        assets: [
          {
            url: "/downloads/result.bin",
            media_type: "application/octet-stream",
          },
        ],
      }),
    ).toEqual([
      {
        kind: "file",
        url: "/downloads/result.bin",
        label: "Download",
        mediaType: "application/octet-stream",
      },
    ]);
  });

  it("extracts real top-level media urls and rejects unsafe protocols", () => {
    expect(
      mediaFrom({
        url: "javascript:alert(1)",
        image_url: "data:text/html;base64,PHNjcmlwdA==",
        audio_url: "blob:https://example.test/audio",
        video_url: "//evil.test/video.mp4",
      }),
    ).toEqual([
      {
        kind: "audio",
        url: "blob:https://example.test/audio",
        label: "Audio",
      },
    ]);
  });

  it("builds canonical same-origin media job paths from job ids", () => {
    expect(
      workflowJob({
        object: "media.job",
        id: "ignored",
        job_id: "job/a b",
        status: "queued",
        request_id: "req_123",
        poll_url: "https://signed.example.test/jobs/job",
        content_url: "https://signed.example.test/content",
      }),
    ).toEqual({
      id: "job/a b",
      status: "queued",
      requestId: "req_123",
      pollPath: "/gateway/v1/media/jobs/job%2Fa%20b",
      contentPath: "/gateway/v1/media/jobs/job%2Fa%20b/content",
      error: undefined,
    });
  });

  it("returns media jobs for terminal statuses and error payloads", () => {
    expect(
      workflowJob({
        object: "media.job",
        job_id: "job_done",
        status: "failed",
        error: { message: "backend failed" },
      }),
    ).toMatchObject({
      id: "job_done",
      status: "failed",
      error: "backend failed",
    });

    expect(
      workflowJob({
        object: "media.job",
        job_id: "job_text_error",
        status: "failed",
        error: "plain failure",
      }),
    ).toMatchObject({
      id: "job_text_error",
      status: "failed",
      error: "plain failure",
    });
  });

  it("builds declared canonical video job paths", () => {
    expect(
      workflowJob(
        {
          object: "video",
          id: "vid/a b",
          status: "completed",
          request_id: "req_video",
        },
        {
          poll: { path: "/v1/videos/{video_id}" },
          content: { path: "/v1/videos/{video_id}/content" },
        },
      ),
    ).toEqual({
      id: "vid/a b",
      status: "completed",
      requestId: "req_video",
      pollPath: "/v1/videos/vid%2Fa%20b",
      contentPath: "/v1/videos/vid%2Fa%20b/content",
      error: undefined,
    });
  });

  it("uses declared routes for failed video jobs without engine-specific checks", () => {
    expect(
      workflowJob(
        {
          id: "vid_failed",
          status: "failed",
          error: { message: "render failed" },
        },
        {
          poll: { path: "/v1/videos/{video_id}" },
          content: { path: "/v1/videos/{video_id}/content" },
        },
      ),
    ).toMatchObject({
      id: "vid_failed",
      status: "failed",
      pollPath: "/v1/videos/vid_failed",
      contentPath: "/v1/videos/vid_failed/content",
      error: "render failed",
    });
  });

  it("rejects unsafe declared job routes and falls back to canonical same-origin paths", () => {
    expect(
      workflowJob(
        { object: "video", id: "vid_safe", status: "queued" },
        {
          poll: { path: "https://example.test/v1/videos/{video_id}" },
          content: { path: "/openai/v1/videos/{video_id}/content" },
        },
      ),
    ).toMatchObject({
      pollPath: "/v1/videos/vid_safe",
      contentPath: "/v1/videos/vid_safe/content",
    });
  });

  it("ignores non-job payloads", () => {
    expect(workflowJob({ id: "not-enough", status: "queued" })).toBeNull();
    expect(workflowJob({ object: "media.job", status: "queued" })).toBeNull();
  });

  it.each([
    ["audio", "audio/mpeg"],
    ["video", "video/mp4"],
  ])("preserves inline base64 %s across media payload shapes", (kind, mediaType) => {
    const url = `data:${mediaType};base64,YWJj`;
    const label = kind === "audio" ? "Audio" : "Video";
    for (const payload of [
      { data: [{ url }] },
      { assets: [{ url, media_type: mediaType }] },
      { [`${kind}_url`]: url },
    ]) {
      expect(mediaFrom(payload)).toMatchObject([{ kind, url, label }]);
    }
    expect(
      mediaFrom({
        data: [
          { url: `data:${mediaType},abc` },
          { url: "data:text/html;base64,YWJj" },
          { url: "data:image/svg+xml;base64,YWJj" },
          { url: "javascript:alert(1)" },
          { url: "//evil.test/media" },
        ],
      }),
    ).toEqual([]);
  });
});
