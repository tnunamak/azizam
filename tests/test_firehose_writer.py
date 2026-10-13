"""The request counter appends to its log from a writer thread, never from the caller."""
import json
import threading
from unittest.mock import patch

import firehose_metrics


def test_record_request_persists_from_the_writer_thread(tmp_path):
    log = tmp_path / "key_activity.jsonl"
    writers = []
    real = firehose_metrics._append_record

    def recording(path, record):
        writers.append(threading.get_ident())
        return real(path, record)

    with patch.object(firehose_metrics, "_DEFAULT_CACHE_PATH", log), \
            patch.object(firehose_metrics, "_append_record", recording):
        firehose_metrics.record_request("key", "/v1/models", 200, 1.5)
        firehose_metrics.flush_persisted()

    assert writers and threading.get_ident() not in writers, "the log append ran on the calling thread"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["path"] == "/v1/models" and rows[-1]["status"] == 200


def test_replay_sees_every_record_queued_before_it(tmp_path):
    log = tmp_path / "key_activity.jsonl"
    with patch.object(firehose_metrics, "_DEFAULT_CACHE_PATH", log):
        for _ in range(25):
            firehose_metrics.record_request("replay-key", "/v1/models", 200, 1.0)
        firehose_metrics.flush_persisted()
        assert len(log.read_text(encoding="utf-8").splitlines()) == 25
