import { describe, expect, it } from "vitest";
import { assignHandoffFiles } from "./handoff-files";
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
