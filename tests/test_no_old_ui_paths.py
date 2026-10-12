"""No producer links to a browser path that the move to the root removed.

The web app moved from /ui to /. Old /ui URLs, the server portal and admin
aliases, /r/{id} receipt links, /firehose, ?operator=1 and the deleted Home,
Gallery and Services pages must not reappear outside the historical records
listed below (docs/design/CONNECT_ROOT_MAIL.md §2.6, completion check).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPE = ("proxy.py", "gateway", "firehose_render.py", "web/src", "web/scripts", "web/next.config.ts",
         "web/e2e", "web/tests", "scripts", "docs", "tests")
PATTERNS = {
    "ui": re.compile(r"""(^|["'`(=\s:,])/ui([/"'`?#)\s]|$)"""),
    "aliases": re.compile(r'''"/(admin|me)/log(in|out)"|/r/\$\{|"/r/|href="/admin"|"/admin"\)|/firehose|operator=1'''),
    # The page path itself; /home/<user> file paths and /services/<id> API paths are not pages.
    "pages": re.compile(r"""["'`(](/home|/gallery|/services)/?(["'`?#]|$)"""),
}
# Historical records and tests that assert the old paths are gone.
ALLOWED = (
    "docs/design/craft-system/",  # measured captures of r18 screens; regenerated, never hand-edited
    "docs/design/vivid-fish-handoff/", "docs/design/comps/", "docs/design/references/",
    "docs/design/benchmarks/", "docs/research/",
    "docs/design/UI_CUTOVER.md", "docs/design/WEB_UI_STEERING.md", "docs/design/web-api-contract.md",
    "docs/design/D1-CONTACT-SHEET.md", "docs/design/AZIZAM_COMPONENT_LIBRARY_SPEC.md",
    "docs/design/FULL_DESIGN_PASS_PLAN.md", "docs/design/IMPLEMENTATION_PLAN.md",
    "docs/design/ENGINE_REGISTRY_DESIGN.md", "docs/GATEWAY_STREAM_LIVENESS_AUDIT_2026-07-13.md",
    "docs/ops/daisy-reliability-2026-07-14.md", "docs/ops/PROMPT_MEMORY_REVIEW_FIXES.md",
    "docs/verification/", "docs/SERVICE_HARDCODING_AUDIT.md",
    "tests/fixtures/route_method_baseline.json", "tests/fixtures/return_paths.json",
    "tests/test_route_method_baseline.py", "tests/test_no_old_ui_paths.py", "tests/test_ui_routes.py",
    "tests/test_ui_guard_and_revocation.py", "tests/test_browser_signin_cutover.py", "tests/test_accounts.py",
    "tests/test_discovery_metadata.py", "tests/test_firehose_render.py", "tests/test_headless_access.py",
    "tests/test_proxy_admin_integration.py", "tests/test_setup_render.py",  # assert the old path is gone
    "scripts/security/exposure_probe.py", "scripts/post-deploy-smoke",  # probe that /ui/home/ is a 404
    "docs/CORE_CONTRACTS.md", "docs/FIRST_RUN_SETUP.md", "docs/guides/OPERATOR.md",  # describe the move
    "scripts/run-hermetic-pytest.sh", "tests/test_flux2_seamless_tile_composition.py",
    "tests/test_sdxl_seamless_tile_manifest.py",  # /home is a file system path there
    "gateway/category_api.py", "web/src/app/categories/", "web/e2e/categories-editor.spec.ts",
    "web/e2e/engines-real.spec.ts",  # /admin/api/categories/services and /admin/api/services/<id>
    "tests/test_reddit_corpus.py",  # a Reddit /r/ permalink
    "scripts/research/reddit_corpus.py", "scripts/local/run_qwen27b_mtp.sh",  # Reddit /r/ and /home/ file paths
    "web/e2e/root-paths.spec.ts", "web/tests/components/page-registry.spec.tsx",
    "web/tests/lib/signin-return.spec.ts",
)


def _files():
    listed = subprocess.run(["git", "ls-files", "--", *SCOPE], cwd=ROOT, capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout:
        names = listed.stdout.splitlines()
    else:  # the hermetic checkout is an archive without .git
        names = [str(path.relative_to(ROOT)) for entry in SCOPE for path in
                 ([ROOT / entry] if (ROOT / entry).is_file() else (ROOT / entry).rglob("*")) if path.is_file()]
    return sorted(name for name in names if "node_modules/" not in name and not name.startswith(ALLOWED))


def test_no_removed_browser_path_reappears():
    hits = []
    for name in _files():
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    hits.append(f"{name}:{number}: [{kind}] {line.strip()[:120]}")
    assert hits == []
