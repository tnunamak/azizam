import { describe, expect, it } from "vitest";
import { assignHandoffFiles, hasRequiredHandoffFiles } from "./handoff-files";
import { MESH_FILE_SLOTS } from "./mesh-mode";
import { TRANSCRIBE_FILE_SLOTS } from "./transcribe-mode";

const png = new File(["p"], "fox.png", { type: "image/png" });
const wav = new File(["w"], "voice.wav", { type: "audio/wav" });

describe("assignHandoffFiles", () => {
  it("gives a file to the required input of its media family first", () => {
    const slots = [
      { inputName: "mask", required: false, family: "image" },
      { inputName: "source", required: true, family: "image" },
    ];
    expect(assignHandoffFiles([png], slots)).toEqual({ source: png });
  });

  it("matches each file to the input of its own family", () => {
    const slots = [
      { inputName: "voice", required: true, family: "audio" },
      { inputName: "photo", required: true, family: "image" },
    ];
    expect(assignHandoffFiles([png, wav], slots)).toEqual({ photo: png, voice: wav });
  });

  it("lets an input that declares no family take any file, and drops a file nothing takes", () => {
    expect(assignHandoffFiles([wav], [{ inputName: "any", required: true, family: "" }])).toEqual({ any: wav });
    expect(assignHandoffFiles([wav], [{ inputName: "photo", required: true, family: "image" }])).toEqual({});
  });
});

describe("the slots a panel declares", () => {
  it("Photo to 3D takes a picture and refuses a recording", () => {
    expect(assignHandoffFiles([png], MESH_FILE_SLOTS)).toEqual({ image: png });
    expect(assignHandoffFiles([wav], MESH_FILE_SLOTS)).toEqual({});
  });

  it("Transcribe takes a recording and refuses a picture", () => {
    expect(assignHandoffFiles([wav], TRANSCRIBE_FILE_SLOTS)).toEqual({ audio: wav });
    expect(assignHandoffFiles([png], TRANSCRIBE_FILE_SLOTS)).toEqual({});
  });
});

describe("Home files bound by workflow input name", () => {
  // toEqual treats any two File objects as equal, so compare which file (by name) each input got.
  const names = (chosen: Record<string, File | null>) =>
    Object.fromEntries(Object.entries(chosen).map(([input, file]) => [input, file?.name ?? null]));
  const ending = new File(["e"], "end.png", { type: "image/png" });
  const video = new File(["v"], "source.mp4", { type: "video/mp4" });
  const keyframes = [
    { inputName: "image", required: true, family: "image" },
    { inputName: "last_frame", required: true, family: "image" },
  ];

  it("binds reversed keyframes to their declared start and end inputs", () => {
    expect(names(assignHandoffFiles([ending, png], keyframes, ["last_frame", "image"])))
      .toEqual({ image: png.name, last_frame: ending.name });
  });

  it("binds scail picture and video by name, with either list order", () => {
    const slots = [
      { inputName: "reference_image", required: true, family: "image" },
      { inputName: "source_video", required: true, family: "video" },
    ];
    expect(names(assignHandoffFiles([video, png], slots, ["source_video", "reference_image"])))
      .toEqual({ reference_image: png.name, source_video: video.name });
    expect(names(assignHandoffFiles([png, video], slots, ["reference_image", "source_video"])))
      .toEqual({ reference_image: png.name, source_video: video.name });
  });

  it("falls back to required-first family matching for an unknown name", () => {
    const slots = [
      { inputName: "optional", required: false, family: "image" },
      { inputName: "required", required: true, family: "image" },
    ];
    expect(names(assignHandoffFiles([png], slots, ["missing"]))).toEqual({ required: png.name });
  });

  it("keeps the original assignment exactly when file_inputs is absent", () => {
    const slots = [
      { inputName: "optional", required: false, family: "image" },
      { inputName: "voice", required: true, family: "audio" },
      { inputName: "first", required: true, family: "image" },
      { inputName: "second", required: true, family: "image" },
    ];
    expect(names(assignHandoffFiles([ending, wav, png], slots)))
      .toEqual({ first: ending.name, voice: wav.name, second: png.name });
  });

  it("reserves named inputs before an earlier unnamed file uses the fallback", () => {
    expect(names(assignHandoffFiles([ending, png], keyframes, ["", "image"])))
      .toEqual({ image: png.name, last_frame: ending.name });
  });

  it("never puts a named file in an incompatible family or overwrites its slot", () => {
    const slots = [
      ...keyframes,
      { inputName: "source_video", required: true, family: "video" },
    ];
    expect(names(assignHandoffFiles([video, png, ending], slots, ["image", "last_frame", "last_frame"])))
      .toEqual({ last_frame: png.name });
    expect(names(assignHandoffFiles([video], keyframes, ["missing"]))).toEqual({});
  });

  it("waits for both required files, ignores unrelated files, and does not require optional files", () => {
    const slots = [...keyframes, { inputName: "mask", required: false, family: "image" }];
    expect(hasRequiredHandoffFiles(slots, { image: png, unrelated: ending })).toBe(false);
    expect(hasRequiredHandoffFiles(slots, { image: png, last_frame: null })).toBe(false);
    expect(hasRequiredHandoffFiles(slots, { image: png, last_frame: ending })).toBe(true);
    expect(hasRequiredHandoffFiles([], {})).toBe(true);
    expect(hasRequiredHandoffFiles([{ inputName: "mask", required: false }], {})).toBe(true);
  });
});
