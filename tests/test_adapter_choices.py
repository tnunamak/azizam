"""Adapter pick-lists: what an operator installed, narrowed to files whose header names the workflow's base model.

Files are invented. No engine is called: the header and list readers are fakes.
"""
import asyncio
from threading import Event

import pytest

from gateway import adapter_choices as ac
from gateway.manifests import ContractError, validate_input_value, value_problems

TABLE = {"base_a": {"ss_base_model_version": ["base_a"]}, "base_b": {"ss_base_model_version": ["base_b", "base_b_v2"]}}

LORA_CHAIN = {"node": "7", "input": "model",
              "chain": {"node_type": "AdapterLoader", "link": "model",
                        "fields": {"name": "adapter_name", "weight": "strength"}}}


def contract(max_items=3, encoder_enum=("default_encoder.safetensors",)):
    return {
        "inputs": {
            "prompt": {"type": "string", "required": True},
            "loras": {"type": "array", "max_items": max_items,
                      "items": {"type": "object", "properties": {
                          "name": {"type": "string", "required": True},
                          "weight": {"type": "number", "minimum": -4, "maximum": 4, "default": 1.0}}}},
            "text_encoder": {"type": "string", "enum": list(encoder_enum)},
        },
        "bindings": {"graph": {
            "adapter_architecture": "base_a", "adapter_lora_folder": "loras",
            "map": {"loras": [LORA_CHAIN], "text_encoder": [{"node": "2", "input": "clip_name"}]},
        }},
    }


def test_the_declaration_is_read_from_the_binding_data():
    decl = ac.declaration(contract(), "graph")
    assert (decl.architecture, decl.folder) == ("base_a", "loras")
    assert (decl.node_type, decl.listing_input, decl.encoder_input) == ("AdapterLoader", "adapter_name", "text_encoder")
    assert decl.default_encoders == ("default_encoder.safetensors",)


def test_a_binding_with_no_architecture_or_no_chain_has_no_declaration():
    plain = contract()
    del plain["bindings"]["graph"]["adapter_architecture"]
    assert ac.declaration(plain, "graph") is None
    chainless = contract()
    chainless["bindings"]["graph"]["map"].pop("loras")
    assert ac.declaration(chainless, "graph") is None
    assert ac.declaration(contract(), "missing") is None


def test_only_files_whose_header_names_the_architecture_are_offered_in_installed_order():
    installed = ["z.safetensors", "a.safetensors", "other.safetensors", "bare.safetensors", "two.safetensors", "gone.safetensors"]
    headers = {
        "z.safetensors": {"ss_base_model_version": "base_a"},
        "a.safetensors": {"ss_base_model_version": "base_a", "unrelated": "x"},
        "other.safetensors": {"ss_base_model_version": "base_b"},
        "bare.safetensors": {"format": "pt"},
        "two.safetensors": {"ss_base_model_version": "base_b_v2"},
        "gone.safetensors": None,
    }
    assert ac.compatible(installed, headers, "base_a", TABLE, marked=()) == ["z.safetensors", "a.safetensors"]
    assert ac.compatible(installed, headers, "base_b", TABLE, marked=()) == ["other.safetensors", "two.safetensors"]


def test_an_owner_mark_adds_a_file_with_no_usable_header_but_never_one_that_is_not_installed():
    installed = ["bare.safetensors", "wrong.safetensors"]
    headers = {"bare.safetensors": {"format": "pt"}, "wrong.safetensors": {"ss_base_model_version": "base_b"}}
    assert ac.compatible(installed, headers, "base_a", TABLE, marked=["bare.safetensors", "not-installed.safetensors"]) \
        == ["bare.safetensors"]


def test_an_architecture_the_table_does_not_know_offers_only_marked_files():
    installed = ["a.safetensors", "b.safetensors"]
    headers = {"a.safetensors": {"ss_base_model_version": "base_a"}, "b.safetensors": None}
    assert ac.compatible(installed, headers, "unlisted", TABLE, marked=["b.safetensors"]) == ["b.safetensors"]


def test_a_header_value_that_is_not_text_names_nothing():
    assert ac.compatible(["a.safetensors"], {"a.safetensors": {"ss_base_model_version": ["base_a"]}}, "base_a", TABLE,
                         marked=()) == []


def test_marks_are_read_by_architecture_and_a_malformed_setting_marks_nothing():
    setting = {"marks": {"base_a": {"loras": ["x.safetensors"]}, "base_b": {"loras": 5}}}
    assert ac.marked(setting, "base_a", "loras") == ["x.safetensors"]
    assert ac.marked(setting, "base_b", "loras") == []
    assert ac.marked(None, "base_a", "loras") == [] and ac.marked({"marks": []}, "base_a", "loras") == []


def overlaid(loras=("a.safetensors", "b.safetensors"), encoders=("default_encoder.safetensors", "second_encoder.safetensors"), **kw):
    c = contract(**kw)
    return ac.overlay(c, ac.declaration(c, "graph"), ac.AdapterChoices(tuple(loras), tuple(encoders))), c


def test_the_overlay_puts_the_choices_in_the_schema_and_leaves_the_manifest_alone():
    shown, original = overlaid()
    assert shown["inputs"]["loras"]["items"]["properties"]["name"]["enum"] == ["a.safetensors", "b.safetensors"]
    assert shown["inputs"]["text_encoder"]["enum"] == ["default_encoder.safetensors", "second_encoder.safetensors"]
    assert "enum" not in original["inputs"]["loras"]["items"]["properties"]["name"]
    assert original["inputs"]["text_encoder"]["enum"] == ["default_encoder.safetensors"]


def test_with_no_compatible_file_the_list_takes_no_items():
    shown, _ = overlaid(loras=())
    assert shown["inputs"]["loras"]["max_items"] == 0


class Outcome:
    def __init__(self, rejected, errors):
        self.rejected, self.errors = rejected, errors


def run(shown, supplied):
    """The checks a request gets, input by input, against the contract the overlay made."""
    rejected, errors = {}, []
    for name, value in supplied.items():
        spec = shown["inputs"].get(name)
        if spec is None:
            continue
        reason = validate_input_value(name, value, spec)
        if reason is not None:
            rejected[name] = reason
            errors.extend(ContractError(*item) for item in value_problems(spec, value, ("inputs", name)))
    return Outcome(rejected, errors)


def test_a_chosen_adapter_and_encoder_pass_with_a_weight_in_bounds():
    shown, _ = overlaid()
    result = run(shown, {"prompt": "x", "loras": [{"name": "a.safetensors", "weight": 4}, {"name": "b.safetensors"}],
                         "text_encoder": "second_encoder.safetensors"})
    assert not result.rejected


def test_a_name_that_is_not_offered_is_refused_and_the_message_lists_what_is():
    shown, _ = overlaid()
    result = run(shown, {"prompt": "x", "loras": [{"name": "other.safetensors"}]})
    assert result.errors[0].path == ("inputs", "loras", 0, "name")
    assert "a.safetensors" in result.errors[0].message and "b.safetensors" in result.errors[0].message
    assert "other.safetensors" not in result.errors[0].message


def test_an_encoder_that_is_not_offered_is_refused_and_the_message_lists_what_is():
    shown, _ = overlaid()
    reason = run(shown, {"prompt": "x", "text_encoder": "other.safetensors"}).rejected["text_encoder"]
    assert "default_encoder.safetensors" in reason and "second_encoder.safetensors" in reason


@pytest.mark.parametrize("weight", [4.01, -4.5, 100])
def test_a_weight_outside_minus_four_to_four_is_refused(weight):
    shown, _ = overlaid()
    result = run(shown, {"prompt": "x", "loras": [{"name": "a.safetensors", "weight": weight}]})
    assert result.errors[0].path == ("inputs", "loras", 0, "weight")


def test_more_than_the_declared_count_is_refused():
    shown, _ = overlaid(loras=("a.safetensors",), max_items=2)
    assert run(shown, {"prompt": "x", "loras": [{"name": "a.safetensors"}] * 3}).rejected


# ---- the source: what the engine lists, and each file's header -----------------------------------------------------

def make_fetchers(files, headers, calls=None):
    calls = calls if calls is not None else []

    def listing(origin, node_type, input_name):
        calls.append(("list", origin, node_type, input_name))
        if files is None:
            raise OSError("engine down")
        return list(files)

    def header(origin, folder, name):
        calls.append(("header", folder, name))
        value = headers[name]
        if isinstance(value, Exception):
            raise value
        return value

    return listing, header, calls


@pytest.fixture(autouse=True)
def fresh_cache():
    ac.reset()
    yield
    ac.reset()


def source(files, headers, calls=None, **kw):
    listing, header, calls = make_fetchers(files, headers, calls)
    decl = ac.declaration(contract(), "graph")
    return asyncio.run(ac.installed(decl, service_id="svc", origin="http://engine", list_files=listing, read_header=header,
                                    table=TABLE, marks=kw.get("marks"), now=kw.get("now", 1000.0),
                                    wanted=kw.get("wanted", ()))), calls


def test_a_header_value_is_compared_without_case_spaces_or_punctuation():
    names = {"a.safetensors": "Base_A", "b.safetensors": "base-a", "c.safetensors": " BASE A ", "d.safetensors": "base_b"}
    headers = {name: {"ss_base_model_version": value} for name, value in names.items()}
    assert ac.compatible(list(names), headers, "base_a", TABLE, []) == ["a.safetensors", "b.safetensors", "c.safetensors"]
    assert ac.compatible(["x.safetensors"], {"x.safetensors": {"ss_base_model_version": "sdxl"}}, "base_a", TABLE, []) == []


def test_krea2_offers_krea2_spellings_from_the_shipped_table_and_still_hides_sdxl():
    values = {"a": "Krea2", "b": "krea-2", "c": " KREA2 ", "d": "sdxl"}
    headers = {name: {"ss_base_model_version": value} for name, value in values.items()}
    assert ac.compatible(list(values), headers, "krea2", ac.architecture_table(), []) == ["a", "b", "c"]


def test_member_copy_classifier_normalizes_engine_folder_spellings():
    assert ac.is_member_upload_copy("azizam-uploads/style.safetensors")
    assert ac.is_member_upload_copy("azizam-uploads\\style.safetensors")
    assert ac.is_member_upload_copy("AZIZAM-UPLOADS/style.safetensors")
    assert not ac.is_member_upload_copy("operator/style.safetensors")
    assert not ac.is_member_upload_copy("azizam-uploads-other/style.safetensors")


def test_the_installed_list_is_filtered_by_header_and_a_header_that_cannot_be_read_is_not_offered():
    headers = {"a.safetensors": {"ss_base_model_version": "base_a"}, "b.safetensors": {"ss_base_model_version": "base_b"},
               "c.safetensors": OSError("404")}
    choices, _ = source(["a.safetensors", "b.safetensors", "c.safetensors"], headers)
    assert choices.loras == ("a.safetensors",)


def test_the_encoder_choices_are_the_ones_the_manifest_lists_and_an_owner_mark_adds_a_lora_only():
    choices, _ = source(["a.safetensors"], {"a.safetensors": {"format": "pt"}},
                        marks={"marks": {"base_a": {"loras": ["a.safetensors"], "text_encoders": ["enc2.safetensors"]}}})
    assert choices.loras == ("a.safetensors",)
    assert choices.text_encoders == ("default_encoder.safetensors",)


def test_a_second_request_inside_the_age_limit_reads_nothing_from_the_engine():
    calls = []
    headers = {"a.safetensors": {"ss_base_model_version": "base_a"}}
    source(["a.safetensors"], headers, calls, now=1000.0)
    first = len(calls)
    source(["a.safetensors"], headers, calls, now=1000.0 + ac.MAX_AGE_S - 1)
    assert len(calls) == first


def test_after_the_age_limit_the_list_is_read_again_but_a_known_header_is_not():
    calls = []
    headers = {"a.safetensors": {"ss_base_model_version": "base_a"}}
    source(["a.safetensors"], headers, calls, now=1000.0)
    source(["a.safetensors"], headers, calls, now=1000.0 + ac.MAX_AGE_S + 1)
    assert [c[0] for c in calls] == ["list", "header", "list"]


def test_an_engine_that_cannot_list_keeps_the_last_good_choices_and_never_offers_an_empty_list_in_their_place():
    headers = {"a.safetensors": {"ss_base_model_version": "base_a"}}
    source(["a.safetensors"], headers, now=1000.0)
    choices, _ = source(None, headers, now=1000.0 + ac.MAX_AGE_S + 1)
    assert choices.loras == ("a.safetensors",)


def test_an_engine_that_cannot_list_and_was_never_read_offers_nothing_and_says_so():
    with pytest.raises(ac.ChoicesUnavailable):
        source(None, {})


def test_a_new_file_appears_after_the_age_limit():
    headers = {"a.safetensors": {"ss_base_model_version": "base_a"}, "n.safetensors": {"ss_base_model_version": "base_a"}}
    source(["a.safetensors"], headers, now=1000.0)
    choices, _ = source(["a.safetensors", "n.safetensors"], headers, now=1000.0 + ac.MAX_AGE_S + 1)
    assert choices.loras == ("a.safetensors", "n.safetensors")


def test_a_name_missing_from_a_list_older_than_ten_seconds_is_read_again_before_it_is_refused():
    headers = {"a.safetensors": {"ss_base_model_version": "base_a"}, "n.safetensors": {"ss_base_model_version": "base_a"}}
    source(["a.safetensors"], headers, now=1000.0)
    assert source(["a.safetensors", "n.safetensors"], headers, now=1005.0, wanted=["n.safetensors"])[0].loras == ("a.safetensors",)
    choices, _ = source(["a.safetensors", "n.safetensors"], headers, now=1011.0, wanted=["n.safetensors"])
    assert choices.loras == ("a.safetensors", "n.safetensors")


def test_a_name_that_is_listed_never_makes_an_early_re_read():
    headers = {"a.safetensors": {"ss_base_model_version": "base_a"}}
    calls = []
    source(["a.safetensors"], headers, calls, now=1000.0)
    source(["a.safetensors"], headers, calls, now=1100.0, wanted=["a.safetensors"])
    assert [c[0] for c in calls] == ["list", "header"]


def test_a_failed_list_read_is_not_retried_for_thirty_seconds_and_a_held_list_keeps_serving():
    headers = {"a.safetensors": {"ss_base_model_version": "base_a"}}
    source(["a.safetensors"], headers, now=1000.0)
    calls = []
    late = 1000.0 + ac.MAX_AGE_S + 1
    assert source(None, headers, calls, now=late)[0].loras == ("a.safetensors",)
    assert source(None, headers, calls, now=late + 29)[0].loras == ("a.safetensors",)
    assert [c[0] for c in calls] == ["list"]
    source(None, headers, calls, now=late + 31)
    assert [c[0] for c in calls] == ["list", "list"]


def test_with_no_list_held_a_failed_read_answers_unavailable_without_asking_the_engine_again_for_thirty_seconds():
    calls = []
    for at in (1000.0, 1010.0):
        with pytest.raises(ac.ChoicesUnavailable):
            source(None, {}, calls, now=at)
    assert [c[0] for c in calls] == ["list"]
    choices, _ = source(["a.safetensors"], {"a.safetensors": {"ss_base_model_version": "base_a"}}, calls, now=1031.0)
    assert choices.loras == ("a.safetensors",)


# ---- the listing --------------------------------------------------------------------------------------------------

class Record:
    id = "rec"
    contract = contract()


def listing(monkeypatch=None, **kw):
    return asyncio.run(ac.offered([Record()], "graph", service_id="svc", origin="http://engine", **kw))


def test_a_listing_with_no_reader_passed_never_calls_the_engine(monkeypatch):
    """No module state decides this (a switch a running app set once leaked across tests): only the readers the caller
    passes do, so the module defaults, which reach an engine, are never used by a listing that passes none."""
    called = []
    monkeypatch.setattr(ac, "default_list_files", lambda *a: called.append(a) or ["a.safetensors"])
    monkeypatch.setattr(ac, "default_read_header", lambda *a: called.append(a) or {})
    assert listing() == {} and called == []


def test_a_listing_that_outlasts_its_wait_returns_promptly_with_no_files_offered(monkeypatch):
    import time

    monkeypatch.setattr(ac, "LISTING_WAIT_S", 0.05)
    release = []

    def slow_list(*_):
        time.sleep(0.4)
        release.append(1)
        return ["a.safetensors"]

    async def timed():
        started = time.monotonic()
        found = await ac.offered([Record()], "graph", service_id="svc", origin="http://engine",
                                 list_files=slow_list, read_header=lambda *_: {})
        return found, time.monotonic() - started

    found, took = asyncio.run(timed())   # (asyncio.run itself then joins the sleeping thread; the listing did not wait)
    assert found == {} and took < 0.3


def test_header_reads_that_finish_after_the_listing_wait_are_kept_so_the_next_listing_does_not_repeat_them(monkeypatch):
    import time

    monkeypatch.setattr(ac, "architecture_table", lambda: TABLE)
    monkeypatch.setattr(ac, "LISTING_WAIT_S", 0.05)
    reads = []

    def slow_header(origin, folder, name):
        reads.append(name)
        time.sleep(0.3)
        return {"ss_base_model_version": "base_a"}

    def files(*_):
        return ["a.safetensors"]

    assert listing(list_files=files, read_header=slow_header) == {}
    time.sleep(0.5)   # the abandoned read finishes in its thread and writes the cache
    assert reads == ["a.safetensors"]
    shown = listing(list_files=files, read_header=slow_header)
    assert shown["rec"]["loras"]["items"]["properties"]["name"]["enum"] == ["a.safetensors"]
    assert reads == ["a.safetensors"]


def test_overlapping_installed_calls_share_one_list_read():
    async def exercise():
        entered, release = Event(), Event()
        calls = []
        listing, header, _ = make_fetchers(["a"], {"a": {"ss_base_model_version": "base_a"}}, calls)

        def held(*args):
            entered.set()
            assert release.wait(2)
            return listing(*args)

        async def installed():
            return await ac.installed(ac.declaration(contract(), "graph"), service_id="svc", origin="http://engine",
                                      list_files=held, read_header=header, table=TABLE, now=100)

        first = asyncio.create_task(installed())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(installed())
            await asyncio.sleep(0)  # The second request reaches its await while the source is held.
        finally:
            release.set()
        a, b = await asyncio.gather(first, second)
        assert a.loras == b.loras == ("a",)
        assert len([row for row in calls if row[0] == "list"]) == 1

    asyncio.run(exercise())


def test_overlapping_headers_share_one_file_read():
    async def exercise():
        entered, release = Event(), Event()
        calls = []
        _, header, _ = make_fetchers(["a"], {"a": {"ss_base_model_version": "base_a"}}, calls)

        def held(*args):
            entered.set()
            assert release.wait(2)
            return header(*args)

        async def headers():
            return await ac._headers("svc", "http://engine", "loras", ["a"], held, 100)

        first = asyncio.create_task(headers())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(headers())
            # _headers schedules a child for the file; let it reach the held source.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            release.set()
        a, b = await asyncio.gather(first, second)
        assert a == b == {"a": {"ss_base_model_version": "base_a"}}
        assert calls == [("header", "loras", "a")]

    asyncio.run(exercise())


def test_reset_fences_an_older_list_read_that_completes_last():
    async def exercise():
        entered, release = Event(), Event()
        decl = ac.declaration(contract(), "graph")
        _, header, _ = make_fetchers([], {name: {"ss_base_model_version": "base_a"} for name in ["old", "new"]})

        def old_list(*_):
            entered.set()
            assert release.wait(2)
            return ["old"]

        async def installed(listing, now):
            return await ac.installed(decl, service_id="svc", origin="http://engine", list_files=listing,
                                      read_header=header, table=TABLE, now=now)

        old = asyncio.create_task(installed(old_list, 100))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            ac.reset()
            assert (await installed(lambda *_: ["new"], 200)).loras == ("new",)
        finally:
            release.set()
        await old
        assert (await installed(lambda *_: pytest.fail("new list should be held"), 201)).loras == ("new",)

    asyncio.run(exercise())


def test_cancelled_list_waiter_does_not_cancel_the_shared_source():
    async def exercise():
        entered, release = Event(), Event()
        calls = []
        listing, header, _ = make_fetchers(["a"], {"a": {"ss_base_model_version": "base_a"}}, calls)

        def held(*args):
            entered.set()
            assert release.wait(2)
            return listing(*args)

        async def installed():
            return await ac.installed(ac.declaration(contract(), "graph"), service_id="svc", origin="http://engine",
                                      list_files=held, read_header=header, table=TABLE, now=100)

        first = asyncio.create_task(installed())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(first, 0.01)
            second = asyncio.create_task(installed())
            await asyncio.sleep(0)
        finally:
            release.set()
        assert (await second).loras == ("a",)
        assert len([row for row in calls if row[0] == "list"]) == 1

    asyncio.run(exercise())


def test_shipped_architecture_rows_do_not_alias_after_normalization():
    observed = set()
    for identifier, selectors in ac.architecture_table().items():
        normalized = {ac._plain(value) for values in selectors.values() for value in values}
        assert observed.isdisjoint(normalized), identifier
        observed.update(normalized)


# ---- receipts -------------------------------------------------------------------------------------------------------

def test_the_receipt_facts_name_each_file_with_its_weight_and_the_encoder():
    decl = ac.declaration(contract(), "graph")
    facts = ac.receipt_facts(decl, {"prompt": "x", "loras": [{"name": "a.safetensors", "weight": 0.5}, {"name": "b.safetensors"}],
                                    "text_encoder": "second_encoder.safetensors"}, contract())
    assert facts == {"loras": [{"name": "a.safetensors", "weight": 0.5}, {"name": "b.safetensors", "weight": 1.0}],
                     "text_encoder": "second_encoder.safetensors"}


def test_the_receipt_has_no_adapter_facts_when_none_were_chosen():
    decl = ac.declaration(contract(), "graph")
    assert ac.receipt_facts(decl, {"prompt": "x"}, contract()) is None
    assert ac.receipt_facts(None, {"loras": [{"name": "a.safetensors"}]}, contract()) is None

def test_adapter_overlay_disambiguates_upload_display_labels():
    operator = "style-a.safetensors"
    first, second, third, unique = ("upload-first-3f2a", "upload-second-3f2a", "upload-third-8b3c", "upload-unique-1111")
    loras = (operator, first, second, third, unique)
    labels = {first: "watercolour", second: "watercolour", third: operator, unique: "watercolour (…3f2a)"}
    choices = ac.AdapterChoices(loras=loras, text_encoders=(), uploads=loras[1:], labels=labels)
    original = contract()
    shown = ac.overlay(original, ac.declaration(original, "graph"), choices)
    name = shown["inputs"]["loras"]["items"]["properties"]["name"]
    assert name["enum"] == list(loras)
    presented = [name["choice_labels"].get(key, key) for key in loras]
    assert len(set(presented)) == len(loras)
    assert presented[0] == operator
    assert name["choice_labels"][unique] == labels[unique]
    assert all(name["choice_labels"][key].startswith(labels[key]) for key in choices.uploads)
    assert "choice_labels" not in original["inputs"]["loras"]["items"]["properties"]["name"]
