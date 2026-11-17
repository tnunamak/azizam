"""Criterion 7: a second offering (molecule smell inference) runs through the same runner with no core change.

The offering is a test-only http-v1 description (tests/fixtures/engine_protocol/d0/smell.yml) served by an in-process
fake. It is driven by the runner the audio offering uses (``jobs.run``); the only difference is the description file.
The word "smell" must not appear in the code of ``gateway/`` (comments and docstrings are allowed to mention it).
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import json
import tokenize
from pathlib import Path
from typing import Callable

import httpx
import pytest

from gateway import media_jobs
from gateway.engine_protocol import description, jobs
from gateway.engine_protocol.adapter import AdapterRegistry
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.errors import ProtocolError
from gateway.engine_protocol.transport import HttpxTransport
from gateway.engine_protocol.types import Connection, Timeouts
from tests.support.fake_upstream import HandlerTransport
from tests.test_operation_runner import Runtime, _new_job, jobs_db  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parent.parent
SMELL = ROOT / "tests" / "fixtures" / "engine_protocol" / "d0" / "smell.yml"
CONN = Connection(address="http://smell.test:9000", timeouts=Timeouts(connect_s=10, first_byte_s=30, total_s=30))
VECTOR = [0.25, -1.5, 3.0, 0.0]
MOLECULE = json.dumps({"smiles": "CCO", "odour": ["alcohol", "sweet"]}, sort_keys=True).encode("utf-8")


def _runtime(answer: Callable[[httpx.Request], httpx.Response]) -> tuple[Runtime, MemoryAssetStore]:
    doc = description.parse_description_text(SMELL.read_text(encoding="utf-8"), source="smell.yml")
    store = MemoryAssetStore()
    transport = HttpxTransport.for_tests(HandlerTransport(answer))
    adapter = AdapterRegistry({doc.id: doc}, transport=transport, assets=store).adapter_for(doc.id)
    return Runtime(adapter, CONN, {}, (0.5, 5.0)), store


def _engine(states: list[str], posts: list) -> Callable[[httpx.Request], httpx.Response]:
    remaining = list(states)

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/infer":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"job": {"id": "smell-1"}})
        if request.method == "GET" and path == "/v1/infer/smell-1":
            state = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return httpx.Response(200, json={"state": state})
        if request.method == "GET" and path == "/v1/infer/smell-1/molecule":
            return httpx.Response(200, content=MOLECULE, headers={"content-type": "application/octet-stream"})
        return httpx.Response(404)

    return answer


def test_the_smell_offering_completes_through_the_same_runner(jobs_db):  # noqa: F811 - fixture
    posts: list = []
    runtime, store = _runtime(_engine(["queued", "working", "ready"], posts))
    job_id = _new_job(jobs_db)
    refs = asyncio.run(jobs.run(runtime, "smell.infer", {"vector": VECTOR}, job_id=job_id))

    assert posts == [{"input": {"vector": VECTOR}}], "the vector did not reach the engine's input"
    assert len(refs) == 1
    ref = refs[0]
    assert ref.content_type == "application/octet-stream"
    assert ref.size == len(MOLECULE)
    data = asyncio.run(store.read(ref, max_bytes=10_000))
    assert hashlib.sha256(data).hexdigest() == hashlib.sha256(MOLECULE).hexdigest()
    stored = media_jobs.get(job_id, audience="internal", path=jobs_db) or {}
    assert (stored.get("metadata") or {}).get("protocol_state") == "succeeded"


def test_the_smell_offering_never_resubmits_after_a_lost_reply(jobs_db):  # noqa: F811 - fixture
    posts: list = []

    def lost(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        raise httpx.ReadError("the reply to the submission was lost", request=request)

    lost_runtime, _ = _runtime(lost)
    healthy_runtime, _ = _runtime(_engine(["ready"], posts))
    job_id = _new_job(jobs_db)

    with pytest.raises(ProtocolError) as first:
        asyncio.run(jobs.run(lost_runtime, "smell.infer", {"vector": VECTOR}, job_id=job_id))
    assert first.value.code == "submission_unknown"

    # The engine is healthy now, but the job's submit may have started: the later run must not send it again.
    with pytest.raises(ProtocolError) as second:
        asyncio.run(jobs.run(healthy_runtime, "smell.infer", {"vector": VECTOR}, job_id=job_id))
    assert second.value.code == "submission_unknown"
    assert len(posts) == 1, "the submit was sent again after its reply was lost"


def _without_web_comments(source: str) -> str:
    """Blank comment spans without moving lines or interpreting quoted markers."""
    chars = list(source)
    quote = None
    comment = None
    index = 0
    while index < len(source):
        char = source[index]
        pair = source[index:index + 2]
        if comment == "line":
            if char in "\r\n":
                comment = None
            else:
                chars[index] = " "
            index += 1
        elif comment == "block":
            if pair == "*/":
                chars[index:index + 2] = [" ", " "]
                comment = None
                index += 2
            else:
                if char not in "\r\n":
                    chars[index] = " "
                index += 1
        elif quote is not None:
            if char == "\\":
                index += 2
            else:
                if char == quote:
                    quote = None
                index += 1
        elif char in ("'", '"', "\x60"):
            quote = char
            index += 1
        elif pair in ("//", "/*"):
            chars[index:index + 2] = [" ", " "]
            comment = "line" if pair == "//" else "block"
            index += 2
        else:
            index += 1
    return "".join(chars)


def _offering_hits(root: Path, *, web: bool = False) -> list[str]:
    """Scan a repository-shaped tree and report source locations only."""
    folder = root / "web" / "src" if web else root / "gateway"
    hits: set[str] = set()
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or "__pycache__" in path.relative_to(folder).parts:
            continue
        if web and (
            path.suffix not in {".ts", ".tsx"}
            or ".spec." in path.name
            or ".test." in path.name
        ):
            continue
        relative = path.relative_to(root).as_posix()
        if not web and path.suffix == ".py":
            with tokenize.open(path) as handle:
                source = handle.read()
            tree = ast.parse(source, filename=str(path))
            source_lines = source.splitlines(keepends=True)
            docstring_spans = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    first = node.body[0] if node.body else None
                    if (
                        isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)
                    ):
                        docstring_spans.append((
                            (first.lineno, first.col_offset),
                            (first.end_lineno, first.end_col_offset),
                        ))
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT:
                    continue
                if token.type == tokenize.STRING:
                    # AST columns are UTF-8 byte offsets; tokenize uses characters.
                    start = (token.start[0], len(source_lines[token.start[0] - 1][:token.start[1]].encode("utf-8")))
                    end = (token.end[0], len(source_lines[token.end[0] - 1][:token.end[1]].encode("utf-8")))
                    if any(span_start <= start and end <= span_end
                           for span_start, span_end in docstring_spans):
                        continue
                if "smell" in token.string.lower():
                    hits.add(f"{relative}:{token.start[0]}")
            continue
        raw = path.read_bytes()
        if b"\x00" in raw:
            continue
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue  # Binary assets are not source text.
        if web:
            source = _without_web_comments(source)
        for line_number, line in enumerate(source.splitlines(), start=1):
            if "smell" in line.lower():
                hits.add(f"{relative}:{line_number}")
    return sorted(hits)


def test_no_core_module_names_the_smell_offering():
    """The offering lives in its description and tests, not gateway code or data."""
    assert _offering_hits(ROOT) == []


def test_no_web_source_names_the_smell_offering():
    """The web app must not hard-code an offering outside tests or comments."""
    assert _offering_hits(ROOT, web=True) == []


@pytest.mark.parametrize(
    ("relative_path", "source", "web", "expected_lines"),
    [
        ("gateway/inline.py", '"""module docs"""; OFFERING = "smell"\n', False, [1]),
        ("gateway/joined.py", '("ordinary documentation "\n "smell documentation")\n', False, []),
        ("gateway/after.py", '("ordinary documentation "\n "smell documentation")\nOFFERING = "smell"\n', False, [3]),
        ("gateway/unicode.py", '"""é docs"""; OFFERING = "smell"\n', False, [1]),
        ("web/src/inline.ts", '/* explanation */ const offering = "smell";\n', True, [1]),
        ("web/src/quoted.ts", 'const url = "https://example.invalid"; const offering = "smell";\n', True, [1]),
        ("web/src/template.ts", 'const url = \x60https://example.invalid/*text*/\x60; const offering = "smell";\n', True, [1]),
        ("web/src/after-block.ts", '/* explanation\n smell docs */ const offering = "smell";\n', True, [2]),
        ("web/src/inline-comment.ts", 'const value = 1; // smell docs\n', True, []),
        ("web/src/string-comment.ts", 'const text = "/* smell */";\n', True, [1]),
        ("gateway/constant.py", 'QUERY = """SMELL"""\n', False, [1]),
        (
            "gateway/docs.py",
            '"""smell module"""\n'
            'class Example:\n    """smell class"""\n'
            '    def method(self):\n        """smell function"""\n        pass\n'
            'async def task():\n    """smell async function"""\n    pass\n',
            False,
            [],
        ),
        ("gateway/data.json", '{"offering": "SmElL"}\n', False, [1]),
        ("gateway/data.yml", "offering: smell\n", False, [1]),
        ("gateway/comment.py", "# smell\nvalue = 1\n", False, []),
        ("gateway/__pycache__/cached.json", '{"offering": "smell"}\n', False, []),
        ("web/src/component.tsx", 'const offering = "SmElL";\n', True, [1]),
        ("web/src/component.ts", 'const offering = "smell";\n', True, [1]),
        ("web/src/component.spec.tsx", 'const offering = "smell";\n', True, []),
        ("web/src/component.test.ts", 'const offering = "smell";\n', True, []),
        ("web/src/comments.ts", "// smell\n  /* smell\n * smell\n */\n", True, []),
    ],
    ids=[
        "same-line-docstring-assignment",
        "joined-docstring",
        "after-joined-docstring",
        "unicode-docstring-boundary",
        "after-typescript-block-comment",
        "quoted-double-slash",
        "template-comment-markers",
        "after-multiline-block-comment",
        "trailing-line-comment",
        "quoted-block-comment",
        "triple-quoted-constant",
        "real-docstrings",
        "gateway-json",
        "gateway-yaml",
        "python-comment",
        "cache",
        "typescript-component",
        "typescript-source",
        "spec-file",
        "test-file",
        "typescript-comments",
    ],
)
def test_offering_scan(tmp_path, relative_path, source, web, expected_lines):
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    assert _offering_hits(tmp_path, web=web) == [
        f"{relative_path}:{line}" for line in expected_lines
    ]


def test_offering_scan_skips_binary_files(tmp_path):
    package = tmp_path / "gateway"
    package.mkdir()
    (package / "invalid.bin").write_bytes(b"smell\xff")
    (package / "nul.bin").write_bytes(b"smell\x00")
    assert _offering_hits(tmp_path) == []
