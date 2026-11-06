"""Start and run child processes without blocking the event loop.

asyncio.create_subprocess_exec starts a child through the loop. Under uvloop that forks the whole
gateway on the loop thread. The fork copies the page tables of all the memory the gateway holds,
about 0.05 ms per MB resident, and the loop waits for all of it. The stdlib spawn (subprocess.Popen)
uses vfork, which copies none of them, and takes about 5 ms at any size. So the spawn runs on a
worker thread, and the child's pipes stay on the loop, as asyncio's own pipe transports do. One
thread per child waits for its exit, as the stdlib's ThreadedChildWatcher does. Those threads are
daemons, so a child still running at shutdown does not hold the process open.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import subprocess
import threading
from typing import Any, Sequence

# A spawn is quick, so a small pool is enough and nothing waits on it for long.
_SPAWN_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="child-spawn")


class Child:
    """The part of asyncio.subprocess.Process the gateway uses, for a child that subprocess.Popen started."""

    def __init__(self, popen: subprocess.Popen, *, stdin, stdout, stderr, loop: asyncio.AbstractEventLoop) -> None:
        self._popen = popen
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.pid = popen.pid
        self._exited: asyncio.Future[int] = loop.create_future()
        threading.Thread(target=self._wait_for_exit, args=(loop,), name=f"child-wait-{popen.pid}", daemon=True).start()

    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    async def wait(self) -> int:
        # Shielded: a waiter that is cancelled does not stop the others from getting the exit code.
        return await asyncio.shield(self._exited)

    def send_signal(self, sig: int) -> None:
        self._popen.send_signal(sig)

    def terminate(self) -> None:
        self._popen.terminate()

    def kill(self) -> None:
        self._popen.kill()

    def _wait_for_exit(self, loop: asyncio.AbstractEventLoop) -> None:
        code = self._popen.wait()
        try:
            loop.call_soon_threadsafe(_settle, self._exited, code)
        except RuntimeError:  # the loop has closed, so nothing is waiting for the exit
            pass


async def start(argv: Sequence[str], **kwargs: Any) -> Child:
    """Start argv with the keyword arguments subprocess.Popen takes, and return it as a Child.

    The spawn runs on _SPAWN_POOL. A start that is cancelled while its spawn is still running kills
    the child that spawn makes, so no child outlives the request that asked for it.
    """
    loop = asyncio.get_running_loop()
    spawn = loop.run_in_executor(_SPAWN_POOL, functools.partial(subprocess.Popen, list(argv), **kwargs))
    try:
        popen = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        spawn.add_done_callback(_discard_child_of_cancelled_start)
        raise
    try:
        stdin = await _writer(loop, popen.stdin) if popen.stdin is not None else None
        stdout = await _reader(loop, popen.stdout) if popen.stdout is not None else None
        stderr = await _reader(loop, popen.stderr) if popen.stderr is not None else None
    except BaseException:
        _SPAWN_POOL.submit(_kill_and_reap, popen)
        raise
    return Child(popen, stdin=stdin, stdout=stdout, stderr=stderr, loop=loop)


async def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run on a worker thread, awaited.

    The timeout kills the child and raises subprocess.TimeoutExpired, as subprocess.run does. Cancelling
    the caller does not stop the child, as with asyncio's subprocess: the run is left to finish.
    """
    loop = asyncio.get_running_loop()
    answer: asyncio.Future = loop.create_future()

    def work() -> None:
        try:
            outcome = (True, subprocess.run(list(argv), **kwargs))
        except BaseException as error:  # noqa: BLE001 - handed to the awaiting caller
            outcome = (False, error)
        try:
            loop.call_soon_threadsafe(_settle, answer, outcome)
        except RuntimeError:  # the loop has closed, so nothing is waiting for the answer
            pass

    threading.Thread(target=work, name="child-run", daemon=True).start()
    ok, value = await answer
    if ok:
        return value
    raise value


async def _reader(loop: asyncio.AbstractEventLoop, pipe) -> asyncio.StreamReader:
    reader = asyncio.StreamReader(loop=loop)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader, loop=loop), pipe)
    return reader


async def _writer(loop: asyncio.AbstractEventLoop, pipe) -> asyncio.StreamWriter:
    protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader(loop=loop), loop=loop)
    transport, _ = await loop.connect_write_pipe(lambda: protocol, pipe)
    return asyncio.StreamWriter(transport, protocol, None, loop)


def _settle(future: asyncio.Future, value: Any) -> None:
    if not future.done():
        future.set_result(value)


def _discard_child_of_cancelled_start(spawn: asyncio.Future) -> None:
    if spawn.cancelled() or spawn.exception() is not None:
        return
    _SPAWN_POOL.submit(_kill_and_reap, spawn.result())


def _kill_and_reap(popen: subprocess.Popen) -> None:
    popen.kill()
    popen.wait()
