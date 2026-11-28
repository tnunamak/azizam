"""The shared YAML parse cache: a changed file parses again, and callers never share a document."""
from __future__ import annotations

import uuid

from gateway import yaml_files


def test_a_changed_file_parses_again_even_at_the_same_size(tmp_path) -> None:
    path = tmp_path / "doc.yml"
    path.write_text("a: 1\n", encoding="utf-8")
    assert yaml_files.load_yaml(path) == {"a": 1}
    path.write_text("a: 2\n", encoding="utf-8")   # same size, possibly the same timestamp tick
    assert yaml_files.load_yaml(path) == {"a": 2}


def test_each_caller_gets_its_own_copy(tmp_path) -> None:
    path = tmp_path / "doc.yml"
    path.write_text("a: {b: [1, 2]}\n", encoding="utf-8")
    first = yaml_files.load_yaml(path)
    first["a"]["b"].append(3)
    first["c"] = 4
    assert yaml_files.load_yaml(path) == {"a": {"b": [1, 2]}}


def test_unchanged_file_is_parsed_once(tmp_path, monkeypatch) -> None:
    path = tmp_path / "doc.yml"
    key = f"k{uuid.uuid4().hex}"   # content no earlier call has cached
    path.write_text(f"{key}: 1\n", encoding="utf-8")
    calls = []
    real = yaml_files.yaml.safe_load
    monkeypatch.setattr(yaml_files.yaml, "safe_load", lambda text: calls.append(1) or real(text))
    for _ in range(3):
        assert yaml_files.load_yaml(path) == {key: 1}
    assert len(calls) == 1
