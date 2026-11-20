"""Member adapter uploads, the file rules: a safetensors file is checked by its own header, never loaded.

What an uploader controls is the file's bytes, its name and its size. The gateway reads 8 bytes, a bounded JSON header and
the file's length; it never loads a tensor, never unpickles, and never uses the uploader's name for a path.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import struct
from pathlib import Path

import pytest

from gateway import adapter_upload

TABLE = {"alpha_base": {"ss_base_model_version": ("alpha_base",)}, "beta_base": {"ss_base_model_version": ("beta_base",)}}
GIB = 1 << 30


def safetensors(tensors: dict[str, list[int]] | None = None, metadata: dict[str, str] | None = None, *,
                header_edit=None, trailing: bytes = b"") -> bytes:
    """A real safetensors file: 8-byte little-endian header length, the JSON header, then the tensor bytes (F16)."""
    tensors = {"layer.lora_down.weight": [2, 2], "layer.lora_up.weight": [2, 2]} if tensors is None else tensors
    header: dict[str, object] = {} if metadata is None else {"__metadata__": metadata}
    offset = 0
    for name, shape in tensors.items():
        size = 2
        for dim in shape:
            size *= dim
        header[name] = {"dtype": "F16", "shape": shape, "data_offsets": [offset, offset + size]}
        offset += size
    if header_edit is not None:
        header_edit(header)
    blob = json.dumps(header).encode()
    return struct.pack("<Q", len(blob)) + blob + b"\x01" * offset + trailing


def check(data: bytes, **kwargs):
    return adapter_upload.read_header(io.BytesIO(data), len(data), **kwargs)


def refused_code(data: bytes, **kwargs) -> str:
    with pytest.raises(adapter_upload.Refused) as caught:
        check(data, **kwargs)
    return caught.value.code


# --- the file: safetensors only -------------------------------------------------------------------------------------

def test_a_valid_file_gives_its_metadata_and_tensor_count():
    header = check(safetensors(metadata={"ss_base_model_version": "alpha_base"}))
    assert dict(header.metadata) == {"ss_base_model_version": "alpha_base"}
    assert header.tensor_count == 2


def test_a_file_with_no_metadata_block_is_valid_with_empty_metadata():
    assert dict(check(safetensors()).metadata) == {}


@pytest.mark.parametrize("data", [
    b"PK\x03\x04" + b"\x00" * 64,                       # a zip: the container of .pt and .ckpt
    b"\x80\x04\x95" + b"\x00" * 64,                     # a pickle stream
    b"GGUF" + b"\x00" * 64,
    b"not a model at all, just words in a file",
    b"",
    b"\x00" * 4,
])
def test_other_formats_are_refused_as_not_safetensors(data):
    assert refused_code(data) == "not_safetensors"


def test_a_header_length_larger_than_the_file_is_refused():
    data = struct.pack("<Q", 10_000) + b"{}" + b"\x00" * 10
    assert refused_code(data) == "adapter_header_invalid"


def test_a_header_over_the_size_bound_is_refused_before_it_is_read():
    blob = json.dumps({"__metadata__": {"k": "v" * 5000}}).encode()
    data = struct.pack("<Q", len(blob)) + blob
    assert refused_code(data, max_header_bytes=1000) == "adapter_header_invalid"


def test_a_huge_declared_header_length_does_not_make_the_gateway_read_that_much():
    class Counting(io.BytesIO):
        read_total = 0

        def read(self, n=-1):
            chunk = super().read(n)
            Counting.read_total += len(chunk)
            return chunk

    data = struct.pack("<Q", 2**62) + b"{}"
    with pytest.raises(adapter_upload.Refused):
        adapter_upload.read_header(Counting(data), len(data))
    assert Counting.read_total <= 8 + adapter_upload.MAX_HEADER_BYTES


def test_a_header_that_is_not_json_is_refused():
    blob = b"{not json"
    assert refused_code(struct.pack("<Q", len(blob)) + blob) == "adapter_header_invalid"


def test_a_header_that_does_not_open_with_a_brace_is_another_format():
    blob = b"[1, 2]"
    assert refused_code(struct.pack("<Q", len(blob)) + blob) == "not_safetensors"


def test_a_header_with_a_repeated_key_is_refused():
    blob = b'{"a": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}, "a": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}}'
    assert refused_code(struct.pack("<Q", len(blob)) + blob + b"\x00\x00") == "adapter_header_invalid"


def test_offsets_past_the_end_of_the_file_are_refused():
    def lie(header):
        header["layer.lora_up.weight"]["data_offsets"] = [8, 8_000_000]
    assert refused_code(safetensors(header_edit=lie)) == "adapter_header_invalid"


def test_a_truncated_file_is_refused():
    data = safetensors()
    assert refused_code(data[:-3]) == "adapter_header_invalid"


def test_overlapping_offsets_are_refused():
    def lie(header):
        header["layer.lora_up.weight"]["data_offsets"] = [0, 8]
    assert refused_code(safetensors(header_edit=lie)) == "adapter_header_invalid"


def test_bytes_after_the_last_tensor_are_refused():
    assert refused_code(safetensors(trailing=b"x" * 16)) == "adapter_header_invalid"


def test_a_negative_or_reversed_offset_is_refused():
    def lie(header):
        header["layer.lora_up.weight"]["data_offsets"] = [8, 0]
    assert refused_code(safetensors(header_edit=lie)) == "adapter_header_invalid"


def test_a_tensor_entry_that_is_not_shaped_as_one_is_refused():
    def lie(header):
        header["layer.lora_up.weight"] = {"dtype": "F16"}
    assert refused_code(safetensors(header_edit=lie)) == "adapter_header_invalid"


def test_a_file_with_no_tensor_is_refused():
    assert refused_code(safetensors(tensors={}, metadata={"a": "b"})) == "adapter_header_invalid"


def test_metadata_values_must_be_text():
    blob = json.dumps({"__metadata__": {"k": 3}, "t": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}}).encode()
    assert refused_code(struct.pack("<Q", len(blob)) + blob + b"\x00\x00") == "adapter_header_invalid"


# --- which base a file is for: the same table the operator's files use -----------------------------------------------

def test_the_architecture_comes_from_the_metadata_through_the_table():
    assert adapter_upload.architectures({"ss_base_model_version": "alpha_base"}, TABLE) == ("alpha_base",)


def test_an_unknown_base_gives_no_architecture():
    assert adapter_upload.architectures({"ss_base_model_version": "gamma"}, TABLE) == ()
    assert adapter_upload.architectures({}, TABLE) == ()


def test_the_shipped_table_is_the_one_the_architecture_read_uses():
    table = adapter_upload.architecture_table()
    assert table and all(isinstance(keys, dict) for keys in table.values())
    arch, keys = next(iter(table.items()))
    key, values = next(iter(keys.items()))
    assert adapter_upload.architectures({key: values[0]}, table) == (arch,)


# --- the owner's setting and the size cap ---------------------------------------------------------------------------

def test_uploads_are_off_unless_the_owner_turned_them_on():
    assert adapter_upload.policy(None).enabled is False
    assert adapter_upload.policy({}).enabled is False
    assert adapter_upload.policy({"enabled": "yes"}).enabled is False
    assert adapter_upload.policy({"enabled": True}).enabled is True


def test_the_cap_defaults_to_two_gib_and_takes_a_positive_integer():
    assert adapter_upload.policy(None).max_bytes == 2 * GIB
    assert adapter_upload.policy({"enabled": True, "max_bytes": 5 * 1024 * 1024}).max_bytes == 5 * 1024 * 1024
    for bad in (0, -1, True, "big", 1.5):
        assert adapter_upload.policy({"enabled": True, "max_bytes": bad}).max_bytes == 2 * GIB


def test_an_upload_is_refused_when_the_setting_is_off():
    with pytest.raises(adapter_upload.Refused) as caught:
        adapter_upload.admit(adapter_upload.policy(None), size=100, engine_local=True)
    assert (caught.value.code, caught.value.status) == ("adapter_uploads_off", 403)


def test_an_upload_over_the_cap_is_refused():
    on = adapter_upload.policy({"enabled": True, "max_bytes": 1000})
    adapter_upload.admit(on, size=1000, engine_local=True)
    with pytest.raises(adapter_upload.Refused) as caught:
        adapter_upload.admit(on, size=1001, engine_local=True)
    assert (caught.value.code, caught.value.status) == ("adapter_too_large", 413)


def test_an_upload_is_refused_when_the_engine_is_not_on_this_machine():
    with pytest.raises(adapter_upload.Refused) as caught:
        adapter_upload.admit(adapter_upload.policy({"enabled": True}), size=1, engine_local=False)
    assert (caught.value.code, caught.value.status) == ("adapter_engine_not_local", 409)


def test_an_empty_upload_is_refused():
    with pytest.raises(adapter_upload.Refused) as caught:
        adapter_upload.admit(adapter_upload.policy({"enabled": True}), size=0, engine_local=True)
    assert caught.value.code == "not_safetensors"


def test_validate_streams_the_file_and_returns_its_digest(tmp_path):
    data = safetensors(metadata={"ss_base_model_version": "alpha_base"})
    path = tmp_path / "up.bin"
    path.write_bytes(data)
    checked = adapter_upload.validate(path, adapter_upload.policy({"enabled": True}))
    assert checked.digest == hashlib.sha256(data).hexdigest()
    assert checked.size == len(data)
    assert dict(checked.header.metadata) == {"ss_base_model_version": "alpha_base"}


def test_validate_refuses_a_file_over_the_cap_without_reading_it(tmp_path):
    path = tmp_path / "up.bin"
    path.write_bytes(safetensors())
    with pytest.raises(adapter_upload.Refused) as caught:
        adapter_upload.validate(path, adapter_upload.policy({"enabled": True, "max_bytes": 10}))
    assert caught.value.code == "adapter_too_large"


# --- giving the engine a file: a hash-named copy in a folder the gateway owns -----------------------------------------

@pytest.fixture
def lora_root(tmp_path):
    root = tmp_path / "models" / "loras"
    root.mkdir(parents=True)
    return root


def source_file(tmp_path, data: bytes | None = None) -> tuple[Path, str]:
    data = safetensors() if data is None else data
    path = tmp_path / "cache" / "stored.safetensors"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def test_the_materialized_name_is_the_content_hash_under_the_gateway_folder(tmp_path, lora_root):
    source, digest = source_file(tmp_path)
    cache = adapter_upload.Materializer(lora_root)
    name = cache.materialize(digest, source)
    assert name == f"{adapter_upload.SUBFOLDER}/{digest}.safetensors"
    assert (lora_root / name).read_bytes() == source.read_bytes()
    assert os.listdir(lora_root) == [adapter_upload.SUBFOLDER]


def test_materializing_twice_keeps_one_file_and_marks_it_used(tmp_path, lora_root):
    source, digest = source_file(tmp_path)
    cache = adapter_upload.Materializer(lora_root)
    cache.materialize(digest, source)
    target = lora_root / cache.name_for(digest)
    os.utime(target, (1, 1))
    cache.materialize(digest, source)
    assert len(os.listdir(lora_root / adapter_upload.SUBFOLDER)) == 1
    assert target.stat().st_mtime > 1_000_000


@pytest.mark.parametrize("digest", ["../escape", "a" * 63, "A" * 64, "a" * 64 + "/x", "", "g" * 64])
def test_a_digest_that_is_not_lowercase_hex_never_names_a_path(tmp_path, lora_root, digest):
    source, _ = source_file(tmp_path)
    with pytest.raises(ValueError):
        adapter_upload.Materializer(lora_root).materialize(digest, source)
    assert os.listdir(lora_root) == []


def test_a_source_that_does_not_match_its_digest_is_not_copied(tmp_path, lora_root):
    source, _ = source_file(tmp_path)
    with pytest.raises(ValueError):
        adapter_upload.Materializer(lora_root).materialize("b" * 64, source)
    assert not (lora_root / adapter_upload.SUBFOLDER).exists() or not os.listdir(lora_root / adapter_upload.SUBFOLDER)


def test_a_folder_that_leaves_the_loras_folder_by_a_link_is_not_written_to(tmp_path, lora_root):
    outside = tmp_path / "outside"
    outside.mkdir()
    (lora_root / adapter_upload.SUBFOLDER).symlink_to(outside, target_is_directory=True)
    source, digest = source_file(tmp_path)
    with pytest.raises(adapter_upload.Refused):
        adapter_upload.Materializer(lora_root).materialize(digest, source)
    assert os.listdir(outside) == []


def test_remove_deletes_the_copy(tmp_path, lora_root):
    source, digest = source_file(tmp_path)
    cache = adapter_upload.Materializer(lora_root)
    cache.materialize(digest, source)
    cache.remove(digest)
    assert os.listdir(lora_root / adapter_upload.SUBFOLDER) == []
    cache.remove(digest)  # a second removal is not an error


def test_reap_removes_files_idle_for_a_while_and_keeps_the_recent_ones(tmp_path, lora_root):
    cache = adapter_upload.Materializer(lora_root)
    old_data, new_data = safetensors({"a": [1]}), safetensors({"b": [2]})
    old = hashlib.sha256(old_data).hexdigest()
    new = hashlib.sha256(new_data).hexdigest()
    for data, digest in ((old_data, old), (new_data, new)):
        path = tmp_path / f"{digest}.src"
        path.write_bytes(data)
        cache.materialize(digest, path)
    os.utime(lora_root / cache.name_for(old), (1_000, 1_000))
    removed = cache.reap(now=1_000 + 7200, idle_s=3600, max_bytes=10 * GIB)
    assert removed == [old]
    assert os.listdir(lora_root / adapter_upload.SUBFOLDER) == [f"{new}.safetensors"]


def test_reap_keeps_the_total_under_the_byte_bound_oldest_first(tmp_path, lora_root):
    cache = adapter_upload.Materializer(lora_root)
    digests = []
    for index in range(3):
        data = safetensors({f"t{index}": [64]})
        digest = hashlib.sha256(data).hexdigest()
        path = tmp_path / f"{index}.src"
        path.write_bytes(data)
        cache.materialize(digest, path)
        os.utime(lora_root / cache.name_for(digest), (100 + index, 100 + index))
        digests.append((digest, len(data)))
    keep_two = digests[1][1] + digests[2][1]
    removed = cache.reap(now=200, idle_s=10**9, max_bytes=keep_two)
    assert removed == [digests[0][0]]


def test_reap_never_removes_a_file_a_running_request_holds(tmp_path, lora_root):
    source, digest = source_file(tmp_path)
    cache = adapter_upload.Materializer(lora_root)
    cache.materialize(digest, source)
    os.utime(lora_root / cache.name_for(digest), (1, 1))
    with cache.hold(digest):
        assert cache.reap(now=10**9, idle_s=1, max_bytes=0) == []
    assert cache.reap(now=10**9, idle_s=1, max_bytes=0) == [digest]


def test_reap_ignores_names_it_did_not_make(tmp_path, lora_root):
    folder = lora_root / adapter_upload.SUBFOLDER
    folder.mkdir()
    (folder / "something-else.txt").write_text("keep")
    adapter_upload.Materializer(lora_root).reap(now=10**9, idle_s=1, max_bytes=0)
    assert (folder / "something-else.txt").exists()


# --- the pick-list: the operator's files, then this person's own compatible uploads ----------------------------------

def offer(asset_id: str, label: str, *architectures: str) -> adapter_upload.Offer:
    return adapter_upload.Offer(asset_id, label, tuple(architectures))


def test_a_person_sees_the_operator_files_and_their_compatible_uploads():
    mine = [offer("ast_" + "1" * 64, "mine.safetensors", "alpha_base"), offer("ast_" + "2" * 64, "other-base.safetensors", "beta_base")]
    assert adapter_upload.join(["op-a.safetensors"], mine, "alpha_base") == ["op-a.safetensors", "ast_" + "1" * 64]


def test_an_upload_whose_base_is_unknown_is_not_offered():
    assert adapter_upload.join(["op-a.safetensors"], [offer("ast_" + "3" * 64, "x", )], "alpha_base") == ["op-a.safetensors"]


def test_with_no_uploads_the_list_is_the_operator_files():
    assert adapter_upload.join(["op-a.safetensors"], [], "alpha_base") == ["op-a.safetensors"]


def test_an_upload_never_repeats_an_operator_name():
    name = "ast_" + "4" * 64
    assert adapter_upload.join([name], [offer(name, "x", "alpha_base")], "alpha_base") == [name]


def test_the_digest_of_an_offered_name_is_read_back_from_its_asset_id():
    assert adapter_upload.digest_of("ast_" + "5" * 64) == "5" * 64
    assert adapter_upload.digest_of("op-a.safetensors") is None
    assert adapter_upload.digest_of("img_" + "5" * 64) is None
    assert adapter_upload.digest_of("ast_" + "G" * 64) is None


# --- a person's own uploads: the Library's ownership rule decides, the header decides the base -------------------------

class Store:
    """The asset store, the Library's ownership rule and the engine's folder, as the Library class is given them."""

    def __init__(self, tmp_path: Path, *, enabled: bool = True, local: bool = True) -> None:
        self.cache = tmp_path / "cache"
        self.cache.mkdir()
        self.engine = tmp_path / "models" / "loras"
        self.engine.mkdir(parents=True)
        self.enabled, self.local = enabled, local
        self.sidecars: dict[str, dict] = {}
        self.holders: dict[str, set[str]] = {}
        self.noted: list[tuple[str, str]] = []
        self.library = adapter_upload.Library(
            asset_ids=lambda: list(self.sidecars),
            metadata=lambda asset_id: self.sidecars.get(asset_id, {}),
            path=lambda asset_id: self.cache / f"{asset_id.split('_', 1)[1]}{adapter_upload.SUFFIX}",
            holds=lambda person, asset_id: person in self.holders.get(asset_id, ()),
            policy=lambda: adapter_upload.Policy(self.enabled),
            lora_root=lambda folder: self.engine if self.local and folder == "loras" else None,
            note_copy=lambda asset_id, folder: self.noted.append((asset_id, folder)),
            table=TABLE)

    def add(self, person: str, data: bytes, label: str = "mine.safetensors", *, marked: bool = True) -> str:
        digest = hashlib.sha256(data).hexdigest()
        asset_id = f"ast_{digest}"
        (self.cache / f"{digest}{adapter_upload.SUFFIX}").write_bytes(data)
        self.sidecars.setdefault(asset_id, {})
        if marked:
            self.sidecars[asset_id]["adapter"] = {"label": label}
        self.holders.setdefault(asset_id, set()).add(person)
        return asset_id


ALPHA = safetensors(metadata={"ss_base_model_version": "alpha_base"})
BETA = safetensors({"b": [3]}, metadata={"ss_base_model_version": "beta_base"})
UNKNOWN = safetensors({"u": [5]}, metadata={"ss_base_model_version": "gamma"})


def test_a_person_is_offered_their_own_compatible_uploads(tmp_path):
    store = Store(tmp_path)
    mine = store.add("robin", ALPHA, "style.safetensors")
    store.add("robin", BETA, "other.safetensors")
    offers = store.library.offers("robin", folder="loras")
    assert {o.name: (o.label, o.architectures) for o in offers} == {
        mine: ("style.safetensors", ("alpha_base",)),
        "ast_" + hashlib.sha256(BETA).hexdigest(): ("other.safetensors", ("beta_base",))}


def test_another_persons_upload_is_not_offered_and_is_refused_like_an_unknown_name(tmp_path):
    store = Store(tmp_path)
    theirs = store.add("sam", ALPHA)
    assert store.library.offers("robin", folder="loras") == []
    assert store.library.resolve("robin", theirs, folder="loras") is None
    assert store.library.resolve("robin", "ast_" + "0" * 64, folder="loras") is None
    with pytest.raises(adapter_upload.UnknownAdapter):
        store.library.engine_names("robin", [theirs], folder="loras")
    with pytest.raises(adapter_upload.UnknownAdapter):
        store.library.engine_names("robin", ["ast_" + "0" * 64], folder="loras")
    assert os.listdir(store.engine) == []


def test_an_upload_with_an_unknown_base_is_stored_but_not_offered(tmp_path):
    store = Store(tmp_path)
    asset_id = store.add("robin", UNKNOWN)
    [offer_] = store.library.offers("robin", folder="loras")
    assert offer_.name == asset_id and offer_.architectures == ()
    assert adapter_upload.join([], store.library.offers("robin", folder="loras"), "alpha_base") == []
    assert adapter_upload.join([], store.library.offers("robin", folder="loras"), "gamma") == []


def test_nothing_is_offered_when_the_setting_is_off(tmp_path):
    store = Store(tmp_path, enabled=False)
    asset_id = store.add("robin", ALPHA)
    assert store.library.offers("robin", folder="loras") == []
    assert store.library.resolve("robin", asset_id, folder="loras") is None


def test_nothing_is_offered_when_the_engine_is_not_on_this_computer(tmp_path):
    store = Store(tmp_path, local=False)
    store.add("robin", ALPHA)
    assert store.library.offers("robin", folder="loras") == []


def test_a_stored_file_that_no_upload_marked_is_not_an_adapter(tmp_path):
    store = Store(tmp_path)
    store.add("robin", ALPHA, marked=False)
    assert store.library.offers("robin", folder="loras") == []


def test_a_stored_file_whose_header_no_longer_reads_is_not_offered(tmp_path):
    store = Store(tmp_path)
    asset_id = store.add("robin", ALPHA)
    (store.cache / f"{asset_id.split('_', 1)[1]}{adapter_upload.SUFFIX}").write_bytes(b"not safetensors any more")
    assert store.library.offers("robin", folder="loras") == []


def test_using_an_upload_copies_it_under_its_hash_and_notes_the_folder(tmp_path):
    store = Store(tmp_path)
    asset_id = store.add("robin", ALPHA)
    digest = asset_id.split("_", 1)[1]
    names = store.library.engine_names("robin", [asset_id], folder="loras")
    assert names == {asset_id: f"{adapter_upload.SUBFOLDER}/{digest}.safetensors"}
    assert (store.engine / names[asset_id]).read_bytes() == ALPHA
    assert store.noted == [(asset_id, "loras")]
    assert "mine" not in " ".join(os.listdir(store.engine / adapter_upload.SUBFOLDER))


def test_a_deleted_asset_removes_its_copy_and_leaves_the_pick_list(tmp_path):
    store = Store(tmp_path)
    asset_id = store.add("robin", ALPHA)
    names = store.library.engine_names("robin", [asset_id], folder="loras")
    # The Library's delete: the claim goes with the file, and the sidecar said where copies were made.
    store.library.forget(asset_id, {"adapter": {"label": "x", "copies": ["loras"]}})
    assert not (store.engine / names[asset_id]).exists()
    store.holders[asset_id].clear()
    assert store.library.offers("robin", folder="loras") == []


def test_forget_leaves_a_folder_it_cannot_reach_alone(tmp_path):
    store = Store(tmp_path, local=False)
    store.library.forget("ast_" + "7" * 64, {"adapter": {"copies": ["loras"]}})   # no error


def test_a_sweep_removes_copies_whose_asset_is_gone(tmp_path):
    store = Store(tmp_path)
    kept, gone = store.add("robin", ALPHA), store.add("robin", BETA)
    store.library.engine_names("robin", [kept, gone], folder="loras")
    del store.sidecars[gone]
    assert store.library.sweep(folder="loras") == [gone.split("_", 1)[1]]
    assert os.listdir(store.engine / adapter_upload.SUBFOLDER) == [f"{kept.split('_', 1)[1]}.safetensors"]


# --- the body limit and the ceiling of the owner's cap --------------------------------------------------------------

def test_the_adapter_route_may_carry_a_body_up_to_the_ceiling_and_other_routes_keep_their_limits():
    from gateway.request_body_limits import RequestBodyLimits
    limits = RequestBodyLimits()
    assert limits.for_path("/me/api/adapters") > adapter_upload.MAX_BYTES_CEILING
    assert limits.for_path("/me/api/prompt-memory") == limits.json_bytes
    assert limits.for_path("/me/api/adapters/other") == limits.json_bytes


def test_the_owners_cap_cannot_pass_the_ceiling():
    assert adapter_upload.policy({"enabled": True, "max_bytes": adapter_upload.MAX_BYTES_CEILING + 1}).max_bytes == \
        adapter_upload.MAX_BYTES_CEILING
