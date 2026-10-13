"""Kill tests for public_failure survivors that mut3 called equivalent (mut4 review).

Each docstring names the mutant it kills. Inputs go through the public projection.
"""

from __future__ import annotations

from gateway.public_failure import project_failure

_UNKNOWN = "We couldn't tell where this request stopped. Try again."


def test_an_unhashable_code_is_reported_as_unknown_not_raised():
    """Kills public_failure.py:170 ``and`` -> ``or``: the mutant looks the list up in the copy table."""
    for source in ({"code": []}, {"code": {"a": 1}}, {"code": ["prompt_too_long"]}):
        projected = project_failure(source)
        assert projected["message"] == _UNKNOWN
        assert projected["details"]["copy_key"] == "unknown"


def test_an_unhashable_copy_key_in_details_is_ignored():
    """Kills public_failure.py:170 ``and`` -> ``or`` through the second operand of the key tuple."""
    projected = project_failure({"code": "prompt_required", "details": {"copy_key": []}})
    assert projected["details"]["copy_key"] == "prompt_required"


def test_the_character_limit_in_details_reaches_the_sentence():
    """Kills public_failure.py:172 ``"max_chars"`` renamed: the limit would be dropped and the copy replaced by unknown."""
    projected = project_failure({"code": "prompt_too_long", "details": {"max_chars": 10}})
    assert projected["message"] == "The description is too long. Keep it to 10 characters or fewer."
    assert projected["details"]["max_chars"] == 10


def test_the_picture_limit_in_details_reaches_the_sentence():
    """Kills public_failure.py:172 ``"max_images"`` renamed."""
    projected = project_failure({"code": "invalid_n", "details": {"max_images": 2}})
    assert projected["message"] == "That number of pictures isn't allowed. Choose from 1 to 2."
    assert projected["details"]["max_images"] == 2


def test_the_api_wording_of_an_unknown_target_is_recognised_from_its_message():
    """Kills public_failure.py:177 ``"api"`` renamed: only member wording would be matched, so this message becomes unknown."""
    message = "Unknown target. Call capabilities to list valid targets."
    projected = project_failure({"message": message})
    assert projected["details"]["copy_key"] == "target_unknown"
