// A llama.cpp engine reports the GGUF file it loaded, so a model id can be a
// directory on the gateway host. No screen shows it outside an owner's
// technical details.
export const GGUF =
  "/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf";
export const HOST_PATH =
  /\/media\/windows|AI Models|\/GGUF\/|\/home\/[a-z]+\//;

/** Text a person can read now: closed disclosures keep only their summary. */
export function readableText(root: HTMLElement = document.body): string {
  const clone = root.cloneNode(true) as HTMLElement;
  clone.querySelectorAll("details:not([open])").forEach((details) => {
    [...details.children].forEach((child) => {
      if (child.tagName !== "SUMMARY") child.remove();
    });
  });
  return clone.textContent ?? "";
}

/** Titles and aria labels are on-demand text too. */
export function attributeText(root: HTMLElement = document.body): string {
  return [...root.querySelectorAll("[title],[aria-label]")]
    .map(
      (node) =>
        `${node.getAttribute("title") ?? ""} ${node.getAttribute("aria-label") ?? ""}`,
    )
    .join("\n");
}

export function expectNoHostPath(
  expectFn: typeof import("vitest").expect,
  where: string,
) {
  expectFn(readableText(), `${where}: readable text`).not.toMatch(HOST_PATH);
  expectFn(attributeText(), `${where}: title and aria-label`).not.toMatch(
    HOST_PATH,
  );
}
