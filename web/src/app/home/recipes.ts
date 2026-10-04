import type { HomeAlias } from "./home-api";

export const clients = [
  "Open WebUI",
  "LibreChat",
  "SillyTavern",
  "Cursor",
  "Codex",
  "Claude Code",
  "Python",
  "JavaScript",
  "curl",
  "Phone",
] as const;
export type Client = (typeof clients)[number];
export type RecipeField = { label: string; value: string };

function shellQuote(value: string): string {
  return "'" + value.replaceAll("'", "'\"'\"'") + "'";
}

export function chatAlias(aliases: HomeAlias[]): string | null {
  const chat = aliases.filter((alias) =>
    ["chat.generate", "chat", "chat.completions"].includes(alias.operation),
  );
  return (
    chat.find((alias) => alias.id === "fast" && alias.available)?.id ??
    chat.find((alias) => alias.available)?.id ??
    chat[0]?.id ??
    null
  );
}

/** Recipes use server-advertised aliases and environment references, never a person's key. */
export function recipeFor(
  client: Client,
  baseUrl: string,
  publicUrl: string,
  model: string,
) {
  const quotedModel = JSON.stringify(model);
  const quotedUrl = JSON.stringify(baseUrl);
  const messages = '[{"role":"user","content":"Say hello."}]';
  const settings = `Base URL: ${baseUrl}\nAPI key: Paste your saved gateway key\nModel: ${model}`;
  const openAiFields = [
    { label: "Base URL", value: baseUrl },
    { label: "Model", value: model },
  ];
  switch (client) {
    case "Open WebUI":
      return {
        description:
          "Add an OpenAI connection, paste these settings, then send a message.",
        fields: openAiFields,
        keyLabel: "API key",
        language: "Settings",
        code: settings,
        docs: "https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible",
      };
    case "LibreChat":
      return {
        description:
          "Add this custom endpoint to librechat.yaml, set GATEWAY_API_KEY, then restart LibreChat.",
        fields: [
          { label: "baseURL", value: baseUrl },
          { label: "models.default", value: model },
        ],
        keyLabel: "GATEWAY_API_KEY",
        language: "YAML",
        code: `endpoints:\n  custom:\n    - name: "Vivid Fish"\n      apiKey: "\${GATEWAY_API_KEY}"\n      baseURL: ${quotedUrl}\n      models:\n        default: [${quotedModel}]\n        fetch: true\n      modelDisplayLabel: "Vivid Fish"`,
        docs: "https://www.librechat.ai/docs/configuration/librechat_yaml/ai_endpoints",
      };
    case "SillyTavern":
      return {
        description:
          "Choose Custom (OpenAI-compatible), then paste these settings.",
        fields: [
          { label: "Custom endpoint", value: baseUrl },
          { label: "Custom model", value: model },
        ],
        keyLabel: "API key",
        language: "Settings",
        code: `API: Chat Completion\nSource: Custom (OpenAI-compatible)\nCustom endpoint: ${baseUrl}\nAPI key: Paste your saved gateway key\nCustom model: ${model}`,
        docs: "https://docs.sillytavern.app/usage/api-connections/",
      };
    case "Cursor":
      return {
        description:
          "In Cursor Settings → Models, add an OpenAI key and the model below.",
        fields: [
          { label: "Override OpenAI Base URL", value: baseUrl },
          { label: "Add model", value: model },
        ],
        keyLabel: "OpenAI API key",
        language: "Settings",
        code: `OpenAI API key: Paste your saved gateway key\nOverride OpenAI Base URL: ${baseUrl}\nAdd model: ${model}`,
        docs: "https://cursor.com/help/models-and-usage/api-keys",
      };
    case "Codex":
      return {
        description:
          "Add this to ~/.codex/config.toml, then set GATEWAY_API_KEY before starting codex.",
        fields: [
          { label: "base_url", value: baseUrl },
          { label: "model", value: model },
        ],
        keyLabel: "GATEWAY_API_KEY",
        language: "TOML",
        code: `model = ${quotedModel}\nmodel_provider = "vivid_fish"\n\n[model_providers.vivid_fish]\nname = "Vivid Fish"\nbase_url = ${quotedUrl}\nenv_key = "GATEWAY_API_KEY"\nwire_api = "responses"`,
        docs: "https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers",
      };
    case "Claude Code":
      return {
        description: "Set these variables before starting Claude Code.",
        fields: [
          { label: "ANTHROPIC_BASE_URL", value: publicUrl },
          { label: "ANTHROPIC_MODEL", value: model },
        ],
        keyLabel: "ANTHROPIC_AUTH_TOKEN",
        language: "Shell",
        code: `export ANTHROPIC_BASE_URL=${shellQuote(publicUrl)}\nexport ANTHROPIC_AUTH_TOKEN="$GATEWAY_API_KEY"\nexport ANTHROPIC_MODEL=${shellQuote(model)}\nexport ANTHROPIC_DEFAULT_HAIKU_MODEL=${shellQuote(model)}\nclaude`,
        docs: "https://code.claude.com/docs/en/llm-gateway-connect",
      };
    case "Python":
      return {
        description:
          "Install openai, set GATEWAY_API_KEY, then run this example.",
        fields: [
          { label: "base_url", value: baseUrl },
          { label: "model", value: model },
        ],
        keyLabel: "GATEWAY_API_KEY",
        language: "Python",
        code: `import os\nfrom openai import OpenAI\n\nclient = OpenAI(\n    base_url=${quotedUrl},\n    api_key=os.environ["GATEWAY_API_KEY"],\n)\nresponse = client.chat.completions.create(\n    model=${quotedModel},\n    messages=[{"role": "user", "content": "Say hello."}],\n)\nprint(response.choices[0].message.content)`,
        docs: "https://github.com/openai/openai-python#usage",
      };
    case "JavaScript":
      return {
        description:
          "Install openai, set GATEWAY_API_KEY on your server, then run this example.",
        fields: [
          { label: "baseURL", value: baseUrl },
          { label: "model", value: model },
        ],
        keyLabel: "GATEWAY_API_KEY",
        language: "JavaScript",
        code: `import OpenAI from "openai";\n\nconst client = new OpenAI({\n  baseURL: ${quotedUrl},\n  apiKey: process.env.GATEWAY_API_KEY,\n});\nconst response = await client.chat.completions.create({\n  model: ${quotedModel},\n  messages: [{ role: "user", content: "Say hello." }],\n});\nconsole.log(response.choices[0].message.content);`,
        docs: "https://github.com/openai/openai-node#usage",
      };
    case "curl":
      return {
        description:
          "Set GATEWAY_API_KEY in your shell. This sends one chat request.",
        fields: [
          { label: "URL", value: `${baseUrl}/chat/completions` },
          { label: "model", value: model },
        ],
        keyLabel: "GATEWAY_API_KEY",
        language: "Shell",
        code: `curl ${shellQuote(`${baseUrl}/chat/completions`)} \\\n  -H "Authorization: Bearer $GATEWAY_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  --data ${shellQuote(`{"model":${quotedModel},"messages":${messages}}`)}`,
        docs: "https://platform.openai.com/docs/api-reference/chat/create",
      };
    case "Phone":
      return {
        description: "Use your household Open WebUI connection.",
        fields: openAiFields,
        keyLabel: "API key",
        language: "Settings",
        code: settings,
        docs: "https://docs.openwebui.com/getting-started/quick-start/",
      };
  }
}
