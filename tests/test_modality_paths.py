"""The mechanical route inventory stays aligned with the completed audio port."""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_generated_audio_row_matches_the_documented_declared_path():
    report = runpy.run_path(str(ROOT / 'scripts/research/modality_paths.py'))['inventory']()
    row = next(row for row in report['routes'] if row['route'] == '/v1/audio/generate')
    assert row['old_path'] == 'proxy.py:proxy_audio_generate'
    assert row['dispatch'] == '_PreparedAudioGeneration.run -> declared_operation_job.run (results -> publication -> deliver)'
    assert row['generic_ready'] == 'Yes: ported; storage before publication, delivery after publication'
    assert row['old_new_parity'] is True
    assert row['existing_wire_test'] == 'test_w4_audio_declared.py'
    assert 'declared_operation_job.run' in report['symbols']['proxy.py:_PreparedAudioGeneration.run']['calls']
    documented = (f"| `{row['route']}` | `{row['old_path']}`; {row['dispatch']} | {row['generic_ready']} | "
                  f"Yes: {row['existing_wire_test']} (transition commits) |")
    assert documented in (ROOT / 'docs/verification/W4_MODALITY_PORTS.md').read_text()
