import { randomUUID } from "node:crypto";
import { chmodSync, renameSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import type { ModelsStore, ModelsStoreOperationOptions } from "@earendil-works/pi-ai";
import { getAgentDir, getPackageDir } from "@earendil-works/pi-coding-agent";

interface LockedStorage {
	withLockAsync<T>(
		fn: (content: string | undefined) => Promise<{ result: T; next?: string }>,
		options?: ModelsStoreOperationOptions,
	): Promise<T>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Validate only our provider's catalog; unrelated providers own their persisted model schemas. */
function isUsableStore(value: unknown): boolean {
	if (!isRecord(value)) return false;
	if (!Object.hasOwn(value, "azizam")) return true;
	const entry = value.azizam;
	return isRecord(entry) && Array.isArray(entry.models) && entry.models.every((model: unknown) =>
		isRecord(model) && typeof model.id === "string" && model.provider === "azizam" && typeof model.baseUrl === "string",
	);
}

/**
 * Keep Pi's catalog format and file locks, but quarantine malformed catalogs so they cannot poison later reads.
 * Unreadable files and lock failures still propagate: those are storage failures, not evidence of corruption.
 * Pi does not export these storage classes; use its package-directory API to resolve the running installation,
 * including globally installed Pi loading this extension without local peers. The real-Pi regression is the
 * compatibility gate for these internal modules.
 */
export async function createRepairingModelsStore(path = join(getAgentDir(), "models-store.json")): Promise<ModelsStore> {
	const dist = join(getPackageDir(), "dist");
	const [{ FileModelsStore }, { FileAuthStorageBackend }] = await Promise.all([
		import(pathToFileURL(join(dist, "core/models-store.js")).href),
		import(pathToFileURL(join(dist, "core/auth-storage.js")).href),
	]);
	const store: ModelsStore = new FileModelsStore(path);
	const storage: LockedStorage = new FileAuthStorageBackend(path);
	let reported = false;
	const repair = async (options?: ModelsStoreOperationOptions) => {
		await storage.withLockAsync(async (content) => {
			if (!content) return { result: undefined };
			try {
				const parsed: unknown = JSON.parse(content.replace(/^\uFEFF/, ""));
				if (isUsableStore(parsed)) {
					return { result: undefined };
				}
			} catch (error) {
				if (!(error instanceof SyntaxError)) throw error;
			}
			options?.signal?.throwIfAborted();
			const quarantine = `${path}.corrupt-${new Date().toISOString().replaceAll(":", "-")}-${randomUUID()}`;
			// Tighten access before moving: the original may have been created with broad permissions.
			chmodSync(path, 0o600);
			renameSync(path, quarantine);
			if (!reported) {
				reported = true;
				console.error("[azizam] Corrupt models-store.json quarantined in the agent directory; rebuilding the saved catalog.");
			}
			return { result: undefined, next: "{}" };
		}, options);
	};
	return {
		read: async (providerId, options) => {
			await repair(options);
			return store.read(providerId, options);
		},
		write: async (providerId, entry, options) => {
			await repair(options);
			await store.write(providerId, entry, options);
		},
		delete: async (providerId, options) => {
			await repair(options);
			await store.delete(providerId, options);
		},
	};
}
