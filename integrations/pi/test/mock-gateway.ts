import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";

export const CONTRACT_LIST = {
	object: "list",
	data: [
		{
			id: "coder", object: "model", created: 1, owned_by: "azizam", type: "language",
			architecture: { input_modalities: ["text", "image"], output_modalities: ["text"] },
			context_window: 200000, max_tokens: 16000, tags: ["tool-use", "reasoning", "vision"],
		},
		{
			id: "chat-small", object: "model", created: 1, owned_by: "azizam", type: "language",
			architecture: { input_modalities: ["text"], output_modalities: ["text"] },
		},
		{ id: "gpt-image", object: "model", created: 1, owned_by: "azizam", type: "image",
			architecture: { input_modalities: ["text"], output_modalities: ["image"] } },
		{ id: "embed", object: "model", created: 1, owned_by: "azizam", type: "embedding",
			architecture: { input_modalities: ["text"], output_modalities: ["embedding"] }, context_window: 8192 },
		{ id: "tts", object: "model", created: 1, owned_by: "azizam", type: "audio",
			architecture: { input_modalities: ["text"], output_modalities: ["audio"] } },
	],
};

export interface MockGateway {
	url: string;
	baseUrl: string;
	requests: { method: string; url: string; authorization?: string; body?: string; status?: number }[];
	setModels(list: unknown): void;
	setStatus(status: number): void;
	setRedirect(enabled: boolean): void;
	setPartialBody(enabled: boolean): void;
	close(): Promise<void>;
}

const sse = (chunk: unknown) => `data: ${JSON.stringify(chunk)}\n\n`;

/** Mock Azizam gateway on a random free port: contract `/v1/models` plus streaming `/v1/chat/completions`. */
export async function startMockGateway(options: { apiKey?: string } = {}): Promise<MockGateway> {
	let list: unknown = CONTRACT_LIST;
	let status = 200;
	let redirect = false;
	let partialBody = false;
	const requests: MockGateway["requests"] = [];
	const server: Server = createServer(async (req: IncomingMessage, res) => {
		let body = "";
		for await (const chunk of req) body += chunk;
		const entry: MockGateway["requests"][number] = { method: req.method ?? "", url: req.url ?? "", authorization: req.headers.authorization, body };
		requests.push(entry);
		res.once("finish", () => { entry.status = res.statusCode; });
		if (options.apiKey && req.headers.authorization !== `Bearer ${options.apiKey}`) {
			res.writeHead(401, { "content-type": "application/json" }).end(JSON.stringify({ error: { message: "bad key" } }));
			return;
		}
		if (status !== 200) {
			res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify({ error: { message: "forced" } }));
			return;
		}
		if (req.method === "GET" && req.url === "/v1/models") {
			if (redirect) {
				res.writeHead(302, { location: "/v1/redirect-target" }).end();
				return;
			}
			if (partialBody) {
				res.writeHead(200, { "content-type": "application/json" });
				res.write('{"data":[');
				return;
			}
			res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(list));
		} else if (req.method === "POST" && req.url === "/v1/chat/completions") {
			res.writeHead(200, { "content-type": "text/event-stream" });
			const base = { id: "c1", object: "chat.completion.chunk", created: 1, model: "coder" };
			res.write(sse({ ...base, choices: [{ index: 0, delta: { role: "assistant", reasoning_content: "thinking..." } }] }));
			res.write(sse({ ...base, choices: [{ index: 0, delta: { content: "Hello " } }] }));
			res.write(sse({ ...base, choices: [{ index: 0, delta: { content: "from Azizam" } }] }));
			res.write(sse({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: { prompt_tokens: 5, completion_tokens: 4, total_tokens: 9 } }));
			res.end("data: [DONE]\n\n");
		} else {
			res.writeHead(404).end("not found");
		}
	});
	await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
	const port = (server.address() as AddressInfo).port;
	return {
		url: `http://127.0.0.1:${port}`,
		baseUrl: `http://127.0.0.1:${port}/v1`,
		requests,
		setModels: (next) => { list = next; },
		setStatus: (next) => { status = next; },
		setRedirect: (enabled) => { redirect = enabled; },
		setPartialBody: (enabled) => { partialBody = enabled; },
		close: () => new Promise((resolve) => { server.closeAllConnections(); server.close(() => resolve()); }),
	};
}
