"""Dispatch compatibility is contextual data, not global alias equivalence."""
import json
from pathlib import Path
from gateway.operation_catalog import load, project_dispatch_task_aliases

ROOT = Path(__file__).resolve().parents[1]


def test_dispatch_relation_can_change_without_code(tmp_path):
    doc = json.loads((ROOT / 'registry/operations.yml').read_text())
    doc['relations'].append({'kind': 'dispatch', 'word': 'new-task', 'aliases': ['image.generate']})
    path = tmp_path / 'operations.yml'
    path.write_text(json.dumps(doc))
    assert project_dispatch_task_aliases('new-task', catalog=load(path)) == {'image.generate'}


def test_shipped_dispatch_parity():
    catalog = load(ROOT / 'registry/operations.yml')
    expected = {'chat': {'chat', 'chat.generate'}, 'images': {'image', 'image.generate'},
                'speech': {'speech', 'speech.synthesis'}, 'embeddings': {'embeddings', 'text.embeddings'}}
    for word, aliases in expected.items():
        assert project_dispatch_task_aliases(word, catalog=catalog) == aliases
    for word in ('unknown', 'image', 'chat.generate', ''):
        assert project_dispatch_task_aliases(word, catalog=catalog) == {word}


def test_dispatch_reader_refuses_catalog_without_dispatch_rows():
    from gateway.operation_catalog import Catalog, CatalogError
    import pytest
    with pytest.raises(CatalogError, match='dispatch'):
        project_dispatch_task_aliases('chat', catalog=Catalog({}, ()))
