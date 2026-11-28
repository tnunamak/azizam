"""Run a burst of test-client requests on one event loop and one portal thread.

A Starlette ``TestClient`` used outside ``with`` starts a portal (a thread with a new event loop) for every request and
stops it after the answer: about 10 to 30 ms of thread and loop setup and shutdown per request. A sweep that reads every
route as three clients sends over a thousand requests. ``one_loop`` gives the clients one portal for the length of the
block, and does not run the app lifespan (the harness serves without it, as the ``gateway`` fixture says).
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator


@contextlib.contextmanager
def one_loop(*clients: Any) -> Iterator[None]:
    from anyio.from_thread import start_blocking_portal

    with start_blocking_portal() as portal:
        shared = [client for client in clients if client.portal is None]
        for client in shared:
            client.portal = portal
        try:
            yield
        finally:
            for client in shared:
                client.portal = None
