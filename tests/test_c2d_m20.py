import importlib
import yaml

def test_direct_size_uses_release_multiple(monkeypatch):
    from gateway import video_grid as grid
    assert grid.SizeSpec('128x128','256x256').multiple == 32
    original = yaml.safe_load
    def load(text):
        doc = original(text)
        if isinstance(doc, dict) and 'workflow_fact_source' in doc:
            doc['workflow_fact_source']['facts']['size']['multiple']['default'] = 16
        return doc
    with monkeypatch.context() as patch:
        patch.setattr(yaml, 'safe_load', load)
        importlib.reload(grid)
        try:
            assert grid.SizeSpec('128x128','256x256').multiple == 16
            assert grid.configured_facts({}, {}).size.multiple == 16
            assert grid.video_facts({'capability':'sample','sample':{'size':{'default':'128x128','max':'256x256'}}}).size.multiple == 16
        finally:
            patch.undo()
            importlib.reload(grid)
