"""The product docs under /showcase/docs/: build freshness, serving, links."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import showcase  # noqa: E402

STATIC = ROOT / "showcase"
DOCS = STATIC / "docs"
SOURCES = STATIC / "docs-src"
BASE = "/showcase/docs/"


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.ids: set[str] = set()
        self.h1: list[str] = []
        self._in_h1 = False

    def handle_starttag(self, tag, attrs):
        values = {key: value for key, value in attrs if value}
        if "id" in values:
            self.ids.add(values["id"])
        self.hrefs.extend(values[key] for key in ("href", "src") if key in values)
        self._in_h1 = tag == "h1"

    def handle_endtag(self, tag):
        if tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._in_h1 and data.strip():
            self.h1.append(data.strip())


def _parse(html: str) -> _Links:
    parser = _Links()
    parser.feed(html)
    return parser


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(
        showcase.build_showcase_router(static_dir=STATIC, examples_dir=Path(tempfile.mkdtemp()))
    )
    return TestClient(app)


def _page_urls() -> list[str]:
    index = json.loads((DOCS / "search-index.json").read_text(encoding="utf-8"))
    return [entry["url"] for entry in index]


class DocsBuildTests(unittest.TestCase):
    def test_committed_output_matches_sources(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build-docs"), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_every_source_page_is_in_the_index(self) -> None:
        sources = sorted(SOURCES.rglob("*.md"))
        self.assertGreater(len(sources), 10)
        self.assertEqual(len(_page_urls()), len(sources))

    def test_sources_and_output_never_link_to_github(self) -> None:
        for path in [*SOURCES.rglob("*.md"), *DOCS.rglob("*.html"), DOCS / "search-index.json"]:
            self.assertNotIn("github.com", path.read_text(encoding="utf-8").lower(), path)


class DocsServingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = _client()
        cls.pages: dict[str, tuple[httpx.Response, _Links]] = {}
        for url in _page_urls():
            response = cls.client.get(url)
            cls.pages[url] = (response, _parse(response.text))

    def test_every_page_renders(self) -> None:
        self.assertGreater(len(self.pages), 10)
        for url, (response, parsed) in self.pages.items():
            with self.subTest(url=url):
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/html", response.headers["content-type"])
                self.assertIn("script-src 'self'", response.headers["content-security-policy"])
                self.assertEqual(len(parsed.h1), 1, parsed.h1)
                self.assertIn('<html lang="en">', response.text)
                self.assertIn('aria-current="page"', response.text)

    def test_every_internal_link_resolves(self) -> None:
        checked = 0
        for url, (_response, parsed) in self.pages.items():
            for href in parsed.hrefs:
                target, fragment = urldefrag(href)
                if href.startswith("#"):
                    target = url
                elif not target.startswith("/"):
                    self.assertTrue(href.startswith(("https://", "http://", "mailto:")), (url, href))
                    continue
                if target == "/openapi.json" or target == "/showcase":
                    continue
                with self.subTest(page=url, href=href):
                    if target.startswith(BASE):
                        self.assertIn(target, self.pages, "link to a page that is not built")
                        if fragment:
                            self.assertIn(fragment, self.pages[target][1].ids, "missing anchor")
                    else:
                        self.assertEqual(self.client.get(target).status_code, 200)
                checked += 1
        self.assertGreater(checked, 100)

    def test_api_pages_link_to_the_openapi_document(self) -> None:
        for url, (response, _parsed) in self.pages.items():
            if url.startswith(BASE + "api/"):
                with self.subTest(url=url):
                    self.assertIn('href="/openapi.json"', response.text)

    def test_previous_and_next_links_follow_the_nav_order(self) -> None:
        urls = list(self.pages)
        for i, url in enumerate(urls):
            text = self.pages[url][0].text
            if i > 0:
                self.assertIn(f'href="{urls[i - 1]}" rel="prev"', text)
            if i + 1 < len(urls):
                self.assertIn(f'href="{urls[i + 1]}" rel="next"', text)

    def test_code_blocks_have_copy_buttons(self) -> None:
        response = self.pages[BASE + "api/openai/"][0]
        blocks = response.text.count('class="codeblock"')
        self.assertGreater(blocks, 1)
        self.assertEqual(blocks, response.text.count('class="copy js-only"'))


class DocsRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    def test_root_and_slashless_paths_redirect_to_the_slash_form(self) -> None:
        r = self.client.get("/showcase/docs", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (308, "/showcase/docs/"))
        r = self.client.get("/showcase/docs/api", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (308, "/showcase/docs/api/"))

    def test_unknown_pages_get_the_docs_404_page(self) -> None:
        for path in ("/showcase/docs/nope/", "/showcase/docs/api/nope", "/showcase/docs/404.html"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 404)
                self.assertIn("Page not found", r.text)

    def test_paths_outside_the_docs_folder_are_refused(self) -> None:
        for path in (
            "/showcase/docs/..%2findex.html",
            "/showcase/docs/%2e%2e/%2e%2e/proxy.py",
            "/showcase/docs/api/../../docs.css",
            "/showcase/docs/API/",
            "/showcase/docs/api/index.html",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_search_index_and_assets_are_served(self) -> None:
        r = self.client.get("/showcase/docs/search-index.json")
        self.assertEqual(r.status_code, 200)
        entries = r.json()
        self.assertTrue(all({"url", "title", "section", "headings", "text"} <= set(e) for e in entries))
        for name in ("docs.css", "docs.js", "showcase.css"):
            self.assertEqual(self.client.get(f"/showcase/static/{name}").status_code, 200, name)

    def test_docs_paths_are_public(self) -> None:
        self.assertTrue(showcase.is_public_showcase_path("/showcase/docs/"))
        self.assertTrue(showcase.is_public_showcase_path("/showcase/docs/api/openai/"))


class ProductPageLinkTests(unittest.TestCase):
    def test_product_page_links_to_the_docs_not_github(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        hrefs = set(re.findall(r'href="([^"]+)"', html))
        self.assertIn("/showcase/docs/", hrefs)
        self.assertIn("/showcase/docs/start/install/", hrefs)
        self.assertFalse([h for h in hrefs if "github.com" in h and "/docs/" in h], hrefs)


if __name__ == "__main__":
    unittest.main()
