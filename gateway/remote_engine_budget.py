"""A single admission deadline and aggregate counters shared by all operation hops."""
from __future__ import annotations

import asyncio
import threading
import weakref
from contextlib import asynccontextmanager

from gateway.engine_protocol.errors import BudgetExceeded

_CAPACITIES = weakref.WeakKeyDictionary()
_CAPACITY_LOCK = threading.Lock()


class _Capacity:
    def __init__(self):
        self.active = 0
        self.changed = asyncio.Condition()

    async def acquire(self, limit):
        async with self.changed:
            await self.changed.wait_for(lambda: self.active < limit)
            self.active += 1

    async def release(self):
        async with self.changed:
            self.active -= 1
            self.changed.notify_all()


def service_capacity(bound):
    """Share the active-connection ceiling across admitted operations on this loop."""
    loop = asyncio.get_running_loop()
    key = (str(bound.factory.db_path) if bound.factory.db_path else id(bound.factory), bound.capability.service_id)
    with _CAPACITY_LOCK:
        capacities = _CAPACITIES.setdefault(loop, weakref.WeakValueDictionary())
        capacity = capacities.get(key)
        if capacity is None:
            capacity = _Capacity()
            capacities[key] = capacity
        return capacity


class OperationBudget:
    def __init__(self, cap, clock):
        self.limits = dict(cap.finite_budgets)
        self.deadline = cap.admitted_at + self.limits['total_s']
        self.clock = clock
        self.used = {'wire_bytes': 0, 'decoded_bytes': 0, 'polls': 0}

    def remaining(self):
        left = self.deadline - self.clock()
        if left <= 0:
            raise BudgetExceeded('total_s', 'The remote operation deadline expired.')
        return left

    def consume(self, name, amount):
        self.used[name] += amount
        if self.used[name] > self.limits[name]:
            raise BudgetExceeded(name, 'The remote operation exceeded its resource budget.')

    def check_headers(self, data):
        if len(data) > self.limits['header_bytes']:
            raise BudgetExceeded('header_bytes', 'The remote response headers exceeded their budget.')
        if data.count(b'\r\n') - 2 > self.limits['header_fields']:
            raise BudgetExceeded('header_fields', 'The remote response headers exceeded their budget.')


class BoundResponse:
    def __init__(self, inner, bound):
        self.inner, self.bound = inner, bound
        self.status, self.headers = inner.status, inner.headers
        self.closed = False
        self._closing = None
        bound.responses.add(self)

    async def chunks(self):
        iterator = self.inner.chunks().__aiter__()
        try:
            while True:
                self.bound.check()
                budget = self.bound.budget
                async with asyncio.timeout(min(budget.remaining(), budget.limits['idle_read_s'])):
                    try:
                        chunk = await anext(iterator)
                    except StopAsyncIteration:
                        return
                # Encoded responses are refused by the executor before consumption.
                # Count identity bytes independently of wire framing.
                budget.consume('decoded_bytes', len(chunk))
                self.bound.check()
                yield chunk
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._closing)

    async def _finish_close(self):
        if self.closed:
            return
        self.closed = True
        try:
            await self.inner.aclose()
        finally:
            self.bound.responses.discard(self)
            self.bound.clients.discard(getattr(self.inner, '_client', None))
            await self.bound.capacity.release()


class GuardedStream:
    """Wrap the actual socket: enforce authority before each write and cap raw reads."""
    def __init__(self, inner, bound, raw=None):
        self.inner, self.bound = inner, bound
        self.raw = raw or inner
        self.header = bytearray()
        self.headers_done = False
        self.received_any = False

    async def write(self, buffer, timeout=None):
        async with self.bound.fence():
            self.bound.check()
            async with asyncio.timeout(self.bound.budget.remaining()):
                await self.inner.write(buffer, timeout=min(timeout or self.bound.budget.remaining(), self.bound.budget.remaining()))

    async def read(self, max_bytes, timeout=None):
        self.bound.check()
        budget = self.bound.budget
        phase = 'idle_read_s' if self.received_any else 'first_byte_s'
        read_size = min(max_bytes, budget.limits['wire_bytes'] - budget.used['wire_bytes'] + 1)
        if not self.headers_done:
            read_size = min(read_size, budget.limits['header_bytes'] + 1 - len(self.header))
        async with asyncio.timeout(min(budget.remaining(), budget.limits[phase], timeout or budget.remaining())):
            data = await self.inner.read(max(1, read_size), timeout=timeout)
        budget.consume('wire_bytes', len(data))
        self.bound.check()
        self.received_any = self.received_any or bool(data)
        if not self.headers_done:
            self.header.extend(data)
            end = self.header.find(b'\r\n\r\n')
            if end >= 0:
                budget.check_headers(self.header[:end + 4])
                status = self.header.split(b'\r\n', 1)[0].split(b' ')
                if len(status) > 1 and status[1].startswith(b'1'):
                    raise BudgetExceeded('header_bytes', 'Remote informational responses are refused.')
                self.headers_done = True
                self.header.clear()
            elif len(self.header) > budget.limits['header_bytes']:
                budget.check_headers(self.header)
        return data

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.bound.check()
        budget = self.bound.budget
        async with self.bound.fence():
            async with asyncio.timeout(min(budget.remaining(), budget.limits['tls_s'])):
                stream = await self.inner.start_tls(ssl_context, server_hostname=server_hostname,
                                                    timeout=min(timeout or budget.remaining(), budget.limits['tls_s'], budget.remaining()))
        return GuardedStream(stream, self.bound, self.raw)

    async def aclose(self):
        # Connections are never reused. Close TCP without a TLS shutdown write
        # or a peer-controlled shutdown wait, including on overflow/cancellation.
        await asyncio.shield(self.raw.aclose())

    def get_extra_info(self, info):
        return self.inner.get_extra_info(info)
