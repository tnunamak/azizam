"""Keep retired product contracts out of tracked files.

The exceptions below describe durable contracts or rejection/migration fixtures.
They do not exempt a directory of production code or all tests.
"""

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOKENS = (
    "AI_GATEWAY_", "OPENAI_PROXY_", "VIVID_", "AGW_", "vivid_fish",
    "vivid_object", "vivid_status", "x-vivid-", "x-vivid-fish-",
    "x-vividfish-", "x-ai-gateway-", "x-gateway-", "x_ai_gateway_",
    "com.vividfish.", "openai_proxy_policy", "ai-gateway-native",
    "openai-proxy", "openai_proxy", "ai-gateway", "/.well-known/ai-gateway", "vivid-fish", "Vivid Fish",
    "vividfish", "vivid.fish",
)
PATTERN = re.compile("|".join(re.escape(token) for token in sorted(TOKENS, key=len, reverse=True)), re.IGNORECASE)
HISTORY = ("inbox/", "ai/research/", "docs/design/")
FIXTURES = {
    "scripts/migrations/retire_legacy_names.py": {
        "AI_GATEWAY_", "OPENAI_PROXY_", "VIVID_", "AGW_", "openai-proxy", "ai-gateway-native",
    },
    "tests/test_retire_legacy_names_migration.py": {
        "AI_GATEWAY_", "OPENAI_PROXY_", "VIVID_", "AGW_", "openai-proxy", "ai-gateway-native",
    },
    "tests/test_product_compat.py": {
        "AI_GATEWAY_", "OPENAI_PROXY_", "VIVID_", "AGW_", "com.vividfish.",
        "openai-proxy", "/.well-known/ai-gateway", "vividfish",
    },
    "tests/test_rename_review.py": {
        "vivid_fish", "com.vividfish.", "vivid_object", "vivid_status", "x-vividfish-",
        "AI_GATEWAY_", "OPENAI_PROXY_", "VIVID_",
    },
    "tests/test_chat_extensions.py": {"com.vividfish."},
    "tests/test_chat_backend_adapter.py": {"x-ai-gateway-"},
    "tests/test_llm_execution_adapter.py": {"x-ai-gateway-"},
    "tests/test_media_provenance.py": {"com.vividfish.", "vividfish"},
    "tests/test_model_metadata.py": {"vivid_fish"},
    "tests/test_provider_sidecar.py": {"AI_GATEWAY_"},
}
BRAND_REFERENCES = re.compile(r"@vivid-fish/brand|(?:~/code/|/|\")vivid-fish-brand|Vivid-Fish/vivid-fish-brand", re.IGNORECASE)


def exception_reason(path: str, line: str, token: str) -> str | None:
    if path.startswith(HISTORY):
        return "preserved history"
    if path == "tests/test_legacy_name_boundary.py":
        return "guard token definitions and fixtures"
    if token.lower() == "vivid.fish":
        return "retained hostname or deployment wordmark"
    if path in {"LICENSE", "integrations/pi/LICENSE"} and "Copyright" in line:
        return "original license copyright"
    if token.lower() in {"vivid-fish"} and BRAND_REFERENCES.search(line):
        return "separate brand package/repository"
    if path == "gateway/provenance.py" and token == "vividfish" and '"vividfish_provenance"' in line:
        return "permanent media provenance reader"
    if token.lower() in {item.lower() for item in FIXTURES.get(path, set())}:
        return "explicit rejection, arbitrary-payload preservation, or migration fixture"
    return None


def tracked_hits(root: Path = ROOT):
    files = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True).stdout
    for raw in files.split(b"\0"):
        if not raw:
            continue
        path = raw.decode()
        target = root / path
        if not target.is_file():  # deletions in an uncommitted worktree
            continue
        content = target.read_bytes()
        if b"\0" in content:
            continue
        for number, line in enumerate(content.decode("utf-8", errors="replace").splitlines(), 1):
            for match in PATTERN.finditer(line):
                token = match[0]
                yield path, number, token, exception_reason(path, line, token)


class LegacyNameBoundaryTests(unittest.TestCase):
    def test_every_tracked_legacy_token_has_an_explicit_exception(self):
        failures = [f"{path}:{number}: {token}" for path, number, token, reason in tracked_hits() if reason is None]
        self.assertEqual(failures, [], "\n".join(failures))

    def test_keep_set_does_not_allow_retired_runtime_contracts(self):
        for path, line in (
            ("gateway/new.py", 'os.getenv("OPENAI_PROXY_API_KEY")'),
            ("gateway/provenance.py", 'value.get("com.vividfish.workflow")'),
            ("web/src/app/home.tsx", 'localStorage.getItem("vivid-fish-key")'),
            ("gateway/brand_css.py", 'SERVICE = "openai-proxy"'),
            ("tests/test_unrelated.py", 'assert body["vivid_fish_alias"]'),
        ):
            with self.subTest(path=path, line=line):
                hits = list(PATTERN.finditer(line))
                self.assertTrue(hits)
                self.assertTrue(all(exception_reason(path, line, match[0]) is None for match in hits))


if __name__ == "__main__":
    unittest.main()
