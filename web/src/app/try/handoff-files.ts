/** A file input a workflow offers: its name, whether a run needs it, and the media type family it declares ("" for any). */
export interface HandoffFileSlot {
  inputName: string;
  required: boolean;
  family: string;
}

/**
 * Names bind before fallback files, so an unnamed file cannot steal a named input.
 * Unknown or absent names keep the original required-first, family-matching order.
 */
export function assignHandoffFiles(
  files: File[],
  slots: HandoffFileSlot[],
  fileInputs?: string[],
): Record<string, File | null> {
  const chosen: Record<string, File | null> = {};
  const ordered = [...slots].sort((a, b) => Number(b.required) - Number(a.required));
  const fits = (file: File, slot: HandoffFileSlot) =>
    !slot.family || slot.family === file.type.split("/")[0];
  const fallback: File[] = [];
  files.forEach((file, index) => {
    const name = fileInputs?.[index];
    const slot = name ? slots.find((item) => item.inputName === name) : undefined;
    if (!slot) {
      fallback.push(file);
    } else if (!chosen[slot.inputName] && fits(file, slot)) {
      chosen[slot.inputName] = file;
    }
    // A known name with the wrong family (or a duplicate) cannot move elsewhere.
  });
  for (const file of fallback) {
    const slot = ordered.find((item) => !chosen[item.inputName] && fits(file, item));
    if (slot) chosen[slot.inputName] = file;
  }
  return chosen;
}

/** A partial hand-off waits in the form until every required input has a file. */
export function hasRequiredHandoffFiles(
  slots: readonly { inputName: string; required?: boolean }[],
  chosen: Readonly<Record<string, File | null | undefined>>,
): boolean {
  return slots.every((slot) => !slot.required || Boolean(chosen[slot.inputName]));
}

/**
 * A Try panel that takes files on a form of its own, not the workflow form: the file slots it declares, and whether it
 * starts the run itself once it has its files (a panel that does keeps the hand-off from submitting it a second time).
 */
export interface PanelFiles {
  slots: HandoffFileSlot[];
  startsItself?: boolean;
}
