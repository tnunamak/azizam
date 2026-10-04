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
  switch (client) {
    case "Open WebUI":
      return {
        description:
          "Open WebUI v0.11.4: as an administrator, open Settings → Admin → Connections → OpenAI and add a connection. Paste these settings, save, refresh models, and send a message in Open WebUI. Personal/direct connections are separate and must be enabled by its administrator. Open WebUI’s server must reach the gateway: localhost inside a container points to that container. For a gateway on the Docker host, use host.docker.internal where supported; Linux may require a host-gateway mapping or a reachable LAN address. Keep the URL path below. The prior v0.11.4 check used host networking and non-streaming; other versions and network layouts are untested.",
        language: "Settings",
        code: settings,
        docs: "https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible",
      };
    case "LibreChat":
      return {
        description:
          "Add this custom endpoint to librechat.yaml. Set GATEWAY_API_KEY in LibreChat’s environment, then restart LibreChat.",
        language: "YAML",
        code: `endpoints:\n  custom:\n    - name: "Vivid Fish"\n      apiKey: "\${GATEWAY_API_KEY}"\n      baseURL: ${quotedUrl}\n      models:\n        default: [${quotedModel}]\n        fetch: true\n      modelDisplayLabel: "Vivid Fish"`,
        docs: "https://www.librechat.ai/docs/configuration/librechat_yaml/ai_endpoints",
      };
    case "SillyTavern":
      return {
        description:
          "Open API Connections. Choose Chat Completion → Custom (OpenAI-compatible), then enter these settings and connect.",
        language: "Settings",
        code: `API: Chat Completion\nSource: Custom (OpenAI-compatible)\nCustom endpoint: ${baseUrl}\nAPI key: Paste your saved gateway key\nCustom model: ${model}`,
        docs: "https://docs.sillytavern.app/usage/api-connections/",
      };
    case "Cursor":
      return {
        description:
          "In Cursor Settings → Models, enable your OpenAI API key, set Override OpenAI Base URL, and add the model below. Cursor’s servers must reach this URL; a localhost or private LAN URL will not work. Your key is sent through Cursor’s servers. Custom keys cover chat; Tab uses Cursor’s models.",
        language: "Settings",
        code: `OpenAI API key: Paste your saved gateway key\nOverride OpenAI Base URL: ${baseUrl}\nAdd model: ${model}`,
        docs: "https://cursor.com/help/models-and-usage/api-keys",
      };
    case "Codex":
      return {
        description:
          "Add this to your user-level ~/.codex/config.toml. Set GATEWAY_API_KEY in the shell before starting codex. This provider uses the gateway’s Responses API.",
        language: "TOML",
        code: `model = ${quotedModel}\nmodel_provider = "vivid_fish"\n\n[model_providers.vivid_fish]\nname = "Vivid Fish"\nbase_url = ${quotedUrl}\nenv_key = "GATEWAY_API_KEY"\nwire_api = "responses"`,
        docs: "https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers",
      };
    case "Claude Code":
      return {
        description:
          "Set GATEWAY_API_KEY to your saved key first, then run these commands in the shell that starts Claude Code. Check /status after launch to confirm the gateway URL and credential.",
        language: "Shell",
        code: `export ANTHROPIC_BASE_URL=${shellQuote(publicUrl)}\nexport ANTHROPIC_AUTH_TOKEN="$GATEWAY_API_KEY"\nexport ANTHROPIC_MODEL=${shellQuote(model)}\nexport ANTHROPIC_DEFAULT_HAIKU_MODEL=${shellQuote(model)}\nclaude`,
        docs: "https://code.claude.com/docs/en/llm-gateway-connect",
      };
    case "Python":
      return {
        description:
          "Install the openai package in your Python project. Set GATEWAY_API_KEY in your environment, then run this example.",
        language: "Python",
        code: `import os\nfrom openai import OpenAI\n\nclient = OpenAI(\n    base_url=${quotedUrl},\n    api_key=os.environ["GATEWAY_API_KEY"],\n)\nresponse = client.chat.completions.create(\n    model=${quotedModel},\n    messages=[{"role": "user", "content": "Say hello."}],\n)\nprint(response.choices[0].message.content)`,
        docs: "https://github.com/openai/openai-python#usage",
      };
    case "JavaScript":
      return {
        description:
          "Install openai with your package manager. Run this on your server with GATEWAY_API_KEY in the environment.",
        language: "JavaScript",
        code: `import OpenAI from "openai";\n\nconst client = new OpenAI({\n  baseURL: ${quotedUrl},\n  apiKey: process.env.GATEWAY_API_KEY,\n});\nconst response = await client.chat.completions.create({\n  model: ${quotedModel},\n  messages: [{ role: "user", content: "Say hello." }],\n});\nconsole.log(response.choices[0].message.content);`,
        docs: "https://github.com/openai/openai-node#usage",
      };
    case "curl":
      return {
        description:
          "Set GATEWAY_API_KEY to your saved key in your shell. This sends one chat request through your gateway.",
        language: "Shell",
        code: `curl ${shellQuote(`${baseUrl}/chat/completions`)} \\\n  -H "Authorization: Bearer $GATEWAY_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  --data ${shellQuote(`{"model":${quotedModel},"messages":${messages}}`)}`,
        docs: "https://platform.openai.com/docs/api-reference/chat/create",
      };
    case "Phone":
      return {
        description:
          "On your phone, open your household’s Open WebUI address in Safari or Chrome and sign in. Its administrator adds the connection in Settings → Admin → Connections → OpenAI with the settings below. Open WebUI’s server must reach this gateway URL. Add the page to your home screen for quick access.",
        language: "Settings",
        code: settings,
        docs: "https://docs.openwebui.com/getting-started/quick-start/",
      };
  }
}
