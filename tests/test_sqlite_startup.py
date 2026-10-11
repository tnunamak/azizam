from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest
from gateway import media_jobs, receipt_store, admin_store
import responses_store
import metrics_store

STORES = [media_jobs, receipt_store, admin_store, responses_store, metrics_store]

@pytest.mark.parametrize('store', STORES)
def test_locked_database_keeps_history(store, tmp_path):
    path = tmp_path / 'history.db'
    owner = sqlite3.connect(path)
    owner.execute('CREATE TABLE sentinel (value TEXT)')
    owner.execute("INSERT INTO sentinel VALUES ('keep')")
    owner.commit()
    owner.execute('BEGIN EXCLUSIVE')
    original = path.read_bytes()
    connect = sqlite3.connect
    def fast_connect(*args, **kwargs):
        kwargs['timeout'] = 0.01
        return connect(*args, **kwargs)
    try:
        with patch.object(sqlite3, 'connect', fast_connect):
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                store.init_db(path)
        assert path.read_bytes() == original
        assert not list(tmp_path.glob('*.broken-*'))
    finally:
        owner.rollback()
        owner.close()
    store.init_db(path)
    with connect(path) as conn:
        assert conn.execute('SELECT value FROM sentinel').fetchall() == [('keep',)]

@pytest.mark.parametrize('store', STORES)
def test_other_operational_error_preserves_file(store, tmp_path):
    path = tmp_path / 'history.db'
    path.write_bytes(b'existing data')
    with patch.object(sqlite3, 'connect', side_effect=sqlite3.OperationalError('disk I/O error')) as connect:
        with pytest.raises(sqlite3.OperationalError, match='disk I/O error'):
            store.init_db(path)
        assert connect.call_count >= 2
    assert path.read_bytes() == b'existing data'
    assert not list(tmp_path.glob('*.broken-*'))

@pytest.mark.parametrize('store', STORES)
def test_corruption_is_preserved_before_replacement(store, tmp_path):
    path = tmp_path / 'history.db'
    path.write_bytes(b'not a sqlite database')
    store.init_db(path)
    copies = list(tmp_path.glob('history.db.broken-*/history.db'))
    assert len(copies) == 1
    assert copies[0].read_bytes() == b'not a sqlite database'
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)

@pytest.mark.parametrize('store', STORES)
def test_failed_quarantine_never_deletes_database(store, tmp_path):
    path = tmp_path / 'history.db'
    path.write_bytes(b'not a sqlite database')
    with patch.object(Path, 'rename', side_effect=PermissionError('cannot move')):
        with pytest.raises(PermissionError, match='cannot move'):
            store.init_db(path)
    assert path.read_bytes() == b'not a sqlite database'


def test_quarantine_preserves_journals(tmp_path):
    from gateway.sqlite_startup import quarantine_database
    path = tmp_path / 'history.db'
    originals = {suffix: f'keep {suffix}'.encode() for suffix in ('', '-wal', '-shm', '-journal')}
    for suffix, content in originals.items():
        Path(f'{path}{suffix}').write_bytes(content)
    quarantine_database(path, 'confirmed corruption')
    destination, = tmp_path.glob('history.db.broken-*')
    for suffix, content in originals.items():
        assert (destination / f'history.db{suffix}').read_bytes() == content


def test_startup_retries_after_a_transient_lock(tmp_path):
    from gateway import sqlite_startup
    path = tmp_path / 'history.db'
    owner = sqlite3.connect(path)
    owner.execute('CREATE TABLE sentinel (value TEXT)')
    owner.commit()
    owner.execute('BEGIN EXCLUSIVE')
    connect = sqlite3.connect
    def fast_connect(*args, **kwargs):
        kwargs['timeout'] = 0.01
        return connect(*args, **kwargs)
    try:
        with patch.object(sqlite3, 'connect', fast_connect), patch.object(sqlite_startup.time, 'sleep', side_effect=lambda _: owner.rollback()) as retry:
            media_jobs.init_db(path)
        retry.assert_called_once()
        assert not list(tmp_path.glob('*.broken-*'))
    finally:
        owner.close()


def test_integrity_check_failure_is_quarantined(tmp_path):
    from gateway import sqlite_startup
    from unittest.mock import MagicMock
    path = tmp_path / 'history.db'
    path.write_bytes(b'original')
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [('row 1 missing from index',)]
    with patch.object(sqlite3, 'connect', return_value=conn):
        sqlite_startup.check_database(path)
    conn.close.assert_called_once()
    preserved, = tmp_path.glob('*.broken-*/history.db')
    assert preserved.read_bytes() == b'original'


def test_partial_quarantine_blocks_next_startup(tmp_path):
    from gateway.sqlite_startup import quarantine_database
    path = tmp_path / 'history.db'
    path.write_bytes(b'corrupt database')
    journal = Path(f'{path}-wal')
    journal.write_bytes(b'valuable journal')
    rename = Path.rename
    def fail_journal(source, destination):
        if source == journal:
            raise PermissionError('journal busy')
        return rename(source, destination)
    with patch.object(Path, 'rename', fail_journal):
        with pytest.raises(PermissionError, match='journal busy'):
            quarantine_database(path, 'confirmed corruption')
    with pytest.raises(RuntimeError, match='Unfinished database quarantine'):
        media_jobs.init_db(path)
    assert not path.exists()
    assert journal.read_bytes() == b'valuable journal'
    preserved, = tmp_path.glob('*.broken-*/history.db')
    assert preserved.read_bytes() == b'corrupt database'


def test_stale_metrics_marker_does_not_quarantine_healthy_history(tmp_path):
    path = tmp_path / 'metrics.db'
    metrics_store.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE sentinel (value TEXT)')
        conn.execute("INSERT INTO sentinel VALUES ('keep')")
    metrics_store._integrity_marker(path).write_text('old failure')
    metrics_store.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT value FROM sentinel').fetchall() == [('keep',)]
    assert not list(tmp_path.glob('*.broken-*'))
