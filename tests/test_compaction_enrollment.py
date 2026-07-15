from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "compaction_enrollment", Path(__file__).parents[1] / "scripts" / "compaction_enrollment.py"
)
assert _SPEC and _SPEC.loader
enrollment = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(enrollment)


NOW = 1_800_000_000
REPOSITORY_ROOT = Path(__file__).parents[1]


def _json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepared(body, _service_id, _alias_id, config) -> bytes:
    assert config["backends"]["service"]["models"][1]["compaction_safety"]["schema_version"] == 1
    result = {
        "model": "physical-model",
        "messages": body["messages"],
        "max_tokens": 8192,
        "stream": False,
        "thinking_budget_tokens": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    model = tmp_path / "model.gguf"
    template = tmp_path / "template.jinja"
    binary = tmp_path / "llama-server"
    unit = tmp_path / "bee.service"
    for path, content in ((model, b"model"), (template, b"template"), (binary, b"binary"), (unit, b"unit")):
        path.write_bytes(content)
    tokenizer = _json(tmp_path / "tokenizer.json", {
        "parser_version": "fake-parser/v1", "architecture": "qwen35", "tokenizer_model": "gpt2", "pretokenizer": "qwen35",
        "vocabulary": [f"piece-{index}" for index in range(256)], "merges": ["a b"],
        "byte_fallback": {f"{index:02x}": index for index in range(256)},
    })
    wrapper = _json(tmp_path / "wrapper.json", {
        "argv": [str(binary), "--model", str(model), "--chat-template", str(template), "--ctx-size", "102400"],
        "runtime": {"checkout_commit": "abc123", "clean": True},
        "default_template_kwargs": {"preserve_thinking": True},
        "effective_template_kwargs": {"preserve_thinking": True, "enable_thinking": False},
        "unit_files": [str(unit)],
    })
    corpus = _json(tmp_path / "corpus.json", [
        {"id": "empty", "body": {"model": "compaction", "messages": [{"role": "system", "content": ""}, {"role": "user", "content": ""}], "stream": False}},
        {"id": "unicode", "body": {"model": "compaction", "messages": [{"role": "system", "content": "operator prompt must not persist"}, {"role": "user", "content": "雪😀\\\""}], "stream": False}},
    ])
    server_digest = _sha(binary)
    cases = {
        name: {"prompt_sha256": hashlib.sha256(name.encode()).hexdigest(), "prompt_utf8_bytes": 20, "token_ids_sha256": hashlib.sha256((name + "tokens").encode()).hexdigest(), "token_count": 100, "no_thinking_generation_prefix": True}
        for name in ("empty", "unicode")
    }
    apply = _json(tmp_path / "apply.json", {"available": True, "server_binary_sha256": server_digest, "cases": cases})
    local = _json(tmp_path / "local.json", {"server_binary_sha256": server_digest, "cases": cases})
    manifest = _json(tmp_path / "manifest.json", {
        "service_id": "service", "base_model_id": "base", "alias_id": "compaction", "operation_id": "compaction-op",
        "deployment_generation": "g1", "evidence_registry_revision": "registry-g1", "evidence_revision": "receipt-r1", "evidence_receipt_identity": "receipt-1", "validator_version": "compaction-enrollment/v1",
        "validated_at": "2026-01-01T00:00:00Z", "expires_at": "2030-01-01T00:00:00Z", "template_revision": "qwen-fixed-v1", "numeric_bounds": enrollment.NUMERIC_BOUNDS,
        "paths": {"model": str(model), "template": str(template), "server_binary": str(binary), "tokenizer_transcript": str(tokenizer), "wrapper_transcript": str(wrapper), "probe_corpus": str(corpus), "apply_template_transcript": str(apply), "local_template_transcript": str(local)},
    })
    return manifest, tmp_path / "out"


def _collect(manifest: Path, output: Path) -> dict[str, str]:
    return enrollment.collect(manifest, output, preparer=_prepared, now=NOW)


def test_collect_verify_package_are_deterministic_and_redacted(tmp_path: Path):
    manifest, output = _fixture(tmp_path)
    first = _collect(manifest, output)
    second_output = tmp_path / "second"
    second = _collect(manifest, second_output)
    assert first == second
    for name in ("evidence.json", "overlay.json", "proofs.json"):
        assert (output / name).read_bytes() == (second_output / name).read_bytes()
    assert enrollment.verify(manifest, output, preparer=_prepared, now=NOW) == first
    package = enrollment.package(manifest, output, preparer=_prepared, now=NOW)
    assert package["evidence_digest"] == first["evidence_digest"]
    activation = json.loads((output / "activation-package.json").read_text())
    assert activation["format"] == "gateway-operation-activation-package/v1"
    assert activation["evidence"]["expires_at"] == "2030-01-01T00:00:00Z"
    from gateway.operation_activation import canonical_evidence
    assert canonical_evidence(activation["evidence"])[0] == activation["evidence_digest"]
    contents = b"".join((output / name).read_bytes() for name in ("evidence.json", "overlay.json", "proofs.json", "activation-package.json"))
    assert b"operator prompt must not persist" not in contents
    assert b"snow" not in contents
    assert b"opk_" not in contents


def test_collect_uses_the_real_pure_gateway_preparation_seam(tmp_path: Path):
    manifest, output = _fixture(tmp_path)
    result = enrollment.collect(manifest, output, now=NOW)
    proof = json.loads((output / "proofs.json").read_text())
    assert result["proof_digest"] == enrollment._digest(proof)
    assert proof["preparation"]["cases"][0]["admitted_body_utf8_bytes"] > 0


def test_default_cli_stays_outside_runtime_and_database_boundaries(tmp_path: Path):
    """The actual executable must not regain proxy startup through an import."""
    manifest, output = _fixture(tmp_path / "fixture")
    import_guard = tmp_path / "import-guard"
    import_guard.mkdir()
    (import_guard / "sitecustomize.py").write_text(
        """
import importlib.abc
import sys

_FORBIDDEN = frozenset({
    "proxy",
    "gateway.admin_store",
    "gateway.admin_runtime",
    "gateway.config_sources",
    "gateway.operation_activation",
    "gateway.service_registry",
})

class _RejectRuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in _FORBIDDEN:
            raise ImportError(f"offline CLI imported forbidden runtime module: {fullname}")
        return None

sys.meta_path.insert(0, _RejectRuntimeImports())
""".strip(),
        encoding="utf-8",
    )
    state_dir = tmp_path / "runtime-state"
    state_dir.mkdir()
    environment = os.environ.copy()
    environment.update({
        "PYTHONPATH": os.pathsep.join((str(import_guard), str(REPOSITORY_ROOT))),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AI_GATEWAY_ADMIN_DB": str(state_dir / "admin.db"),
        "AI_GATEWAY_METRICS_DB": str(state_dir / "metrics.db"),
        "AI_GATEWAY_RESPONSES_DB": str(state_dir / "responses.db"),
        "AI_GATEWAY_RECEIPTS_DB": str(state_dir / "receipts.db"),
        "AI_GATEWAY_MEDIA_JOBS_DB": str(state_dir / "media_jobs.db"),
    })
    result = subprocess.run(
        [
            sys.executable,
            "scripts/compaction_enrollment.py",
            "collect",
            "--manifest",
            str(manifest),
            "--output",
            str(output),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in output.iterdir()) == [
        "evidence.json",
        "overlay.json",
        "proofs.json",
    ]
    assert list(state_dir.iterdir()) == []
    assert not (state_dir / "litellm.config.yaml").exists()


def test_verify_rejects_tampering_and_changed_artifacts(tmp_path: Path):
    manifest, output = _fixture(tmp_path)
    _collect(manifest, output)
    (output / "evidence.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(enrollment.EnrollmentError, match="evidence.json"):
        enrollment.verify(manifest, output, preparer=_prepared, now=NOW)
    _collect(manifest, output)
    model = Path(json.loads(manifest.read_text())["paths"]["model"])
    model.write_bytes(b"changed")
    with pytest.raises(enrollment.EnrollmentError, match="evidence.json"):
        enrollment.verify(manifest, output, preparer=_prepared, now=NOW)


@pytest.mark.parametrize("mutation, message", [
    (lambda document: document.update({"unexpected": 0.5}), "floats"),
    (lambda document: document.update({"api_key": "opk_not_allowed"}), "not permitted"),
    (lambda document: document["paths"].update({"model": "../model.gguf"}), "traverse"),
    (lambda document: document.update({"expires_at": "2020-01-01T00:00:00Z"}), "expired"),
])
def test_collect_rejects_invalid_boundary_inputs(tmp_path: Path, mutation, message: str):
    manifest, output = _fixture(tmp_path)
    document = json.loads(manifest.read_text())
    mutation(document)
    _json(manifest, document)
    with pytest.raises(enrollment.EnrollmentError, match=message):
        _collect(manifest, output)


def test_collect_rejects_mismatched_kwargs_runtime_and_template_parity(tmp_path: Path):
    manifest, output = _fixture(tmp_path)
    document = json.loads(manifest.read_text())
    wrapper_path = Path(document["paths"]["wrapper_transcript"])
    wrapper = json.loads(wrapper_path.read_text())
    wrapper["effective_template_kwargs"] = {"preserve_thinking": True}
    _json(wrapper_path, wrapper)
    with pytest.raises(enrollment.EnrollmentError, match="effective_template_kwargs"):
        _collect(manifest, output)

    manifest, output = _fixture(tmp_path / "runtime")
    document = json.loads(manifest.read_text())
    wrapper_path = Path(document["paths"]["wrapper_transcript"])
    wrapper = json.loads(wrapper_path.read_text())
    wrapper["runtime"] = {"checkout_commit": "abc123", "clean": False}
    _json(wrapper_path, wrapper)
    with pytest.raises(enrollment.EnrollmentError, match="dirty runtime"):
        _collect(manifest, output)

    manifest, output = _fixture(tmp_path / "parity")
    document = json.loads(manifest.read_text())
    local_path = Path(document["paths"]["local_template_transcript"])
    local = json.loads(local_path.read_text())
    local["cases"]["unicode"]["token_count"] = 101
    _json(local_path, local)
    with pytest.raises(enrollment.EnrollmentError, match="parity differs"):
        _collect(manifest, output)


def test_collect_rejects_unavailable_or_mismatched_apply_template_runtime(tmp_path: Path):
    manifest, output = _fixture(tmp_path)
    document = json.loads(manifest.read_text())
    apply_path = Path(document["paths"]["apply_template_transcript"])
    apply = json.loads(apply_path.read_text())
    apply["available"] = False
    _json(apply_path, apply)
    with pytest.raises(enrollment.EnrollmentError, match="unavailable"):
        _collect(manifest, output)

    manifest, output = _fixture(tmp_path / "digest")
    document = json.loads(manifest.read_text())
    apply_path = Path(document["paths"]["apply_template_transcript"])
    apply = json.loads(apply_path.read_text())
    apply["server_binary_sha256"] = "0" * 64
    _json(apply_path, apply)
    with pytest.raises(enrollment.EnrollmentError, match="server binary"):
        _collect(manifest, output)


def test_collect_refuses_prompt_persistence_in_template_transcripts(tmp_path: Path):
    manifest, output = _fixture(tmp_path)
    document = json.loads(manifest.read_text())
    apply_path = Path(document["paths"]["apply_template_transcript"])
    apply = json.loads(apply_path.read_text())
    apply["cases"]["unicode"]["prompt"] = "operator prompt must not persist"
    _json(apply_path, apply)
    with pytest.raises(enrollment.EnrollmentError, match="must not persist prompt"):
        _collect(manifest, output)
