// Lets plain `node` import "@earendil-works/pi-ai" in unit tests by resolving it from the installed Pi CLI.
import { existsSync, realpathSync } from "node:fs";
import { register } from "node:module";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";
import { findPi } from "./pi-harness.ts";

export function piAiRoot(): string | undefined {
	const bin = findPi();
	if (!bin) return undefined;
	try {
		const real = realpathSync(bin.includes("/") ? bin : join(process.env.PATH?.split(":").find((d) => existsSync(join(d, bin))) ?? "", bin));
		let dir = dirname(real);
		while (dir !== dirname(dir)) {
			const candidate = join(dir, "node_modules/@earendil-works/pi-ai");
			if (existsSync(candidate)) return candidate;
			dir = dirname(dir);
		}
	} catch { /* fall through */ }
	return undefined;
}

export function registerPiResolver(root: string): void {
	const hook = `
const root = ${JSON.stringify(pathToFileURL(root).href)};
export async function resolve(specifier, context, next) {
  if (specifier === "@earendil-works/pi-ai") return next(root + "/dist/index.js", context);
  if (specifier.startsWith("@earendil-works/pi-ai/")) return next(root + "/dist/" + specifier.slice("@earendil-works/pi-ai/".length) + ".js", context);
  return next(specifier, context);
}`;
	register(`data:text/javascript,${encodeURIComponent(hook)}`);
}
