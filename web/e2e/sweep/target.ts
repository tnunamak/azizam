// Where the sweep runs: the hermetic household fixture (default), the
// synthetic live-data harness (scripts/ui/live_data_seed.py), or that harness
// on a COPY of the production data (--copy-from; scripts/ui/live_data_copy.py). Ports stay out of the way
// of the other suites (18180-18182) so parallel worktrees do not collide.
import path from "node:path";

export interface SweepTarget {
  name: "fixtures" | "live" | "copy";
  port: number;
  baseURL: string;
  /** Unclaimed gateway for the first-run Setup screen (fixtures only). */
  setupPort?: number;
  setupBaseURL?: string;
}

const WEB = path.resolve(__dirname, "../..");

export function sweepTarget(): SweepTarget {
  const name = (process.env.SWEEP_TARGET ?? "fixtures") as SweepTarget["name"];
  if (!["fixtures", "live", "copy"].includes(name)) throw new Error("SWEEP_TARGET must be fixtures, live or copy");
  const port = Number(process.env.SWEEP_PORT ?? { fixtures: "18860", live: "18866", copy: "18870" }[name]);
  if (!Number.isInteger(port) || port < 18000 || port > 18994)
    throw new Error("SWEEP_PORT must be an integer between 18000 and 18994 (the sweep uses port to port+3)");
  const target: SweepTarget = { name, port, baseURL: `http://127.0.0.1:${port}` };
  if (name === "fixtures") {
    target.setupPort = port + 2;
    target.setupBaseURL = `http://127.0.0.1:${port + 2}`;
  }
  return target;
}

export function sweepPaths(target: SweepTarget) {
  const dir = path.join(WEB, "test-results", "sweep", target.name);
  return {
    web: WEB,
    dir,
    findings: path.join(dir, "findings"),
    shots: path.join(dir, "shots"),
    seedManifest: path.join(dir, `seed-manifest-${target.port}.json`),
    ownerState: path.join(dir, "owner.json"),
    memberState: path.join(dir, "member.json"),
    anonymousState: path.join(dir, "anonymous.json"),
    context: path.join(dir, "context.json"),
    json: path.join(dir, "sweep.json"),
    report: path.join(dir, "report.md"),
  };
}
