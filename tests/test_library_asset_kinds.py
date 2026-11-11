"""An asset's kind comes from its media type, so a new output type reaches the Library without a code change."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import accounts, admin_store, asset_types, media_jobs, me_media, prompt_memory, receipt_store, workflow_call
from gateway.admin_oidc import AdminSession

GLB = b"glTF\x02\x00\x00\x00" + b"\x00" * 24


@pytest.mark.parametrize("suffix,media_type,kind", [
    (".glb", "model/gltf-binary", "model"),
    (".gltf", "model/gltf+json", "model"),
    (".png", "image/png", "image"),
    (".mp3", "audio/mpeg", "audio"),
    (".mp4", "video/mp4", "video"),
    (".txt", "text/plain", "text"),
    (".zzunknown", "application/octet-stream", "file"),
    ("", "application/octet-stream", "file"),
])
def test_kind_follows_the_media_type_of_the_suffix(suffix, media_type, kind):
    assert asset_types.media_type_for_suffix(suffix) == media_type
    assert asset_types.kind_for_media_type(media_type) == kind


@pytest.mark.parametrize("media_type", [None, "", "nonsense", "application/octet-stream"])
def test_an_unknown_media_type_is_a_file(media_type):
    assert asset_types.kind_for_media_type(media_type) == "file"


def test_a_future_top_level_type_needs_no_code():
    assert asset_types.kind_for_media_type("smell/aroma-bundle") == "smell"


def test_asset_kind_keeps_the_fixed_prefixes_and_derives_the_rest():
    digest = "a" * 64
    assert me_media._asset_kind(f"img_{digest}", {}) == "image"
    assert me_media._asset_kind(f"aud_{digest}", {}) == "audio"
    assert me_media._asset_kind(f"vid_{digest}", {}) == "video"
    assert me_media._asset_kind(f"ast_{digest}", {}, "model/gltf-binary") == "model"
    assert me_media._asset_kind(f"ast_{digest}", {}, lambda: "model/gltf-binary") == "model"
    assert me_media._asset_kind(f"ast_{digest}", {}, "application/octet-stream") == "file"
    assert me_media._asset_kind(f"ast_{digest}", {}) == "file"


@pytest.fixture
def library(tmp_path, monkeypatch):
    admin = tmp_path / "data/admin.db"
    jobs = tmp_path / "data/media_jobs.db"
    receipts = tmp_path / "data/receipts.db"
    admin_store.init_db(admin)
    accounts.initialize(admin)
    media_jobs.init_db(jobs)
    receipt_store.init_db(receipts)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    monkeypatch.setattr(prompt_memory, "DB_PATH", jobs)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache")
    robin = admin_store.upsert_oidc_user("iss", "robin", email="robin@x.test", path=admin)
    # Workflow outputs are stored for the signed-in caller; the tests call the store without a request.
    monkeypatch.setattr(proxy, "_auth_person_id", lambda auth: robin["id"])
    monkeypatch.setattr(proxy, "_asset_owner_key", lambda auth: None)

    def session(_request):
        return AdminSession(subject=robin["id"], email=robin["email"], role="none",
                            issued_at=time.time(), expires_at=time.time() + 3600)

    app = FastAPI()
    app.include_router(me_media.create_me_media_router(
        session_provider=session,
        signed_url_builder=lambda request, asset_id, mode: f"http://testserver/v1/assets/{asset_id}/content?md={mode}",
        asset_path_resolver=proxy.asset_cache_path,
        asset_media_type_resolver=proxy.asset_media_type,
        asset_deleter=lambda asset_id, **_: False,
        asset_catalog_provider=me_media.create_asset_catalog_provider(proxy.IMAGE_CACHE_DIR),
        asset_metadata_reader=proxy._read_asset_metadata,
        admin_db_path=admin, media_jobs_path=jobs, receipts_path=receipts, prompt_memory_path=jobs,
    ))
    return SimpleNamespace(client=TestClient(app), robin=robin)


def _store(library, content, suffix):
    return proxy.store_asset_bytes(content, kind="ast", suffix=suffix, owner_key_hash=None,
                                   maker_person_id=library.robin["id"])


def test_a_stored_glb_is_listed_by_the_real_media_endpoint(library):
    asset_id = _store(library, GLB, ".glb")
    body = library.client.get("/me/api/media").json()
    item = next(i for i in body["assets"] if i["asset_id"] == asset_id)
    assert item["kind"] == "model"
    assert item["media_type"] == "model/gltf-binary"
    assert item["size_bytes"] == len(GLB)
    assert item["download_url"]


def test_the_kind_filter_selects_a_derived_kind(library):
    glb = _store(library, GLB, ".glb")
    other = _store(library, b"hello", ".zzunknown")
    models = library.client.get("/me/api/media?kind=model").json()["assets"]
    assert [i["asset_id"] for i in models] == [glb]
    files = library.client.get("/me/api/media?kind=file").json()["assets"]
    assert [(i["asset_id"], i["media_type"]) for i in files] == [(other, "application/octet-stream")]


def test_a_stored_file_is_found_again_by_its_id(library):
    asset_id = _store(library, GLB, ".glb")
    path = proxy.asset_cache_path(asset_id)
    assert path is not None and path.read_bytes() == GLB
    assert proxy.asset_media_type(asset_id) == "model/gltf-binary"


# --- what made a stored file: any workflow, no workflow named in the store ---------------------------------------------

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)


def _store_output(library, **overrides):
    output = asset_types.WorkflowOutput(
        data=GLB, suffix=".glb", workflow_name="TRELLIS.2 image to 3D", request_id="req_made",
        inputs=(asset_types.OutputInput(PNG, "garden-gnome.png", "image/png"),),
    )
    from dataclasses import replace
    return proxy.store_workflow_output(None, replace(output, **overrides))


def _item(library, asset_id):
    return next(i for i in library.client.get("/me/api/media").json()["assets"] if i["asset_id"] == asset_id)


def test_a_workflow_output_keeps_its_workflow_name_request_and_source(library):
    asset_id = _store_output(library)
    sidecar = proxy._read_asset_metadata(asset_id)
    [source] = sidecar["origin"]["sources"]
    assert source["name"] == "garden-gnome" and source["asset_id"].startswith("img_")
    assert sidecar["origin"]["workflow_name"] == "TRELLIS.2 image to 3D"
    assert sidecar["origin"]["request_id"] == "req_made"
    assert proxy.asset_cache_path(source["asset_id"]).read_bytes() == PNG


def test_the_library_titles_a_made_item_from_its_source_and_names_the_workflow(library):
    asset_id = _store_output(library)
    item = _item(library, asset_id)
    assert item["derived_title"] == "garden-gnome → 3D model"
    assert item["workflow_name"] == "TRELLIS.2 image to 3D"
    assert item["request_id"] == "req_made"
    assert item["takes_text"] is True
    [source] = item["source_assets"]
    assert source["name"] == "garden-gnome"
    # The source is named as a person would: by the media type it was stored with, with its format and size.
    assert (source["media_type"], source["kind"], source["word"], source["format"], source["size_bytes"]) == (
        "image/png", "image", "picture", "PNG", len(PNG))
    # The source picture stands in for the item on a tile; the input is an image, so it can.
    assert item["poster_asset_id"] == source["asset_id"]


def test_the_run_prompt_is_kept_for_the_result_and_not_for_its_inputs(library, monkeypatch):
    kept = []
    decision = SimpleNamespace(keep=True, person_id=library.robin["id"], kept_until=None, created_at=1)
    delivery = SimpleNamespace(prompt_decision=decision, prompt_text="a gnome in a garden", receipt=None,
                               person_id=library.robin["id"], capture_embedded_prompt=True)
    monkeypatch.setattr(proxy.media_delivery, "current", lambda: delivery)
    monkeypatch.setattr(proxy.prompt_memory, "capture_metadata", lambda content, suffix: {})
    monkeypatch.setattr(proxy.prompt_memory, "safe_put", lambda asset_id, *args, **kw: kept.append(asset_id) or True)
    made = _store_output(library)
    [source] = proxy._read_asset_metadata(made)["origin"]["sources"]
    assert kept == [made] and source["asset_id"] not in kept


def test_a_made_item_is_dated_by_the_receipt_of_its_request(library):
    # Without a date a new 3D model would sort below every older item for good.
    receipt_store.record({"request_id": "req_made", "status": "succeeded", "accepted_at_ms": 1_700_000_000_000, "intent": "x"},
                         path=receipt_store.DB_PATH)
    item = _item(library, _store_output(library))
    assert item["created_at"] == 1_700_000_000


def test_a_workflow_with_no_text_input_says_so(library):
    assert _item(library, _store_output(library, takes_text=False))["takes_text"] is False


def test_an_item_with_no_origin_has_none_of_these(library):
    item = _item(library, _store(library, GLB, ".glb"))
    assert item["takes_text"] is True
    assert item["derived_title"] is None and item["workflow_name"] is None
    assert item["source_assets"] == [] and item["poster_asset_id"] is None


def test_a_source_that_is_not_a_picture_is_no_poster(library):
    notes = asset_types.OutputInput(b"a note", "notes.txt", "text/plain")
    item = _item(library, _store_output(library, inputs=(notes,)))
    assert item["derived_title"] == "notes → 3D model"
    assert item["poster_asset_id"] is None


def test_an_input_that_cannot_be_stored_does_not_cost_the_output(library, monkeypatch):
    real = proxy.store_asset_bytes

    def refuse_pictures(content, **options):
        if options.get("kind") == "img":
            raise OSError("disk full")
        return real(content, **options)

    monkeypatch.setattr(proxy, "store_asset_bytes", refuse_pictures)
    item = _item(library, _store_output(library))
    assert item["kind"] == "model" and item["source_assets"] == []


def test_the_download_name_comes_from_the_stored_suffix(library):
    made = _item(library, _store_output(library))
    assert made["file_name"] == "garden-gnome.glb"
    plain = _item(library, _store(library, b"hello", ".zzunknown"))
    assert plain["file_name"] == "File.zzunknown"


@pytest.mark.parametrize("stem,suffix,media_type,expected", [
    ("My model", ".glb", "model/gltf-binary", "My model.glb"),
    ("a/b\\c", "", "model/gltf-binary", "a-b-c.glb"),
    ("", "", "smell/vapor-profile", "download.bin"),
    ("clip", "", "audio/mpeg", "clip.mp3"),
])
def test_download_file_name_uses_the_suffix_or_the_media_type(stem, suffix, media_type, expected):
    assert asset_types.download_file_name(stem, suffix, media_type) == expected


def test_the_media_list_names_each_kind_once_for_the_web(library):
    _store(library, GLB, ".glb")
    _store(library, b"hello", ".zzunknown")
    kinds = library.client.get("/me/api/media").json()["kinds"]
    assert kinds == [
        {"kind": "file", "label": "File", "plural": "Files", "untitled": "File", "input": "file"},
        {"kind": "model", "label": "3D model", "plural": "3D models", "untitled": "3D model", "input": "3D model"},
    ]


def test_a_kind_with_no_row_is_named_from_its_own_type():
    assert asset_types.kind_names("smell") == {
        "kind": "smell", "label": "Smell", "plural": "Smell", "untitled": "Smell", "input": "smell",
    }


@pytest.mark.parametrize("media_type,word,fmt", [
    ("image/png", "picture", "PNG"),
    ("audio/mpeg", "recording", "MP3"),
    ("video/mp4", "clip", "MP4"),
    ("model/gltf-binary", "3D model", "GLB"),
    ("application/octet-stream", "file", None),
    (None, "file", None),
])
def test_a_file_is_named_from_its_media_type_by_the_one_kind_table(media_type, word, fmt):
    assert asset_types.file_words(media_type) == {"word": word, "format": fmt}


def test_the_input_word_of_a_modality_comes_from_the_kind_table():
    assert [asset_types.input_word(m) for m in ("image", "audio", "video", "Smell")] == ["picture", "recording", "clip", "smell"]


def test_the_poster_is_chosen_by_the_stored_media_type_not_by_the_id():
    types = {"ast_a_picture": "image/png", "img_not_a_picture": "text/plain"}
    sources = [me_media._source_item({"asset_id": asset_id, "name": asset_id}, lambda asset_id, path: types[asset_id],
                                     lambda asset_id: None)
               for asset_id in ("img_not_a_picture", "ast_a_picture")]
    assert [source["kind"] for source in sources] == ["text", "image"]
    assert me_media._poster_id(sources) == "ast_a_picture"
    assert me_media._poster_id(sources[:1]) is None


def test_the_detail_route_names_the_kind_and_the_sources(library):
    asset_id = _store_output(library)
    body = library.client.get(f"/me/api/media/{asset_id}").json()
    assert body["asset"]["asset_id"] == asset_id
    assert body["kinds"] == [asset_types.kind_names("model")]
    assert body["asset"]["source_assets"][0]["word"] == "picture"


# --- a file stored only as an input is reached from the item that uses it, never listed -------------------------------

def _listed_ids(library, query=""):
    """Every id the list names, page by page, as the web pages through it."""
    ids, cursor = [], None
    while True:
        url = f"/me/api/media?limit=1{query}" + (f"&cursor={cursor}" if cursor else "")
        body = library.client.get(url).json()
        ids += [item["asset_id"] for item in body["assets"]]
        cursor = body.get("next_cursor")
        if not cursor:
            return ids


def test_a_source_only_picture_is_listed_by_no_filter_and_no_page(library):
    made = _store_output(library)
    source_id = proxy._read_asset_metadata(made)["origin"]["sources"][0]["asset_id"]
    for query in ("", "&kind=image", "&kind=model"):
        assert source_id not in _listed_ids(library, query)
    assert _listed_ids(library) == [made]
    assert _listed_ids(library, "&kind=image") == []
    # The count and the filter chips follow: there is no Images kind when nothing in the Library is one.
    body = library.client.get("/me/api/media").json()
    assert [kind["kind"] for kind in body["kinds"]] == ["model"]


def test_a_source_only_picture_still_opens_from_the_item_that_uses_it(library):
    made = _store_output(library)
    [source] = _item(library, made)["source_assets"]
    opened = library.client.get(f"/me/api/media/{source['asset_id']}")
    assert opened.status_code == 200 and opened.json()["asset"]["kind"] == "image"


def test_a_picture_that_was_made_stays_listed_when_a_later_run_uses_it(library):
    first = _store_output(library, data=PNG + b"made", suffix=".png", inputs=())
    again = asset_types.OutputInput(PNG + b"made", "first.png", "image/png")
    second = _store_output(library, inputs=(again,))
    assert first in _listed_ids(library, "&kind=image")
    assert second in _listed_ids(library, "&kind=model")


def test_a_video_poster_is_listed_by_no_filter_and_no_page():
    def entry(asset_id, **fields):
        return me_media._Entry(asset_id=asset_id, kind="image", created_at=1, model=None, workflow=None, prompt=None,
                               source_id=None, build=lambda probe: {}, **fields)

    kept = me_media._listed_entries([
        entry("vid_a", posters=("img_poster",)), entry("img_poster"), entry("img_other"),
    ])
    assert [e.asset_id for e in kept] == ["vid_a", "img_other"]


# --- the kind words are shipped data: a kind added only in the data file is named, filtered and listed -------------------

def test_the_shipped_kind_file_holds_every_word_of_every_kind():
    table = asset_types.load_kind_names()
    assert table == asset_types.KIND_NAMES
    assert {"image", "video", "audio", "model", "text", "file"} <= set(table)
    assert all(set(asset_types.KIND_WORDS) <= set(words) <= set(asset_types.KIND_WORDS) | {"prefix"}
               for words in table.values())


@pytest.mark.parametrize("text", [
    "kinds: {}\n",
    "kinds: {smell: {label: Smell, plural: Smells, untitled: Smell}}\n",
    "kinds: {smell: {label: Smell, plural: Smells, untitled: Smell, input: smell, extra: x}}\n",
    "kinds: {smell: {label: Smell, plural: Smells, untitled: '', input: smell}}\n",
    "kinds: {Smell: {label: Smell, plural: Smells, untitled: Smell, input: smell}}\n",
    "kinds: {smell: {label: Smell, plural: Smells, untitled: Smell, input: smell, prefix: ast}}\n",
    "other: 1\n",
])
def test_a_malformed_kind_file_is_refused(tmp_path, text):
    path = tmp_path / "asset_kinds.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="asset_kinds.yml"):
        asset_types.load_kind_names(path)


def test_a_kind_added_only_in_the_data_file_is_named_filtered_and_listed(library, tmp_path, monkeypatch):
    data = tmp_path / "asset_kinds.yml"
    data.write_text(asset_types.KIND_NAMES_PATH.read_text(encoding="utf-8")
                    + "  smell: {label: Aroma, plural: Aromas, untitled: Scent, input: aroma sample}\n", encoding="utf-8")
    monkeypatch.setattr(asset_types, "KIND_NAMES", asset_types.load_kind_names(data))
    # The one other place a new type is one row, as the module says: a suffix that stands for the type.
    monkeypatch.setitem(asset_types.SUFFIX_MEDIA_TYPES, ".aroma", "smell/aroma-bundle")
    assert asset_types.kind_names("smell") == {
        "kind": "smell", "label": "Aroma", "plural": "Aromas", "untitled": "Scent", "input": "aroma sample"}
    assert asset_types.input_word("smell") == "aroma sample"
    assert asset_types.file_words("smell/aroma-bundle")["word"] == "aroma sample"
    smell = _store(library, b"vapor", ".aroma")
    _store(library, GLB, ".glb")
    listing = library.client.get("/me/api/media").json()
    assert next(i for i in listing["assets"] if i["asset_id"] == smell)["kind"] == "smell"
    assert {"kind": "smell", "label": "Aroma", "plural": "Aromas", "untitled": "Scent", "input": "aroma sample"} in listing["kinds"]
    filtered = library.client.get("/me/api/media?kind=smell").json()
    assert [i["asset_id"] for i in filtered["assets"]] == [smell]


# --- stored-id prefixes are kind data; callers retain their distinct unknown-type defaults ----------------------------

def test_the_shipped_asset_prefixes_keep_the_existing_ids_and_kind_name_shape():
    assert asset_types.ASSET_PREFIX_BY_TOP_TYPE == {"image": "img", "audio": "aud", "video": "vid"}
    assert asset_types.asset_prefixes_by_top_type(asset_types.load_kind_names()) == asset_types.ASSET_PREFIX_BY_TOP_TYPE
    assert asset_types.OTHER_ASSET_PREFIX == "ast"
    assert asset_types.asset_prefix_for_media_type("model/gltf-binary") == "ast"
    assert asset_types.asset_prefix_for_media_type("smell/aroma-bundle") == "ast"
    for kind, prefix in asset_types.ASSET_PREFIX_BY_TOP_TYPE.items():
        assert asset_types.asset_prefix_for_media_type(f"{kind}/example") == prefix
        assert set(asset_types.kind_names(kind)) == {"kind", *asset_types.KIND_WORDS}


@pytest.mark.parametrize("prefix", [
    None, True, 7, "", "Msh", "msh1", "m_sh", "m-sh", "m sh", "meshmeshx", "mésh", "msh ",
])
def test_a_malformed_asset_prefix_is_refused(tmp_path, prefix):
    path = tmp_path / "asset_kinds.yml"
    row = {"label": "Mesh", "plural": "Meshes", "untitled": "Mesh", "input": "mesh", "prefix": prefix}
    path.write_text(yaml.safe_dump({"kinds": {"mesh": row}}), encoding="utf-8")
    with pytest.raises(ValueError, match="asset_kinds.yml:.*prefix"):
        asset_types.load_kind_names(path)


def test_duplicate_asset_prefixes_are_refused(tmp_path):
    path = tmp_path / "asset_kinds.yml"
    row = {"label": "Mesh", "plural": "Meshes", "untitled": "Mesh", "input": "mesh", "prefix": "msh"}
    path.write_text(yaml.safe_dump({"kinds": {"mesh": row, "other": dict(row)}}), encoding="utf-8")
    with pytest.raises(ValueError, match="asset_kinds.yml:.*duplicate prefix"):
        asset_types.load_kind_names(path)


def test_a_prefix_added_only_in_the_kind_file_is_used(tmp_path, monkeypatch):
    path = tmp_path / "asset_kinds.yml"
    path.write_text(asset_types.KIND_NAMES_PATH.read_text(encoding="utf-8")
                    + "  mesh: {label: Mesh, plural: Meshes, untitled: Mesh, input: mesh, prefix: msh}\n",
                    encoding="utf-8")
    kinds = asset_types.load_kind_names(path)
    monkeypatch.setattr(asset_types, "KIND_NAMES", kinds)
    monkeypatch.setattr(asset_types, "ASSET_PREFIX_BY_TOP_TYPE", asset_types.asset_prefixes_by_top_type(kinds))
    assert asset_types.asset_prefix_for_media_type("mesh/example") == "msh"
    assert workflow_call.kind_for("mesh/example") == "msh"
    assert "prefix" not in asset_types.kind_names("mesh")
    assert asset_types.asset_prefix_for_media_type("model/gltf-binary") == "ast"
    assert workflow_call.kind_for("model/gltf-binary") == ""


@pytest.mark.parametrize("media_type,prefix", [
    ("image/png", "img"),
    ("audio/mpeg", "aud"),
    ("video/mp4", "vid"),
    ("model/gltf-binary", ""),
    ("smell/aroma-bundle", ""),
    ("", ""),
    ("nonsense", ""),
    ("IMAGE/png", ""),
])
def test_workflow_asset_prefixes_keep_the_empty_unknown_default(media_type, prefix):
    assert workflow_call.kind_for(media_type) == prefix


def test_the_stored_id_tables_derive_from_the_asset_kind_rows():
    # Literal oracle: the tables the gateway and the member media module hand-kept before they came from data.
    suffixes = {
        "img": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
        "aud": (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"),
        "vid": (".mp4", ".webm", ".mov", ".mkv"),
        "ast": (),
    }
    assert asset_types.asset_suffixes_by_prefix() == suffixes
    assert list(asset_types.asset_suffixes_by_prefix()) == list(suffixes)
    assert asset_types.asset_kind_names_by_prefix() == {"img": "image", "aud": "audio", "vid": "video"}
    assert proxy._ASSET_SUFFIXES_BY_KIND == me_media._ASSET_SUFFIXES_BY_KIND == suffixes
    assert proxy._ASSET_KIND_NAMES == {"img": "image", "aud": "audio", "vid": "video"}
