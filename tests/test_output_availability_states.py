"""Direct projection contracts, independent of persisted availability hints."""
from copy import deepcopy
import errno

import pytest

from gateway.output_availability import project


@pytest.fixture
def resolve(tmp_path):
    present = tmp_path / "present"
    present.write_bytes(b"fixture")
    paths = {
        "present": present,
        "missing": tmp_path / "missing",
        "other-missing": tmp_path / "other-missing",
        "directory": tmp_path,
        "unreadable": OSError(errno.EACCES, "fixture denied"),
    }

    def lookup(asset_id):
        value = paths.get(asset_id)
        if isinstance(value, OSError):
            raise value
        return value

    return lookup


def checked(job, resolve):
    before = deepcopy(job)
    view = project(job, resolve)
    assert job == before, "projection mutated the stored job"
    assert view["status"] == job["status"]
    return view


def test_missing_and_unreadable_are_unavailable(resolve):
    job = {"status": "succeeded", "assets": [
        {"asset_id": "missing"}, {"asset_id": "unreadable"}]}
    view = checked(job, resolve)
    assert view["output_state"] == "unavailable"
    assert [asset["availability"] for asset in view["assets"]] == ["missing", "unavailable"]


def test_two_missing_are_missing(resolve):
    job = {"status": "succeeded", "assets": [
        {"asset_id": "missing"}, {"asset_id": "other-missing"}]}
    view = checked(job, resolve)
    assert view["output_state"] == "missing"
    assert all(asset["available"] is False for asset in view["assets"])


def test_present_and_missing_are_partial(resolve):
    job = {"status": "succeeded", "assets": [
        {"asset_id": "present", "url": "https://fixture.invalid/present"},
        {"asset_id": "missing"}]}
    view = checked(job, resolve)
    assert view["output_state"] == "partial"
    assert view["assets"][0] == job["assets"][0]
    assert view["assets"][1]["availability"] == "missing"


def test_present_drops_stale_asset_hints(resolve):
    job = {"status": "succeeded", "assets": [
        {"asset_id": "present", "availability": "unavailable", "available": False,
         "url": "https://fixture.invalid/present"}]}
    view = checked(job, resolve)
    assert "output_state" not in view
    assert view["assets"] == [
        {"asset_id": "present", "url": "https://fixture.invalid/present"}]


def test_all_present_drops_stored_output_state(resolve):
    job = {"status": "succeeded", "output_state": "missing", "assets": [
        {"asset_id": "present"}, {"id": "present", "available": True}]}
    view = checked(job, resolve)
    assert "output_state" not in view
    assert view["assets"] == [{"asset_id": "present"}, {"id": "present"}]


def test_non_mapping_keeps_its_position(resolve):
    job = {"status": "succeeded", "assets": [
        {"asset_id": "missing"}, "invalid", {"asset_id": "other-missing"}]}
    view = checked(job, resolve)
    assert view["output_state"] == "unavailable"
    assert len(view["assets"]) == 3
    assert view["assets"][1] == {"available": False, "availability": "unavailable"}
    assert view["assets"][0]["asset_id"] == "missing"
    assert view["assets"][2]["asset_id"] == "other-missing"


def test_directory_counts_as_missing(resolve):
    job = {"status": "succeeded", "assets": [{"asset_id": "directory"}]}
    view = checked(job, resolve)
    assert view["output_state"] == "missing"
    assert view["assets"][0]["availability"] == "missing"


@pytest.mark.parametrize("asset_id", ["missing", "unreadable"])
def test_non_available_removes_all_six_url_fields(resolve, asset_id):
    fields = ("url", "asset_url", "content_url", "download_url",
              "thumbnail_url", "workflow_download_url")
    job = {"status": "succeeded", "assets": [
        {"asset_id": asset_id, **{key: "https://fixture.invalid/asset" for key in fields}}]}
    view = checked(job, resolve)
    assert view["assets"][0]["available"] is False
    assert not any(key in view["assets"][0] for key in fields)


def test_no_resolver_preserves_stored_view(resolve):
    job = {"status": "succeeded", "output_state": "missing", "assets": [
        {"asset_id": "present", "available": False, "availability": "missing"}, "invalid"]}
    assert checked(job, None) == job


@pytest.mark.parametrize("assets", [None, []])
def test_no_assets_preserves_stored_view(assets):
    job = {"status": "succeeded", "output_state": "missing", "assets": assets}

    def unexpected(_asset_id):
        pytest.fail("resolver must not be called for an empty job")

    assert checked(job, unexpected) == job
