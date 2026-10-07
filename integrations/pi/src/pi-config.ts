import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { getAgentDir, getPackageDir } from "@earendil-works/pi-coding-agent";
import { AzizamError } from "./gateway.ts";

/** Use Pi's own credential-blind parser, including its JSON comments and validation. Pi 1.0.x internal API. */
export async function configuredBaseUrl(): Promise<string | undefined> {
	try {
		const { ModelConfig } = await import(pathToFileURL(join(getPackageDir(), "dist/core/model-config.js")).href);
		const config = await ModelConfig.load(join(getAgentDir(), "models.json"));
		if (config.getError()) throw new Error("Invalid model configuration");
		return config.getProvider("azizam")?.baseUrl;
	} catch {
		throw new AzizamError("Could not read Azizam configuration in models.json. Fix the file before sending credentials.");
	}
}
