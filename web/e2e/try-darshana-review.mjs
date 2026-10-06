import { chromium } from "playwright";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import login from "./darshana-auth.mjs";
import { captureAll } from "@opendatalabs/darshana/src/capture.mjs";
import { assembleHtml } from "@opendatalabs/darshana/src/html.mjs";

// Darshana 1.4 has no interaction hook. Wrap navigation to capture the same
// controlled workflow response states as the browser acceptance tests.
const baseURL = process.env.APP_URL;
if (!baseURL || !/^http:\/\/127\.0\.0\.1:18\d{3}$/.test(baseURL))
  throw new Error("Use the isolated seed-review harness");
const output = path.resolve(process.argv[2]);
const image = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);
const fields = {
  prompt: { in: "body", field: "prompt" },
  width: { in: "body", field: "size", combine: ["width", "height"] },
  height: { in: "body", field: "size", combine: ["width", "height"] },
  steps: { in: "extension", field: "steps" },
  seed: { in: "extension", field: "seed" },
  style: { in: "extension", field: "style" },
};
const workflow = {
  id: "image.generate.review",
  short_id: "review",
  title: "Review image",
  inputs: [
    { name: "prompt", type: "string", required: true },
    {
      name: "width",
      type: "integer",
      request_field: "size",
      minimum: 64,
      maximum: 2048,
      default: 512,
    },
    {
      name: "height",
      type: "integer",
      request_field: "size",
      minimum: 64,
      maximum: 2048,
      default: 512,
    },
    {
      name: "steps",
      type: "integer",
      applies: "Workflow extension",
      minimum: 1,
      maximum: 50,
      default: 8,
    },
    {
      name: "seed",
      type: "integer",
      applies: "Workflow extension",
      default: 0,
    },
    {
      name: "style",
      type: "string",
      applies: "Workflow extension",
      enum: ["studio", "flatlay"],
      default: "studio",
    },
    { name: "background", type: "string", ignored: true, accepted: false },
  ],
  outputs: [{ type: "image" }],
  example_request: {
    path: "/v1/images/generations",
    content_type: "application/json",
    body: {
      model: "review",
      prompt: "A small teal fish on a cream card.",
      size: "512x512",
      extensions: {
        "com.azizam.workflow": { steps: 8, seed: 0, style: "studio" },
      },
    },
  },
  openapi: { execution: { sync: { path: "/v1/images/generations", fields } } },
};
const browser = await chromium.launch({ headless: true });
try {
  const storageState = await login(browser);
  const reviewBrowser = {
    async newContext(options) {
      const context = await browser.newContext(options);
      await context.route("**/me/api/workflows", (route) =>
        route.fulfill({ json: { workflows: [workflow] } }),
      );
      await context.route("**/v1/assets/try-review", (route) =>
        route.fulfill({ contentType: "image/png", body: image }),
      );
      await context.route("**/me/api/receipts/try-review-receipt", (route) =>
        route.fulfill({ json: { request_id: "try-review-receipt" } }),
      );
      const createPage = context.newPage.bind(context);
      context.newPage = async () => {
        const page = await createPage();
        const navigate = page.goto.bind(page);
        page.goto = async (url, options) => {
          const response = await navigate(url, options);
          const state = new URL(url).searchParams.get("review");
          await page.getByRole("heading", { name: "Inputs" }).waitFor();
          if (state !== "empty") {
            await page.route("**/v1/images/generations", (route) =>
              state === "error"
                ? route.fulfill({
                    status: 503,
                    json: {
                      error: {
                        message:
                          "The workflow engine is unavailable. Try again when it is ready.",
                      },
                    },
                  })
                : route.fulfill({
                    headers: {
                      "x-azizam-request-id": "try-review-receipt",
                    },
                    json: {
                      data: [
                        {
                          media_type: "image/png",
                          asset_url: `${baseURL}/v1/assets/try-review`,
                        },
                      ],
                    },
                  }),
            );
            await page
              .getByRole("button", { name: "Run", exact: true })
              .click();
            await page
              .getByText(
                state === "error"
                  ? "The workflow engine is unavailable. Try again when it is ready."
                  : "Workflow result",
                { exact: true },
              )
              .waitFor();
          }
          return response;
        };
        return page;
      };
      return context;
    },
  };
  const config = {
    _storageStatePath: storageState,
    title: "Try workflow states · isolated fixtures",
    capture: {
      themes: ["light", "dark"],
      viewports: ["desktop", "mobile"],
      delay: 400,
      fullPage: true,
    },
  };
  const states = ["empty", "seeded", "error"];
  const urls = states.map(
    (state) =>
      `${baseURL}/ui/try/?workflow=image.generate.review&review=${state}`,
  );
  const captures = await captureAll(reviewBrowser, config, urls);
  if (captures.length !== 12)
    throw new Error(`Expected 12 captures, got ${captures.length}`);
  await mkdir(output, { recursive: true });
  for (const capture of captures) {
    const state = new URL(capture.url).searchParams.get("review");
    capture.label = `${state} · ${capture.viewport} · ${capture.theme}`;
    await writeFile(
      path.join(output, `${state}-${capture.viewport}-${capture.theme}.png`),
      capture.imageBuffer,
    );
  }
  await assembleHtml(captures, config, output);
  console.log(`12 workflow state captures written to ${output}`);
} finally {
  await browser.close();
}
