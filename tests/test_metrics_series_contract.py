from types import SimpleNamespace

import pytest

import metrics_store
import status_endpoint


@pytest.fixture
def metrics_db(tmp_path, monkeypatch):
    path = tmp_path / "metrics.db"
    metrics_store.init_db(path)
    monkeypatch.setattr(metrics_store, "DB_PATH", path)
    monkeypatch.setattr(metrics_store, "time", SimpleNamespace(time=lambda: 1800))
    return path


@pytest.mark.parametrize("tags_filter", [None, {}, {"key": "alpha"}])
@pytest.mark.parametrize(
    "op,expected",
    [("avg", 5.0), ("min", 2.0), ("max", 8.0),
     ("sum", 10.0), ("count", 2.0)],
)
def test_raw_series_uses_requested_operator(metrics_db, tags_filter, op, expected):
    with metrics_store.reader(metrics_db) as conn:
        metrics_store.record_samples(conn, [
            (1740, "fixture", '{"key":"alpha"}', 2.0),
            (1750, "fixture", '{"key":"alpha"}', 8.0),
            (1739, "fixture", '{"key":"alpha"}', 1000.0),
            (1750, "other_metric", '{"key":"alpha"}', 1000.0),
        ])
        conn.commit()
        actual = metrics_store.query_series(
            "fixture", since_s=60, points=1,
            tags_filter=tags_filter, op=op, conn=conn,
        )
    assert actual == [(1740, expected)]


def test_status_xid_fallback_keeps_peak_not_mean(metrics_db, monkeypatch):
    from gateway import metrics_writer

    monkeypatch.setattr(metrics_writer, "writer_stats", lambda: {})
    monkeypatch.setattr(metrics_store, "db_health", lambda: {})
    with metrics_store.reader(metrics_db) as conn:
        metrics_store.record_samples(conn, [
            (1740, "xid_109_60min", "", 2.0),
            (1750, "xid_109_60min", "", 8.0),
        ])
        conn.commit()
        payload = status_endpoint._metrics_section_queries(
            metrics_store, [], {"partitions": []}, [], conn=conn,
        )
    assert payload["trends"]["xid_109_hourly_24h"] == [(0, 8.0)]
