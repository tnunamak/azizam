"""Status metrics payload parity for shared and per-query SQLite readers."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import metrics_store
import status_endpoint


class TestStatusMetricsReader(unittest.TestCase):
    def test_shared_reader_matches_old_queries_at_frozen_time(self):
        now = 1_800_000_000
        gpus = [{"index": 0}, {"index": 1}, {}]
        disk = {"partitions": [
            {"target": "/", "avail_bytes": 2_000_000},
            {"target": "/data", "avail_bytes": 8_000_000},
            {},
        ]}
        backends = [
            {"key": "alpha", "name": "Alpha", "role": "required"},
            {"key": "beta", "name": "Beta", "role": "optional"},
        ]
        cases = [
            ("gpu_temp_c", {"gpu": 0}), ("gpu_temp_c", {"gpu": 1}),
            ("vram_pct", {"gpu": 0}), ("vram_pct", {"gpu": 1}),
            ("cpu_temp_c", None), ("load_avg_1min", None),
            ("disk_avail_bytes", {"mount": "/"}),
            ("disk_avail_bytes", {"mount": "/data"}),
            ("xid_109_24h", None), ("xid_109_60min", None),
            ("request", {"key": "alpha"}), ("request", {"key": "beta"}),
        ]
        for backend in backends:
            tags = status_endpoint._backend_history_tags(backend)
            cases.extend((metric, tags) for metric in (
                "service_up", "service_status_code", "service_latency_ms",
            ))
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "metrics.db"
            metrics_store.init_db(db)
            health = metrics_store.db_health
            with metrics_store.reader(db) as conn:
                for i, (metric, tags) in enumerate(cases):
                    for j, age in enumerate((
                        0, 299, 300, 301, 3599, 3600, 3601,
                        86399, 86400, 86401, 604800, 604801,
                    )):
                        value = float((i + 1) * (j + 1))
                        metrics_store.record_sample(conn, now - age, metric, value, tags)
                        # Mixed rollup/raw-only metrics exercise both fallback paths.
                        if i % 2 == 0:
                            conn.execute(
                                "INSERT INTO samples_1min VALUES (?,?,?,?,?,?,?)",
                                (now - age, metric, metrics_store._serialize_tags(tags),
                                 value, value - 1, value + 1, j + 1),
                            )
                conn.execute("INSERT INTO samples VALUES (?,?,?,?)",
                             (now, "request", "invalid-json", 1))
                conn.commit()
            with (
                patch.object(metrics_store, "DB_PATH", db),
                patch.object(metrics_store.time, "time", return_value=now),
                patch.object(metrics_store, "db_health", side_effect=lambda: health(db)),
                patch("gateway.metrics_writer.writer_stats", return_value={"pending": 0}),
                patch.object(metrics_store, "reader", wraps=metrics_store.reader) as reader,
            ):
                for empty in (False, True):
                    with self.subTest(empty=empty):
                        if empty:
                            with metrics_store.reader(db) as conn:
                                conn.execute("DELETE FROM samples")
                                conn.execute("DELETE FROM samples_1min")
                                conn.commit()
                        reader.reset_mock()
                        # No connection reproduces the original per-query strategy.
                        expected = status_endpoint._metrics_section_queries(
                            metrics_store, gpus, disk, backends,
                        )
                        old_opens = reader.call_count
                        reader.reset_mock()
                        with patch.object(
                            metrics_store, "query_aggregate",
                            wraps=metrics_store.query_aggregate,
                        ) as aggregate:
                            actual = status_endpoint._metrics_section(gpus, disk, backends)
                        self.assertEqual(json.dumps(actual), json.dumps(expected))
                        self.assertGreater(old_opens, 20)
                        # One reader for all metrics queries, one for DB health.
                        self.assertEqual(reader.call_count, 2)
                        shared = aggregate.call_args_list[0].kwargs["conn"]
                        self.assertIsNotNone(shared)
                        self.assertTrue(all(
                            call.kwargs["conn"] is shared for call in aggregate.call_args_list
                        ))
                        with self.assertRaises(sqlite3.ProgrammingError):
                            shared.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
