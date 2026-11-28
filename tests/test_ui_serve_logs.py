"""A browser test server that fails readiness keeps its private logs for diagnosis."""
import stat

from scripts.ui.serve_logs import retain_logs


def test_logs_are_copied_privately_under_the_destination(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / '18180.log').write_text('gateway said why\n')
    (state / '18181.log').write_text('engine said why\n')
    (state / 'not-a-log.txt').write_text('skip me\n')
    kept = retain_logs(state, tmp_path / 'failed')
    assert kept.parent == tmp_path / 'failed'
    assert stat.S_IMODE(kept.stat().st_mode) == 0o700
    names = sorted(p.name for p in kept.iterdir())
    assert names == ['18180.log', '18181.log']
    assert (kept / '18180.log').read_text() == 'gateway said why\n'
    for log in kept.iterdir():
        assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_two_failures_keep_two_directories(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / '18180.log').write_text('x\n')
    first = retain_logs(state, tmp_path / 'failed')
    second = retain_logs(state, tmp_path / 'failed')
    assert first != second and first.is_dir() and second.is_dir()

def test_retention_does_not_follow_symlinked_logs(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('synthetic outside data')
    (state / 'outside.log').symlink_to(outside)
    (state / '18180.log').write_text('process output')

    kept = retain_logs(state, tmp_path / 'failed')

    assert sorted(p.name for p in kept.iterdir()) == ['18180.log']
