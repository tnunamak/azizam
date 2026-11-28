// Kept apart from models-store.ts, which loads Pi internals; provider.ts must import this without them.
/** A catalog write that waited out the lock budget. The store throws; each caller decides what that means. */
export class ModelsStoreWriteTimeoutError extends Error {
	readonly lockPath: string;
	readonly budgetMs: number;
	constructor(lockPath: string, budgetMs: number) {
		super(`The saved model catalog is locked (${lockPath}) and was not written after ${budgetMs} ms.`);
		this.name = "ModelsStoreWriteTimeoutError";
		this.lockPath = lockPath;
		this.budgetMs = budgetMs;
	}
}
