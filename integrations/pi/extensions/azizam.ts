import { ModelRuntime, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { azizamProvider, registerProviderScoped } from "../src/provider.ts";

export default async function azizamExtension(pi: ExtensionAPI): Promise<void> {
	const provider = azizamProvider();
	try {
		// A throwaway runtime over the same agent dir resolves credentials the way Pi does and shares this
		// provider's catalog state, so the catalog exists before model selection. Registration is scoped to this
		// provider so unrelated providers' credential commands are not run.
		const runtime = await ModelRuntime.create({ refreshOnCreate: false });
		registerProviderScoped(runtime, provider);
		await provider.prefetch(runtime);
	} catch {
		// Discovery is best effort; Pi's own background refresh still runs and reports errors.
	}
	pi.registerProvider(provider);
}
