"""Declared control operations for selection and residency.

Only an explicit operation row can cause a control request. This small runtime
keeps v2 control data separate from the legacy activation driver.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from gateway.engine_contract_view import observations_config, residency_config
from gateway.engine_observations import ObservationReader
from gateway.engine_residency import ResidencyController
from gateway.engine_switch import SwitchController
from gateway.swap_drain import SwapDrain


class EngineControlRuntime:
    def __init__(self, service: Mapping[str, Any], *, drain: SwapDrain,
                 lock_for, observations: ObservationReader | None = None,
                 client_factory=httpx.AsyncClient) -> None:
        self.service = service
        self._drain = drain
        self._lock_for = lock_for
        self._observations = observations or ObservationReader()
        self._client_factory = client_factory

    def _operations(self) -> Mapping[str, Any]:
        config = self.service.get("config") or {}
        operations = self.service.get("protocol_operations") or config.get("protocol_operations")
        return operations if isinstance(operations, Mapping) else {}

    async def observe(self, name: str):
        return await self._observations.read({**self.service,
            "observations": observations_config(self.service)}, name)

    async def invoke(self, name: str, args: Mapping[str, Any]) -> None:
        operation = self._operations().get(name)
        if not isinstance(operation, Mapping):
            raise ValueError(f"undeclared control operation: {name}")
        method = operation.get("method", "POST")
        path = operation.get("path")
        connection = self.service.get("connection") or {}
        address = connection.get("address") if isinstance(connection, Mapping) else None
        if method not in {"GET", "POST", "PUT", "DELETE"} or not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("control operation needs an HTTP method and absolute path")
        if not isinstance(address, str) or urlsplit(address).scheme not in {"http", "https"}:
            raise ValueError("control operation needs an HTTP connection")
        url = urljoin(address.rstrip("/") + "/", path.lstrip("/"))
        origin = urlsplit(address)
        target = urlsplit(url)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
            raise ValueError("control operation cannot cross origins")
        self._check_paths(args)
        timeout_s = max(0.01, min(float(operation.get("timeout_s", 10)), 30))
        async with asyncio.timeout(timeout_s):
            async with self._client_factory(follow_redirects=False, trust_env=False) as client:
                async with client.stream(method, url, json=dict(args) if method != "GET" else None,
                                         timeout=timeout_s) as response:
                    if response.is_redirect or not 200 <= response.status_code < 300:
                        raise ValueError("control operation failed")
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 1048576:
                            raise ValueError("control response exceeded size limit")

    def _check_paths(self, args: Mapping[str, Any]) -> None:
        config = self.service.get("config") or {}
        roots = self.service.get("control_file_roots") or config.get("control_file_roots") or []
        if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
            raise ValueError("invalid control file roots")
        allowed = [Path(root).expanduser().resolve() for root in roots]
        def inspect(value: Any) -> None:
            if isinstance(value, Mapping):
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)
            elif isinstance(value, str) and Path(value).is_absolute():
                path = Path(value).expanduser().resolve()
                if not any(path.is_relative_to(root) for root in allowed):
                    raise ValueError("control file path is outside owner allowlisted roots")

        inspect(args)

    async def select(self, offering_id: str, selection: Mapping[str, Any], *,
                     on_acquired=None, on_release=None, prepare=None):
        controller = SwitchController(drain=self._drain, invoke=self.invoke,
            observe=self.observe, lock_for=self._lock_for)
        return await controller.select(self.service, offering_id, selection,
                                       on_acquired=on_acquired, on_release=on_release,
                                       prepare=prepare)

    async def load(self) -> bool:
        controller = ResidencyController(invoke=self.invoke, observe=self.observe)
        return await controller.load({**self.service, "residency": residency_config(self.service)})

    async def unload(self) -> bool:
        controller = ResidencyController(invoke=self.invoke, observe=self.observe)
        return await controller.unload({**self.service, "residency": residency_config(self.service)})

    async def sleep(self) -> bool:
        controller = ResidencyController(invoke=self.invoke, observe=self.observe)
        return await controller.sleep({**self.service, "residency": residency_config(self.service)})

    async def wake(self) -> bool:
        controller = ResidencyController(invoke=self.invoke, observe=self.observe)
        return await controller.wake({**self.service, "residency": residency_config(self.service)})
