import React from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const network = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("@/lib/network-error", () => ({ networkFetch: network.fetch }));
vi.mock("@/app/try/mesh-viewer", () => ({ MeshViewer: () => null }));

import ModelPreview from "./model-preview";

type Pending = {
  signal: AbortSignal;
  promise: Promise<Response>;
  resolve: (response: Response) => void;
  reject: (reason: unknown) => void;
};
let pending: Pending[];

beforeEach(() => {
  pending = [];
  network.fetch.mockReset();
  network.fetch.mockImplementation((_src: string, init?: RequestInit) => {
    if (!init?.signal) throw new Error("download must carry an abort signal");
    let resolve!: Pending["resolve"];
    let reject!: Pending["reject"];
    const promise = new Promise<Response>((yes, no) => {
      resolve = yes;
      reject = no;
    });
    pending.push({ signal: init.signal, promise, resolve, reject });
    return promise;
  });
});

afterEach(() => {
  cleanup();
  network.fetch.mockReset();
});

async function rejectAborted(request: Pending) {
  await act(async () => {
    request.reject(new DOMException("Aborted", "AbortError"));
    await request.promise.catch(() => undefined);
  });
}

async function answerNotOk(request: Pending) {
  await act(async () => {
    request.resolve({ ok: false } as Response);
    await request.promise;
  });
}

describe("ModelPreview download lifecycle", () => {
  it("aborts the old download and starts a new one when src changes without reporting failure", async () => {
    const onFailed = vi.fn();
    const view = render(<ModelPreview src="/first.glb" title="Model" onFailed={onFailed} />);
    expect(network.fetch).toHaveBeenCalledTimes(1);
    const first = pending[0]!;
    expect(first.signal.aborted).toBe(false);

    view.rerender(<ModelPreview src="/second.glb" title="Model" onFailed={onFailed} />);
    expect(first.signal.aborted).toBe(true);
    expect(network.fetch).toHaveBeenCalledTimes(2);
    expect(network.fetch).toHaveBeenNthCalledWith(2, "/second.glb", { signal: pending[1]!.signal });
    expect(pending[1]!.signal).not.toBe(first.signal);
    expect(pending[1]!.signal.aborted).toBe(false);
    await rejectAborted(first);
    expect(onFailed).not.toHaveBeenCalled();
  });

  it("aborts on unmount and ignores a later abort rejection", async () => {
    const onFailed = vi.fn();
    const view = render(<ModelPreview src="/pending.glb" title="Model" onFailed={onFailed} />);
    expect(network.fetch).toHaveBeenCalledTimes(1);
    const request = pending[0]!;
    expect(request.signal.aborted).toBe(false);
    view.unmount();
    expect(request.signal.aborted).toBe(true);
    expect(onFailed).not.toHaveBeenCalled();
    await rejectAborted(request);
    expect(onFailed).not.toHaveBeenCalled();
  });

  it("reports a live non-ok response exactly once", async () => {
    const onFailed = vi.fn();
    render(<ModelPreview src="/failed.glb" title="Model" onFailed={onFailed} />);
    expect(network.fetch).toHaveBeenCalledTimes(1);
    expect(pending[0]!.signal.aborted).toBe(false);
    await answerNotOk(pending[0]!);
    expect(onFailed).toHaveBeenCalledTimes(1);
  });

  it("does not refetch for a new onFailed prop and reports to the current callback", async () => {
    const previous = vi.fn(), current = vi.fn();
    const view = render(<ModelPreview src="/same.glb" title="Model" onFailed={previous} />);
    expect(network.fetch).toHaveBeenCalledTimes(1);
    const request = pending[0]!;
    view.rerender(<ModelPreview src="/same.glb" title="Model" onFailed={current} />);
    expect(network.fetch).toHaveBeenCalledTimes(1);
    expect(request.signal.aborted).toBe(false);
    await answerNotOk(request);
    expect(previous).not.toHaveBeenCalled();
    expect(current).toHaveBeenCalledTimes(1);
  });
});
