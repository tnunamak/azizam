"""Adapter pick-lists: what an operator installed, narrowed to files whose header names the workflow's base model.

Files are invented. No engine is called: the header and list readers are fakes.
"""
import asyncio

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
                                    table=TABLE, marks=kw.get("marks"), now=kw.get("now", 1000.0))), calls


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
