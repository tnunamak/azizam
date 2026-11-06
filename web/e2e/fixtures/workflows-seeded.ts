// Fixtures shared by the Workflows page specs (browser and vitest).
export const seededCatalog = {
  schema_version: 1,
  workflows: [
    {
      id: "image.product-render",
      capability: "image",
      title: "Product render",
      description: "Create a clean product render.",
      technical_notes:
        "Long manifest guidance stays searchable and available inside Technical details without turning browse cards into documentation blocks.",
      inputs: [
        {
          name: "prompt",
          type: "string",
          required: true,
          accepted: true,
          description: "What to render.",
        },
        {
          name: "width",
          type: "integer",
          default: 1024,
          minimum: 256,
          maximum: 2048,
          accepted: true,
        },
        {
          name: "style_preset",
          type: "string",
          default: "studio",
          enum: ["studio", "flatlay"],
          accepted: false,
        },
      ],
      outputs: [
        {
          name: "render",
          media_type: "image/png",
          description: "Final product render asset.",
        },
      ],
      engine_binding: "image-local",
      availability: {
        status: "configured",
        reason_code: "configured",
        available: true,
        measured: false,
      },
      typical_seconds: 12,
      aliases: ["image", "product-shot"],
      examples: {
        curl: 'curl $AZIZAM_BASE/v1/images/generations -H "Authorization: Bearer $AZIZAM_API_KEY" -H "Content-Type: application/json" -d \'{"workflow":"image.product-render","prompt":"A teal desk lamp"}\'',
        python:
          'from openai import OpenAI\nclient = OpenAI()\nclient.images.generate(model="image", prompt="A teal desk lamp", extra_body={"workflow":"image.product-render"})',
        javascript:
          'import OpenAI from "openai";\nconst client = new OpenAI();\nawait client.images.generate({ model: "image", prompt: "A teal desk lamp", workflow: "image.product-render" });',
      },
      native_route: "/v1/workflows/image.product-render/run",
      discovery_url: "/gateway/v1/workflows/image.product-render",
      try_url: "/try/?workflow=image.product-render",
    },
    {
      id: "audio.voiceover",
      capability: "audio",
      title: "Voiceover",
      description: "Generate spoken audio from a short script.",
      inputs: [
        {
          name: "text",
          type: "string",
          required: true,
          accepted: true,
        },
      ],
      outputs: [],
      engine_binding: null,
      availability: {
        status: "unavailable",
        reason_code: "unavailable_unconfigured",
        available: false,
        measured: false,
      },
      typical_seconds: null,
      aliases: ["speech"],
      examples: {
        curl: 'curl $AZIZAM_BASE/v1/audio/speech -H "Authorization: Bearer $AZIZAM_API_KEY" -d \'{"workflow":"audio.voiceover","input":"Hello"}\'',
        python:
          'from openai import OpenAI\nclient = OpenAI()\nclient.audio.speech.create(model="voice", input="Hello", voice="alloy")',
        javascript:
          'import OpenAI from "openai";\nconst client = new OpenAI();\nawait client.audio.speech.create({ model: "voice", input: "Hello", voice: "alloy" });',
      },
      native_route: null,
      discovery_url: "/gateway/v1/workflows/audio.voiceover",
      try_url: "/try/?workflow=audio.voiceover",
    },
  ],
};

export const seededRecentJobs = {
  assets: [
    {
      asset_id: "asset_image_1",
      job_id: "job_product_1",
      kind: "image",
      prompt: "A teal desk lamp on walnut",
      model: "image",
      workflow: "image.product-render",
      created_at: 1_760_000_000,
      generation_ms: 12_400,
      download_url: "/me/api/media/asset_image_1/download",
      receipt_id: "req_product_1",
    },
    {
      asset_id: "asset_image_2",
      job_id: "job_product_2",
      kind: "image",
      prompt: "A quiet studio product render",
      model: "image",
      workflow: "image.product-render",
      created_at: 1_760_010_000,
      generation_ms: 10_900,
      download_url: "/me/api/media/asset_image_2/download",
      receipt_id: "req_product_2",
    },
  ],
  models: ["image"],
  next_cursor: null,
};

export const faceLikenessRow = {
  id: "image.analyze.face-likeness",
  capability: "image",
  title: "Face Likeness Check",
  purpose: "Score how closely a face matches a reference",
  description: "Score how closely a face matches a reference",
  inputs: [
    { name: "reference", type: "string", asset: true, media_type: "image/*", label: "a face photo", required: true, accepted: true },
    { name: "candidate", type: "string", asset: true, media_type: "image/*", label: "a photo to check", required: true, accepted: true },
    { name: "people", type: "string", label: "a list of people", required: false, accepted: true },
    { name: "thresholds", type: "string", label: "custom thresholds", required: false, accepted: true },
  ],
  outputs: [{ name: "report", media_type: "application/json", label: "a likeness score" }],
  engine_binding: "gateway",
  availability: { status: "unknown", reason_code: "unknown", available: false, measured: false, evidence: { kind: "unknown" } },
  typical_seconds: 2,
  aliases: [],
  examples: { curl: "curl", python: "python", javascript: "js" },
  native_route: null,
  discovery_url: "/gateway/v1/workflows/image.analyze.face-likeness",
};

// Workflows the gateway composes from engines: their availability names the
// engine they wait on (gateway/availability.py _dependency_reason).
export const composedEvidence = { source: "dependencies", kind: "required" };
export const composedRows = [
  {
    ...faceLikenessRow,
    id: "image.edit.image-identity",
    title: "Identity Scene",
    discovery_url: "/gateway/v1/workflows/image.edit.image-identity",
    availability: { status: "ready", reason_code: "ready", available: true, measured: true,
                    reason: "Every engine it needs is ready.", engine: "comfyui", evidence: composedEvidence },
  },
  {
    ...faceLikenessRow,
    availability: { status: "idle", reason_code: "idle", available: true, measured: false,
                    reason: "Face service starts when you make a request.", engine: "faces", evidence: composedEvidence },
  },
  {
    ...faceLikenessRow,
    id: "image.generate.flux2-klein-4b-seamless-tile",
    title: "Seamless Tile",
    discovery_url: "/gateway/v1/workflows/image.generate.flux2-klein-4b-seamless-tile",
    availability: { status: "unavailable", reason_code: "unavailable", available: false, measured: true,
                    reason: "ComfyUI is down.", engine: "comfyui", evidence: composedEvidence },
  },
];
