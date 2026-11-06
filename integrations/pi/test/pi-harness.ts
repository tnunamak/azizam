import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync, readdirSync, rmSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

export const ENTRY = resolve(import.meta.dirname, "../extensions/azizam.ts");

/** Locate the `pi` CLI: $PI_BIN, then the mise node lts install, then PATH. */
export function findPi(): string | undefined {
	const candidates = [process.env.PI_BIN, join(homedir(), ".local/share/mise/installs/node/lts/bin/pi")];
	for (const candidate of candidates) if (candidate && existsSync(candidate)) return candidate;
	return "pi";
}

export interface PiSession {
	send(command: Record<string, unknown>): Promise<any>;
	waitFor(predicate: (event: any) => boolean, timeoutMs?: number): Promise<any>;
	events: any[];
	stderr(): string;
	close(): Promise<void>;
}

/**
 * Pi guards its stores with proper-lockfile directories (`<store>.lock`, stale after 30 s). A Pi that exits while it is
 * between creating a lock directory and registering it for exit cleanup leaves the directory behind, and the next Pi
 * on the same agent dir then waits up to 30 s for it with no output (the 20 s waitFor timeout, `last=[]`).
 * The owner has exited by the time close() gets here, so no live process can hold these locks: remove them.
 */
function removeOrphanedLocks(agentDir: string): void {
	if (!existsSync(agentDir)) return;
	for (const name of readdirSync(agentDir)) if (name.endsWith(".lock")) rmSync(join(agentDir, name), { recursive: true, force: true });
}

/** Start `pi --mode rpc --no-session` with an isolated agent dir and the extension loaded from source. */
export function startPi(options: { agentDir: string; env?: Record<string, string>; args?: string[]; viaPackage?: boolean; noExtension?: boolean; bin?: string }): PiSession {
	const child: ChildProcessWithoutNullStreams = spawn(
		options.bin ?? findPi()!,
		["--mode", "rpc", "--no-session", ...(options.viaPackage || options.noExtension ? [] : ["--extension", ENTRY]), ...(options.args ?? [])],
		{ env: { ...process.env, PI_CODING_AGENT_DIR: options.agentDir, ...options.env }, stdio: "pipe" },
	);
	const events: any[] = [];
	const waiters: { predicate: (e: any) => boolean; resolve: (e: any) => void }[] = [];
	let stderr = "";
	let buffer = "";
	let nextId = 1;
	child.stderr.on("data", (d) => { stderr += d; });
	child.stdout.on("data", (d) => {
		buffer += d;
		let index: number;
		while ((index = buffer.indexOf("\n")) >= 0) {
			const line = buffer.slice(0, index);
			buffer = buffer.slice(index + 1);
			if (!line.trim()) continue;
			let event: any;
			try { event = JSON.parse(line); } catch { continue; }
			events.push(event);
			for (const w of [...waiters]) if (w.predicate(event)) { waiters.splice(waiters.indexOf(w), 1); w.resolve(event); }
		}
	});
	const waitFor = (predicate: (e: any) => boolean, timeoutMs = 20000) => {
		const existing = events.find(predicate);
		if (existing) return Promise.resolve(existing);
		return new Promise<any>((resolvePromise, reject) => {
			const timer = setTimeout(() => reject(new Error(`timeout waiting for event; stderr=${stderr}; last=${JSON.stringify(events.slice(-3))}`)), timeoutMs);
			waiters.push({ predicate, resolve: (e) => { clearTimeout(timer); resolvePromise(e); } });
		});
	};
	return {
		events,
		stderr: () => stderr,
		waitFor,
		send: async (command) => {
			const id = `t${nextId++}`;
			child.stdin.write(`${JSON.stringify({ id, ...command })}\n`);
			return waitFor((e) => e.type === "response" && e.id === id);
		},
		close: async () => {
			if (child.exitCode === null) {
				await new Promise<void>((done) => {
					child.once("exit", () => done());
					child.stdin.end();
					setTimeout(() => child.kill("SIGKILL"), 3000).unref();
				});
			}
			removeOrphanedLocks(options.agentDir);
		},
	};
}
