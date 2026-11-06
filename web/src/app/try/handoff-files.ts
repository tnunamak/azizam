/** A file input a workflow offers: its name, whether a run needs it, and the media type family it declares ("" for any). */
export interface HandoffFileSlot {
  inputName: string;
  required: boolean;
  family: string;
}

/**
 * Sets the files Home sent on a workflow's file inputs. Each file takes the first input still empty whose media type
 * family it matches (an input that declares none takes any file), required inputs first.
 */
export function assignHandoffFiles(files: File[], slots: HandoffFileSlot[]): Record<string, File | null> {
  const chosen: Record<string, File | null> = {};
  const ordered = [...slots].sort((a, b) => Number(b.required) - Number(a.required));
  for (const file of files) {
    const family = file.type.split("/")[0];
    const slot = ordered.find((item) => !chosen[item.inputName] && (!item.family || item.family === family));
    if (slot) chosen[slot.inputName] = file;
  }
  return chosen;
}

/**
 * A Try panel that takes files on a form of its own, not the workflow form: the file slots it declares, and whether it
 * starts the run itself once it has its files (a panel that does keeps the hand-off from submitting it a second time).
 */
export interface PanelFiles {
  slots: HandoffFileSlot[];
  startsItself?: boolean;
}
