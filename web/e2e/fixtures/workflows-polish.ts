export const workflowsPolishCatalog = {
  schema_version: 1,
  workflows: [
    {
      id: "image.product-render",
      capability: "image",
      title: "Product render",
      description:
        "Create a clean product render with a local workflow. Long manifest guidance stays searchable and available inside Technical details without turning browse cards into documentation blocks. Use it when the operator needs exact route behavior, backend binding context, and alias provenance.",
      inputs: [
        {
          name: "prompt",
          type: "string",
          required: true,
          accepted: true,
          description: "What to render.",
        },
        {
          name: "seed",
          type: "integer",
          default: 0,
          minimum: 0,
          maximum: 65_535,
          accepted: true,
        },
        {
          name: "guidance_scale",
          type: "number",
          default: 0,
          minimum: 0,
          maximum: 20,
          accepted: true,
        },
        {
          name: "enabled",
          type: "boolean",
          default: false,
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
        status: "ready",
        reason: "Configured for this access plan.",
        available: true,
      },
      typical_seconds: 12,
      aliases: ["image", "product-shot"],
      examples: {
        curl: 'curl $AI_GATEWAY_BASE/v1/images/generations -H "Authorization: Bearer $AI_GATEWAY_API_KEY" -H "Content-Type: application/json" -d \'{"workflow":"image.product-render","prompt":"A teal desk lamp"}\'',
        python:
          'from openai import OpenAI\nclient = OpenAI()\nclient.images.generate(model="image", prompt="A teal desk lamp", extra_body={"workflow":"image.product-render"})',
        javascript:
          'import OpenAI from "openai";\nconst client = new OpenAI();\nawait client.images.generate({ model: "image", prompt: "A teal desk lamp", workflow: "image.product-render" });',
      },
      native_route: "/v1/workflows/image.product-render/run",
      discovery_url: "/gateway/v1/workflows/image.product-render",
      try_url: "/ui/try/?workflow=image.product-render",
    },
    {
      id: "image.scene-board",
      capability: "image",
      title: "Scene board",
      description: "Make a set of coordinated scene images.",
      inputs: [],
      outputs: [
        {
          name: "board",
          media_type: "image/webp",
          description: "Generated scene board contact sheet.",
        },
      ],
      engine_binding: "image-local",
      availability: {
        status: "ready",
        reason: "Configured for this access plan.",
        available: true,
      },
      typical_seconds: 18,
      aliases: ["scene"],
      examples: {
        curl: "curl example",
        python: "python example",
        javascript: "js example",
      },
      native_route: null,
      discovery_url: "/gateway/v1/workflows/image.scene-board",
    },
    {
      id: "image.product-thumbnail",
      capability: "image",
      title: "Product thumbnail",
      description: "Create a compact product thumbnail.",
      inputs: [],
      outputs: [],
      engine_binding: "image-local",
      availability: {
        status: "ready",
        reason: "Configured for this access plan.",
        available: true,
      },
      typical_seconds: 6,
      aliases: ["thumbnail"],
      examples: {
        curl: "curl example",
        python: "python example",
        javascript: "js example",
      },
      native_route: null,
      discovery_url: "/gateway/v1/workflows/image.product-thumbnail",
    },
  ],
};
