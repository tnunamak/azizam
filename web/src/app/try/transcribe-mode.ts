import type { HandoffFileSlot } from "./handoff-files";

/** The file input the transcription form declares, for the files Home hands to Try (see assignHandoffFiles). */
export const TRANSCRIBE_FILE_SLOTS: HandoffFileSlot[] = [{ inputName: "audio", required: true, family: "audio" }];
