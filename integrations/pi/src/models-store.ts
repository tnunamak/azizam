import { randomUUID } from "node:crypto";
import { chmodSync, lstatSync, renameSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import type { ModelsStore, ModelsStoreOperationOptions } from "@earendil-works/pi-ai";
import { getAgentDir, getPackageDir } from "@earendil-works/pi-coding-agent";
import { DEFAULT_TIMEOUT_MS } from "./gateway.ts";

/**
 * Pi core holds `models-store.json.lock` through proper-lockfile with stale=30 s and refreshes the lock's mtime every
 * stale/2 = 15 s while held. A Pi that exits between creating the lock directory and registering it leaves an orphan
 * that blocks the next start for the full 30 s. A lock whose mtime is older than one refresh interval plus a grace has
 * missed a refresh, so no live holder owns it; younger locks are never touched.
 */
const CORE_LOCK_UPDATE_MS = 15_000;
export const ORPHAN_LOCK_STALE_MS = CORE_LOCK_UPDATE_MS + 1_000;

interface LockfileApi {
	check(file: string, options: { realpath: false; stale: number }): Promise<boolean>;
	lock(file: string, options: { realpath: false; stale: number; retries: 0 }): Promise<() => Promise<void>>;
}

export interface RepairingModelsStoreOptions {
	/** Longest any one store operation may wait for the lock before degrading. Defaults to the 10 s startup budget. */
	lockBudgetMs?: number;
	/** Age at which an unrefreshed lock counts as orphaned. Defaults to {@link ORPHAN_LOCK_STALE_MS}. */
	orphanStaleMs?: number;
}

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
 * Unreadable files still propagate: those are storage failures, not evidence of corruption. A lock that outlasts the
 * startup budget degrades to an empty read / skipped write instead of blocking.
 * Pi does not export these storage classes; use its package-directory API to resolve the running installation,
 * including globally installed Pi loading this extension without local peers. The real-Pi regression is the
 * compatibility gate for these internal modules.
 */
export async function createRepairingModelsStore(
	path = join(getAgentDir(), "models-store.json"),
	{ lockBudgetMs = DEFAULT_TIMEOUT_MS, orphanStaleMs = ORPHAN_LOCK_STALE_MS }: RepairingModelsStoreOptions = {},
): Promise<ModelsStore> {
	const dist = join(getPackageDir(), "dist");
	const lockfile = createRequire(join(getPackageDir(), "package.json"))("proper-lockfile") as LockfileApi;
	const lockPath = `${path}.lock`;
	const lockExists = () => { try { return lstatSync(lockPath).isDirectory(); } catch { return false; } };
	// proper-lockfile's own stale handling removes the orphan: lock() with a shorter stale age rmdirs it and takes
	// the lock, which we release at once. A lock refreshed within the age answers ELOCKED and is left alone.
	const reclaimOrphanedLock = async () => {
		if (!lockExists()) return;
		const opts = { realpath: false, stale: orphanStaleMs } as const;
		try {
			if (await lockfile.check(path, opts)) return;
			await (await lockfile.lock(path, { ...opts, retries: 0 }))();
		} catch {
			// ELOCKED or a race with the holder: the lock is live, so the bounded wait below decides.
		}
	};
	let degradedReported = false;
	/** Runs a store operation within the lock budget; a lock that outlasts it degrades to `fallback`. */
	const bounded = async <T>(options: ModelsStoreOperationOptions | undefined, fallback: T, run: (options: ModelsStoreOperationOptions) => Promise<T>): Promise<T> => {
		await reclaimOrphanedLock();
		const budget = AbortSignal.timeout(lockBudgetMs);
		const signal = options?.signal ? AbortSignal.any([options.signal, budget]) : budget;
		try {
			return await run({ ...options, signal });
		} catch (error) {
			if (!budget.aborted || options?.signal?.aborted || !lockExists()) throw error;
			if (!degradedReported) {
				degradedReported = true;
				console.error(`[azizam] ${lockPath} is still locked after ${lockBudgetMs} ms; continuing without the saved catalog this start.`);
			}
			return fallback;
		}
	};
	const [{ FileModelsStore }, { FileAuthStorageBackend }] = await Promise.all([
		import(pathToFileURL(join(dist, "core/models-store.js")).href),
		import(pathToFileURL(join(dist, "core/auth-storage.js")).href),
	]);
	const store: ModelsStore = new FileModelsStore(path);
	const storage: LockedStorage = new FileAuthStorageBackend(path);
	let reported = false;
	const repair = async (options: ModelsStoreOperationOptions) => {
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
		read: (providerId, options) => bounded(options, undefined, async (op) => {
			await repair(op);
			return store.read(providerId, op);
		}),
		write: (providerId, entry, options) => bounded(options, undefined, async (op) => {
			await repair(op);
			await store.write(providerId, entry, op);
		}),
		delete: (providerId, options) => bounded(options, undefined, async (op) => {
			await repair(op);
			await store.delete(providerId, op);
		}),
	};
}
