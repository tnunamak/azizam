"""Second review regressions: real media and deterministic resource races, no inference."""
import asyncio
import errno
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import proxy
from gateway import client_errors, custom_voices, stt_chunking as stt
from gateway.receipts import RouteReceipt
from tests.test_stt_d47_conformance import wav
from tests.test_stt_d47_review import chunk
from tests.test_stt_round4_planner import config


@pytest.mark.parametrize("rates", [(48000, 44100), (44100, 48000)])
@pytest.mark.parametrize("shared", [False, True])
def test_multitrack_decode_uses_probed_track_and_reservation(tmp_path, rates, shared):
    source = tmp_path / "two-tracks.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate={rates[0]}:duration=700", "-f", "lavfi", "-i",
        f"sine=frequency=880:sample_rate={rates[1]}:duration=700",
        "-map", "0:a", "-map", "1:a", "-ac", "2", "-c:a", "libmp3lame", "-q:a", "4",
        "-disposition:a:0", "0", "-disposition:a:1", "default", str(source)], check=True)
    # Independent oracle: decode the first audio track at the probe's native format.
    oracle = tmp_path / "oracle.pcm"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-map", "0:a:0",
        "-ar", str(rates[0]), "-ac", "2", "-f", "s16le", str(oracle)], check=True)
    expected = oracle.read_bytes()
    oracle.unlink()

    async def run():
        execution = config(tmp_path, target_seconds=300, threshold_seconds=600,
            max_segment_seconds=350, search_seconds=0,
            max_decoded_bytes=256 * 1024**2 if shared else 100 * 1024**2)
        gate = stt.SttResourceGate()
        lease = gate.acquire(request_id="multitrack", config=execution, upload_bytes=source.stat().st_size)
        sent = hashlib.sha256()
        frames = 0
        command = stt.run_media_command
        async def bounded_command(argv, **kwargs):
            result = await command(argv, **kwargs)
            assert lease.written_bytes <= lease._reserved_bytes
            return result
        async def post(path, _name):
            nonlocal frames
            import wave
            assert (tmp_path / "extraction-source.pcm").exists() == shared
            with wave.open(str(path)) as audio:
                assert (audio.getframerate(), audio.getnchannels()) == (rates[0], 2)
                frames += audio.getnframes()
                sent.update(audio.readframes(audio.getnframes()))
            return stt.UpstreamResponse(200, b'{"text":"piece"}', {})
        try:
            with patch.object(stt, "run_media_command", side_effect=bounded_command):
                outcome = await chunk(source, execution, post, resource_lease=lease)
            assert outcome.mode == "chunked"
            # Source fallback ends on the 16 kHz planning grid; its endpoint
            # can round down by less than one planning sample at the native rate.
            assert 0 <= len(expected) // 4 - frames <= (0 if shared else rates[0] // 16000)
            assert sent.digest() == hashlib.sha256(expected[:frames * 4]).digest()
        finally:
            lease.release()
        assert gate._reserved_bytes == gate.active_count == 0
    asyncio.run(run())


def test_resize_receipt_preserves_admission_queue_facts(tmp_path):
    gate = stt.SttResourceGate()
    lease = gate.acquire(request_id="resize", config=config(tmp_path), upload_bytes=1024**2)
    admitted = lease.decision
    try:
        lease.resize(3 * 1024**2)
        receipt = RouteReceipt(protocol="openai.audio", intent="audio.transcribe")
        receipt.resource_decision = stt.resource_evidence(lease, None)
        facts = receipt.to_dict()["resource_decision"]["facts"]
        queue_facts = [fact for fact in facts if fact["resource_class"] == "queue"]
        assert {fact["name"] for fact in queue_facts} == {"queue_limit", "queue_active"}
        assert queue_facts == [fact for fact in admitted.details.as_dict("accepted")["facts"]
                               if fact["resource_class"] == "queue"]
        assert next(fact["value"] for fact in facts if fact["name"] == "requested_disk_mb") == 3
    finally:
        lease.release()


def test_written_bytes_tolerates_unlink_between_is_file_and_stat(tmp_path):
    gate = stt.SttResourceGate()
    lease = gate.acquire(request_id="unlink", config=config(tmp_path))
    lease.workspace = tmp_path
    gone = tmp_path / "gone.pcm"
    gone.write_bytes(b"gone")
    (tmp_path / "kept.pcm").write_bytes(b"keep me")
    (tmp_path / "directory").mkdir()
    original = Path.stat
    calls = 0
    def racing_stat(path, *args, **kwargs):
        nonlocal calls
        if path == gone:
            calls += 1
            if calls == 2:
                gone.unlink()
        return original(path, *args, **kwargs)
    try:
        with patch.object(Path, "stat", racing_stat):
            assert lease.written_bytes == 7
    finally:
        lease.release()


@pytest.mark.parametrize("failure,status,code,retry", [
    ("disk", 503, "resource_admission_denied", "5"),
    ("disk900", 503, "resource_admission_denied", "5"),
    ("disk1024", 503, "resource_admission_denied", "5"),
    ("queue", 503, "resource_admission_denied", "5"),
    (stt.SttResourceLimitExceeded("Decoded audio exceeds the configured STT limit"),
     503, "backend_unavailable", None),
    (stt.SttDeadlineExceeded("deadline"), 504, "stt_request_deadline", None),
    (stt.SttBackendUnavailable("offline"), 503, "backend_unavailable", None),
    (stt.SttInvalidAudio("bad"), 400, "invalid_value", None),
])
def test_voice_failure_shape_and_retry_value(tmp_path, failure, status, code, retry):
    source = wav(tmp_path / "reference.wav", 0.2)
    if failure == "disk":
        with patch.object(stt, "_disk_free_bytes", return_value=0):
            with pytest.raises(stt.SttResourceLimitExceeded) as denied:
                stt.SttResourceGate().acquire(request_id="voice", config=config(tmp_path))
        failure = denied.value
    if failure in ("disk900", "disk1024", "queue"):
        gate = stt.SttResourceGate()
        leases = []
        with patch.object(stt, "_disk_free_bytes", return_value=4096 * 1024**2) as free:
            try:
                leases.append(gate.acquire(request_id="held", config=config(tmp_path)))
                if failure == "queue":
                    leases.append(gate.acquire(request_id="held2", config=config(tmp_path)))
                else:
                    free.return_value = int(failure[4:]) * 1024**2
                with pytest.raises(stt.SttResourceLimitExceeded) as denied:
                    gate.acquire(request_id="voice", config=config(tmp_path))
                failure = denied.value
            finally:
                for lease in leases:
                    lease.release()
    async def run():
        receipt = RouteReceipt(protocol="openai.audio", intent="audio.speak")
        with patch.dict(proxy.config["backends"], {"stt": {"host": "review.fk.test", "port": 9876}}), \
             patch.object(proxy, "_stt_adapter", return_value=type("Adapter", (), {"invoke": AsyncMock(side_effect=failure)})()), \
             patch.object(proxy, "_voice_store") as store, \
             patch.object(proxy, "_custom_voice_engines", return_value=["test"]), \
             patch.object(client_errors, "retry_after", wraps=client_errors.retry_after) as retry_after:
            store.return_value.get_voice.return_value = {"id": "test"}
            store.return_value.reference_path.return_value = source
            response = await proxy._apply_custom_voice({}, "test", {"custom_voices": True}, None, "omnivoice", receipt=receipt)
        if getattr(failure, "decision", None) is not None:
            reasons = receipt.to_dict()["resource_decision"]["reasons"]
            assert reasons == failure.decision.details.as_dict(failure.decision.status.value)["reasons"]
        assert response.status_code == status
        error = json.loads(response.body)["error"]
        assert error["code"] == code
        assert error["type"] == ("api_error" if status >= 500 else "invalid_request_error")
        assert response.headers.get("Retry-After") == retry
        if retry:
            assert "busy" in error["message"]
            retry_after.assert_called_once_with(retry)
        else:
            retry_after.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["transcriptions", "translations"])
@pytest.mark.parametrize("shortage", ["cap", "media_cap", "disk900", "disk1024", "queue"])
def test_stt_routes_distinguish_request_caps_from_busy_capacity(tmp_path, operation, shortage):
    from starlette.requests import Request
    from tests.support.access import access_context
    from tests.test_stt_chunking import _multipart_body

    async def run():
        mib = 1024**2
        execution = config(tmp_path, max_input_bytes=8 if shortage == "cap" else 512 * mib)
        gate = stt.SttResourceGate()
        leases = []
        cap = shortage in {"cap", "media_cap"}
        parser = stt.parse_bounded_multipart
        async def parse(*args, **kwargs):
            if shortage == "media_cap":
                raise stt.SttResourceLimitExceeded("Decoded audio exceeds the configured STT limit")
            return await parser(*args, **kwargs)
        body, content_type = _multipart_body(content=b"over cap!")
        delivered = False
        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Future()
        request = Request({"type": "http", "method": "POST", "path": f"/v1/audio/{operation}",
                           "headers": [(b"content-type", content_type.encode())]}, receive)
        receipt = RouteReceipt(protocol="openai.audio", intent="audio.transcribe")
        with patch.object(stt, "_disk_free_bytes", return_value=4096 * mib) as free, \
             patch.object(stt, "GLOBAL_STT_RESOURCE_GATE", gate), \
             patch.object(stt, "parse_bounded_multipart", side_effect=parse), \
             patch.object(proxy, "_service_availability_response", return_value=None), \
             patch.object(proxy.receipt_store, "begin"), \
             patch.object(proxy.receipt_store, "finalize") as finalize:
            try:
                if not cap:
                    leases.append(gate.acquire(request_id="held", config=execution))
                if shortage == "queue":
                    leases.append(gate.acquire(request_id="held2", config=execution))
                if shortage.startswith("disk"):
                    free.return_value = int(shortage[4:]) * mib
                response = await proxy.proxy_stt_request(request, access_context(), operation=operation,
                    execution_snapshot=execution, route_receipt=receipt)
                error = json.loads(response.body)["error"]
                assert response.status_code == (413 if cap else 503)
                assert error["code"] == ({"cap": "stt_upload_limit", "media_cap": "stt_resource_limit"}.get(
                    shortage, "resource_admission_denied"))
                assert error["type"] == ("invalid_request_error" if cap else "api_error")
                assert response.headers.get("Retry-After") == (None if cap else "5")
                finalize.assert_called_once()
                if not cap:
                    assert (receipt.status, receipt.metadata["terminal_reason"]) == ("rejected", "resource_admission_denied")
                    assert "busy" in error["message"]
                    reasons = receipt.to_dict()["resource_decision"]["reasons"]
                    assert any(r["code"] == ("queue_full" if shortage == "queue" else "capacity_insufficient")
                               and r["resource_class"] == ("queue" if shortage == "queue" else "disk") for r in reasons)
                assert gate.active_count == len(leases)
            finally:
                for lease in leases:
                    lease.release()
        assert gate.active_count == gate._reserved_bytes == 0
    asyncio.run(run())


@pytest.mark.parametrize("free_mib", [900, 1024])
def test_gate_classifies_reserved_disk_shortage_as_storage(tmp_path, free_mib):
    gate = stt.SttResourceGate()
    execution = config(tmp_path)
    with patch.object(stt, "_disk_free_bytes", return_value=4096 * 1024**2) as free:
        held = gate.acquire(request_id="held", config=execution)
        try:
            free.return_value = free_mib * 1024**2
            with pytest.raises(stt.SttStorageUnavailable) as caught:
                gate.acquire(request_id="denied", config=execution)
            assert any(r.code == "capacity_insufficient" and r.resource_class == "disk"
                       for r in caught.value.decision.details.reasons)
            assert gate.active_count == 1
        finally:
            held.release()


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EDQUOT])
def test_multipart_write_storage_shortage_is_retryable_and_removes_partial_file(tmp_path, error_number):
    from starlette.requests import Request
    from tests.test_stt_chunking import _multipart_body

    async def run():
        execution = config(tmp_path, spool_dir=str(tmp_path))
        body, content_type = _multipart_body(content=b"audio")
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request = Request({"type": "http", "headers": [(b"content-type", content_type.encode())]}, receive)
        files = []
        async def partial_write(upload, data):
            upload.file.write(data[:1])
            files.append(upload.file)
            raise OSError(error_number, "synthetic storage shortage")
        with patch.object(stt.UploadFile, "write", partial_write):
            with pytest.raises(stt.SttStorageUnavailable) as caught:
                await stt.parse_bounded_multipart(request, execution)
        assert caught.value.__cause__.errno == error_number
        assert files and all(handle.closed for handle in files)
        assert list(tmp_path.iterdir()) == []
        response = proxy._stt_resource_limit_response(caught.value,
            RouteReceipt(protocol="openai.audio", intent="audio.transcribe"))
        assert response.status_code == 503
        assert json.loads(response.body)["error"]["code"] == "resource_admission_denied"
        assert response.headers["Retry-After"] == "5"
    asyncio.run(run())
