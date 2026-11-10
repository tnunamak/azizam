"""Recipe descriptors keep source selection in data; all I/O is local and stubbed."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest
import yaml

from gateway import stt_samples
from tests.test_stt_samples import _recipe_case, _sample, _source


@pytest.fixture(autouse=True)
def _no_downloads_or_conversion(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("This test must stub downloads and conversion.")

    monkeypatch.setattr(stt_samples, "fetch_sources", refuse)
    monkeypatch.setattr(stt_samples, "convert_audio", refuse)
    monkeypatch.setattr(stt_samples, "_download", refuse)


def _document(kind="earnings21"):
    bodies, params, expected = _recipe_case(kind)
    descriptor = {
        "algorithm": kind,
        "required_source_params": [name for name in params if name != "word_members"],
        "required_params": ["word_members"] if "word_members" in params else [],
        "conversion_source_param": "audio",
    }
    sample = _sample([_source(name, body) for name, body in bodies.items()], kind="unfamiliar", params=params)
    return {"recipes": {"schema_version": 1, "entries": {"unfamiliar": descriptor}}, "samples": [sample]}, bodies, expected


def _load(tmp_path, document):
    path = tmp_path / "samples.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return stt_samples.load_manifest(path)


@pytest.mark.parametrize("kind", ["ami-words", "icsi-words", "earnings21"])
def test_recipe_source_role_is_declared_once(tmp_path, monkeypatch, kind):
    document, bodies, expected = _document(kind)
    descriptor = document["recipes"]["entries"]["unfamiliar"]
    descriptor["required_source_params"][0] = "payload_blob"
    descriptor["conversion_source_param"] = "payload_blob"
    sample = document["samples"][0]
    sample["recipe"]["params"]["payload_blob"] = "picked-file"
    del sample["recipe"]["params"]["audio"]
    sample["recipe"]["params"]["cut_seconds"] = 0.6
    sample["sources"][0]["name"] = "picked-file"
    bodies["picked-file"] = bodies.pop("audio")
    # A misleading old source name must not drive either selection.
    decoy = _source("audio", b"decoy")
    decoy["audio_rights"]["status"] = "not-applicable"
    sample["sources"].append(decoy)
    bodies["audio"] = b"decoy"

    sample["sources"][0]["audio_rights"]["status"] = "not-applicable"
    with pytest.raises(stt_samples.SampleError, match="audio_rights"):
        _load(tmp_path, document)
    sample["sources"][0]["audio_rights"]["status"] = "stated"
    spec = _load(tmp_path, document)[0]
    assert spec.recipe_descriptor.conversion_source_param == "payload_blob"

    downloaded, converted = [], []
    files = {}
    for name, body in bodies.items():
        files[name] = tmp_path / name
        files[name].write_bytes(body)

    def download(selected, cache_dir, **kwargs):
        downloaded.append(selected)
        return files

    def convert(source, target, *, cut_seconds=None):
        converted.append((source, target, cut_seconds))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"stubbed conversion")
        return 0.6

    def no_client():
        pytest.fail("A stubbed download must not create a client.")

    monkeypatch.setattr(stt_samples, "fetch_sources", download)
    monkeypatch.setattr(stt_samples, "convert_audio", convert)
    prepared = stt_samples.fetch_sample(spec, tmp_path / "cache", client_factory=no_client)
    assert downloaded == [spec]
    assert converted == [(files["picked-file"], prepared.audio_path, 0.6)]
    assert prepared.words == [word for word in expected if word[2] <= 0.6]
    assert prepared.duration_seconds == 0.6
    assert prepared.audio_path.read_bytes() == b"stubbed conversion"


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda d: d["recipes"].pop("schema_version"), "schema_version"),
        (lambda d: d["recipes"].update(schema_version=True), "schema_version"),
        (lambda d: d["recipes"].update(schema_version=2), "schema_version"),
        (lambda d: d["recipes"].update(extra=True), "extra"),
        (lambda d: d["recipes"].update(entries=[]), "entries"),
        (lambda d: d["recipes"]["entries"].update(unfamiliar=[]), "unfamiliar"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].update(extra=True), "extra"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].pop("algorithm"), "algorithm"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].update(algorithm="unknown"), "algorithm"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].update(algorithm="os.system"), "algorithm"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].update(algorithm=[]), "algorithm"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].pop("conversion_source_param"), "conversion_source_param"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].update(conversion_source_param=""), "conversion_source_param"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].update(conversion_source_param="unknown"), "conversion_source_param"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].update(conversion_source_param=[]), "conversion_source_param"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].update(required_source_params=[]), "required_source_params"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].update(required_source_params="audio"), "required_source_params"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"]["required_source_params"].append("audio"), "required_source_params"),
        (lambda d: d["recipes"]["entries"]["unfamiliar"].update(required_params=["audio"]), "overlap"),
        (lambda d: d["samples"][0]["recipe"]["params"].pop("audio"), "missing audio"),
        (lambda d: d["samples"][0]["recipe"]["params"].update(audio="absent"), "names no source"),
    ],
)
def test_recipe_descriptors_refuse_invalid_data_before_execution(tmp_path, monkeypatch, change, message):
    document, _, _ = _document()
    executed = []
    monkeypatch.setattr(stt_samples, "RECIPES", {key: lambda *args: executed.append(True)
                                               for key in stt_samples.RECIPES})
    change(document)
    with pytest.raises(stt_samples.SampleError, match=message):
        _load(tmp_path, document)
    assert executed == []


def test_samples_only_manifest_uses_bundled_descriptors(tmp_path):
    document, _, _ = _document()
    document.pop("recipes")
    document["samples"][0]["recipe"]["kind"] = "earnings21"
    spec = _load(tmp_path, document)[0]
    assert spec.recipe_descriptor.algorithm == "earnings21"
    assert spec.recipe_descriptor.conversion_source_param == "audio"


def test_unknown_algorithm_cannot_execute_after_spec_construction(tmp_path):
    document, _, _ = _document()
    spec = _load(tmp_path, document)[0]
    unknown = replace(spec.recipe_descriptor, algorithm="os.system")
    with pytest.raises(stt_samples.SampleError, match="algorithm"):
        stt_samples.build_reference(replace(spec, recipe_descriptor=unknown), {})


def test_loaded_descriptor_does_not_change_with_a_later_manifest(tmp_path):
    document, _, _ = _document()
    spec = _load(tmp_path, document)[0]
    changed = copy.deepcopy(document)
    descriptor = changed["recipes"]["entries"]["unfamiliar"]
    descriptor["required_source_params"][0] = "payload_blob"
    descriptor["conversion_source_param"] = "payload_blob"
    params = changed["samples"][0]["recipe"]["params"]
    params["payload_blob"] = params.pop("audio")
    other = _load(tmp_path, changed)[0]
    assert spec.recipe_descriptor.conversion_source_param == "audio"
    assert other.recipe_descriptor.conversion_source_param == "payload_blob"


def test_legacy_spec_without_descriptor_uses_bundled_role(tmp_path, monkeypatch):
    document, bodies, expected = _document()
    document.pop("recipes")
    document["samples"][0]["recipe"]["kind"] = "earnings21"
    spec = replace(_load(tmp_path, document)[0], recipe_descriptor=None)
    files = {}
    for name, body in bodies.items():
        files[name] = tmp_path / name
        files[name].write_bytes(body)
    converted = []

    def convert(source, target, **kwargs):
        converted.append(source)
        return 1.0

    monkeypatch.setattr(stt_samples, "convert_audio", convert)
    prepared = stt_samples.prepare_sample(spec, files, tmp_path / "prepared.wav")
    assert converted == [files["audio"]]
    assert prepared.words == expected


@pytest.mark.parametrize("kind, field, parameter", [
    ("ami-words", "required_source_params", "annotations"),
    ("ami-words", "required_params", "word_members"),
    ("icsi-words", "required_source_params", "annotations"),
    ("icsi-words", "required_params", "word_members"),
    ("earnings21", "required_source_params", "transcript"),
    ("earnings21", "required_source_params", "segments"),
])
def test_descriptor_cannot_omit_algorithm_parameter(tmp_path, kind, field, parameter):
    document, _, _ = _document(kind)
    document["recipes"]["entries"]["unfamiliar"][field].remove(parameter)
    del document["samples"][0]["recipe"]["params"][parameter]
    with pytest.raises(stt_samples.SampleError) as raised:
        _load(tmp_path, document)
    message = str(raised.value)
    assert all(word in message for word in ("unfamiliar", kind, parameter))
