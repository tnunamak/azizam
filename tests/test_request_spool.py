"""Request-local spool lifecycle with invented bytes and pytest-owned storage."""

import asyncio
import stat
from types import SimpleNamespace

import pytest

from gateway import request_spool as spool_module
from gateway.request_spool import FileRequestSpool, request_scope


async def _chunks(*parts):
    for part in parts:
        yield part


def _spool(root, **kwargs):
    return FileRequestSpool(root, max_bytes=4, measure=kwargs.pop("measure", lambda *_: "text/plain"), **kwargs)


async def _stage(spool, *parts):
    return await spool.stage("sample", _chunks(*parts), filename=None, wire_media_type=None)


def _request():
    return SimpleNamespace(state=SimpleNamespace())


def _files(root):
    return sorted(path for path in (root / "staging").glob("*/*") if path.is_file())


def test_normal_release_and_handler_exception_remove_staged_file(tmp_path):
    async def exercise():
        with request_scope(_request(), _spool(tmp_path)) as spool:
            await _stage(spool, b"one")
            assert len(_files(tmp_path)) == 1
        assert _files(tmp_path) == []
        spool.release()
        with pytest.raises(RuntimeError, match="handler stopped"):
            with request_scope(_request(), _spool(tmp_path)) as spool:
                await _stage(spool, b"two")
                assert len(_files(tmp_path)) == 1
                raise RuntimeError("handler stopped")
        assert _files(tmp_path) == []

    asyncio.run(exercise())


def test_reader_cancellation_removes_partial_file_before_release(tmp_path):
    async def exercise():
        ready = asyncio.Event()
        async def paused():
            yield b"one"
            ready.set()
            await asyncio.Event().wait()

        with request_scope(_request(), _spool(tmp_path)) as spool:
            task = asyncio.create_task(spool.stage("sample", paused(), filename=None, wire_media_type=None))
            await ready.wait()
            assert len(_files(tmp_path)) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert _files(tmp_path) == []

    asyncio.run(exercise())


def test_write_failure_removes_partial_file(tmp_path, monkeypatch):
    class FailingWriter:
        def __init__(self, path, mode):
            self.file = open(path, mode)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.file.close()

        def write(self, chunk):
            self.file.write(chunk)
            raise OSError("fixture write failed")

    monkeypatch.setattr(spool_module, "open", FailingWriter, raising=False)
    with request_scope(_request(), _spool(tmp_path)) as spool:
        with pytest.raises(OSError, match="fixture write failed"):
            asyncio.run(_stage(spool, b"one"))
        assert _files(tmp_path) == []


def test_size_limit_removes_partial_file(tmp_path):
    with request_scope(_request(), _spool(tmp_path)) as spool:
        result = asyncio.run(_stage(spool, b"abc", b"de"))
        assert isinstance(result, spool_module.RunRefused)
        assert result.http_status == 413
        assert _files(tmp_path) == []


def test_measurement_exception_removes_staged_file_before_release(tmp_path):
    def unavailable(*_):
        raise RuntimeError("fixture measurement failed")

    with request_scope(_request(), _spool(tmp_path, measure=unavailable)) as spool:
        with pytest.raises(RuntimeError, match="fixture measurement failed"):
            asyncio.run(_stage(spool, b"one"))
        assert _files(tmp_path) == []


def test_concurrent_requests_get_distinct_paths_and_inherit_normal_file_mode(tmp_path):
    control = tmp_path / "control"
    with control.open("xb"):
        pass

    async def exercise():
        with request_scope(_request(), _spool(tmp_path)) as left:
            with request_scope(_request(), _spool(tmp_path)) as right:
                first, second = await asyncio.gather(_stage(left, b"one"), _stage(right, b"two"))
                assert first.spool_key != second.spool_key
                paths = _files(tmp_path)
                assert len({path.parent for path in paths}) == 2
                assert {path.read_bytes() for path in paths} == {b"one", b"two"}
                assert all(stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(control.stat().st_mode) for path in paths)
            assert [path.read_bytes() for path in _files(tmp_path)] == [b"one"]
        assert _files(tmp_path) == []

    asyncio.run(exercise())
