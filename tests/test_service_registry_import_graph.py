import copy

import pytest

from gateway import service_registry as sr


def _record(models, *, imported=False):
    record = {
        "source": "imported" if imported else "ui",
        "config": {"model_policy_schema_version": 1, "models": models},
    }
    if imported:
        record["provenance"] = {
            "package_id": "fake-package",
            "package_version": "1",
            "package_hash": "fake-content-hash",
        }
    return record


def _base():
    return {"id": "base", "model_group": "base", "upstream_model": "engine-model"}


def _alias(public_name="current", base_id="base"):
    return {
        "id": "alias",
        "model_group": public_name,
        "upstream_model": "engine-model",
        "base_model_id": base_id,
    }


def test_import_rejects_a_new_service_with_a_missing_model_base():
    registry = sr.normalize_service_registry(
        {"updated_at": 1, "services": {"existing": _record([_base()])}}
    )
    before = copy.deepcopy(registry)
    candidate = _record([_alias(base_id="missing")], imported=True)
    candidate_before = copy.deepcopy(candidate)

    with pytest.raises(ValueError, match="unknown base_model_id"):
        sr.merge_imported_records(registry, services={"new-service": candidate})

    assert registry == before
    assert candidate == candidate_before


def test_import_rejects_a_case_insensitive_public_alias_collision():
    registry = sr.normalize_service_registry(
        {
            "updated_at": 1,
            "services": {"existing": _record([_base(), _alias("Current")])},
        }
    )
    before = copy.deepcopy(registry)

    with pytest.raises(ValueError, match="ambiguous across services"):
        sr.merge_imported_records(
            registry,
            services={
                "new-service": _record(
                    [
                        _base(),
                        {
                            **_alias(" current "),
                            "id": "another-alias",
                        },
                    ],
                    imported=True,
                )
            },
        )

    assert registry == before
