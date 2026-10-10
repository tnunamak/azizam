import { expect, test } from "@playwright/test";

// Every workflow manifest that takes a picture or video, through the
// gateway's real catalog (no mocks): each media input gets exactly one picker
// on Try, and the form shows no made-up file path and no raw field name.
// The 2026-10-09 sweep found Flux2 Klein 4B Edit's pictures rendered as text
// boxes plus a second upload control reading "./source-image.bin".

type CatalogInput = {
  name: string;
  type: string;
  asset?: boolean;
  media_type?: string;
  ignored?: boolean;
  accepted?: boolean;
};
type CatalogWorkflow = { id: string; title: string; inputs: CatalogInput[] };

const isMedia = (input: CatalogInput) =>
  (input.asset === true || input.type === "asset") &&
  !input.ignored &&
  input.accepted !== false;

test("every picture and video input on Try is one picker, in plain words", async ({
  page,
}) => {
  const response = await page.request.get("/me/api/workflows");
  expect(response.ok()).toBe(true);
  const { workflows } = (await response.json()) as {
    workflows: CatalogWorkflow[];
  };
  const withMedia = workflows.filter((workflow) =>
    workflow.inputs.some(isMedia),
  );
  // image.edit (5 Qwen/Flux edit and inpaint manifests, SeedVR2, Identity),
  // face likeness, image-to-video and the video edits.
  expect(withMedia.length).toBeGreaterThanOrEqual(13);

  for (const workflow of withMedia) {
    await test.step(workflow.id, async () => {
      await page.goto(`/ui/try/?workflow=${encodeURIComponent(workflow.id)}`);
      await expect(page.getByRole("combobox").first()).toContainText(
        workflow.title,
      );
      const form = page.getByRole("tabpanel", { name: "Workflow" });
      const media = workflow.inputs.filter(isMedia);
      // One picker per media input, and no other file control.
      await expect(
        form.getByRole("button", { name: "Choose a file" }),
      ).toHaveCount(media.length);
      const text = (await form.innerText()).replace(/\s+/g, " ");
      expect(text, workflow.id).not.toMatch(/\.\/[\w-]+\.\w+|\.bin\b/);
      // A raw field name ("source_image") is never the label.
      for (const input of media.filter((item) => item.name.includes("_"))) {
        expect(text, `${workflow.id} ${input.name}`).not.toContain(input.name);
      }
      expect(text).not.toMatch(/Managed by the workflow|upload control below/);
    });
  }
});
