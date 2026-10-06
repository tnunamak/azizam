import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { azizamProvider } from "../src/provider.ts";

export default async function azizamExtension(pi: ExtensionAPI): Promise<void> {
	const provider = azizamProvider();
	await provider.prefetch();
	pi.registerProvider(provider);
}
