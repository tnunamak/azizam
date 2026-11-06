import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { getAgentDir, getPackageDir } from "@earendil-works/pi-coding-agent";
import { AzizamError } from "./gateway.ts";

let warnedUnavailable = false;

/**
 * Pi's configured Azizam baseUrl, read with Pi's own credential-blind parser (Pi 1.0.x internal API).
 * If a Pi release moves that internal module, skip the models.json cross-check with a warning rather than
 * break discovery. A models.json that Pi itself rejects still stops credentials from being sent.
 */
export async function configuredBaseUrl(): Promise<string | undefined> {
	let ModelConfig: { load(path: string): Promise<{ getError(): unknown; getProvider(id: string): { baseUrl?: string } | undefined }> };
	try {
		({ ModelConfig } = await import(pathToFileURL(join(getPackageDir(), "dist/core/model-config.js")).href));
	} catch {
		if (!warnedUnavailable) {
			warnedUnavailable = true;
			console.error("[azizam] This Pi version does not expose its models.json parser; using AZIZAM_BASE_URL only.");
		}
		return undefined;
	}
	try {
		const config = await ModelConfig.load(join(getAgentDir(), "models.json"));
		if (config.getError()) throw new Error("Invalid model configuration");
		return config.getProvider("azizam")?.baseUrl;
	} catch {
		throw new AzizamError("Could not read Azizam configuration in models.json. Fix the file before sending credentials.");
	}
}
