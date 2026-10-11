"""Phase 2 engine-knowledge migrations preserve the owner's observable behavior.

``tests/engine_p2_differential.py`` drives the real routes. The golden file holds
what the pre-migration code sent and answered for the owner's sanitized
configuration; the migrated code with the migrated configuration must match it
byte for byte, apart from the intended changes listed here.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import yaml

from tests.engine_p2_differential import FIXTURES, load_fixture, observe

_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))


def _mismatches(section: str, observed: dict, expected_changes: dict | None = None) -> list[str]:
    golden = _GOLDEN[section]
    changes = expected_changes or {}
    problems = [f"case set differs: {sorted(set(golden) ^ set(observed))}"] if set(golden) != set(observed) else []
    for case in sorted(set(golden) & set(observed)):
        expected = changes.get(case, golden[case])
        if observed[case] != expected:
            problems.append(f"{case}:\n  expected {json.dumps(expected)[:600]}\n  observed {json.dumps(observed[case])[:600]}")
    return problems


class MigratedConfigurationTests(unittest.TestCase):
    def test_the_proven_configuration_is_the_shipped_configuration(self) -> None:
        shipped = yaml.safe_load((_ROOT / "proxy_config.yml").read_text(encoding="utf-8"))
        migrated = load_fixture("owner_config.migrated.json")
        self.assertEqual(migrated["model_aliases"]["speech"], shipped["model_aliases"]["speech"])
        self.assertEqual(migrated["voices"], shipped["voices"])


class VoiceDesignDifferentialTests(unittest.TestCase):
    def test_owner_configuration_designs_byte_identical_requests(self) -> None:
        observed = observe(load_fixture("owner_config.migrated.json"), ("voice_design",))["voice_design"]
        problems = _mismatches("voice_design", observed)
        self.assertFalse(problems, "\n".join(problems[:20]))

    def test_a_replacement_design_engine_without_a_vocabulary_receives_the_free_text(self) -> None:
        fixture = copy.deepcopy(load_fixture("owner_config.migrated.json"))
        engines = fixture["model_aliases"]["speech"]["engines"]
        engines["designer"] = {"service": engines["omnivoice"]["service"], "model": "designer-1"}
        fixture["voices"]["design_engine"] = "designer"
        observed = observe(fixture, ("voice_design",))["voice_design"]
        case = observed["prompt[" + str(_prompt_index("A warm, calm young woman with a bright voice and an Australian accent")) + "] "
                        "'A warm, calm young woman with a bright voice and an Australian accent'"]
        self.assertEqual(case["response"]["json"]["design_instruct"], "A warm, calm young woman with a bright voice and an Australian accent")
        self.assertEqual(case["response"]["json"]["warnings"], ["quality_check_unavailable"])
        body = json.loads(case["upstream"][0]["body"])
        self.assertEqual(list(body), ["input", "instructions", "seed"])
        self.assertEqual(body["instructions"], "A warm, calm young woman with a bright voice and an Australian accent")
        nothing = observed["prompt[" + str(_prompt_index("warm and calm")) + "] 'warm and calm'"]
        self.assertEqual(nothing["response"]["status"], 200)

    def test_without_a_design_engine_voice_design_is_unconfigured_not_guessed(self) -> None:
        fixture = copy.deepcopy(load_fixture("owner_config.migrated.json"))
        del fixture["voices"]["design_engine"]
        observed = observe(fixture, ("voice_design",))["voice_design"]
        case = observed["explicit script"]
        self.assertEqual(case["upstream"], [])
        self.assertEqual(case["response"]["status"], 503)
        self.assertIn("not configured", case["response"]["json"]["error"]["message"])


def _prompt_index(prompt: str) -> int:
    from tests.engine_p2_differential import _DESIGN_PROMPTS

    return _DESIGN_PROMPTS.index(prompt)


if __name__ == "__main__":
    unittest.main()
