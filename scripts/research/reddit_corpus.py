#!/usr/bin/env python3
"""Collect and normalize Reddit-like research corpora.

This script captures the repeatable parts of the Azizam Reddit research:

- run a query plan across target subreddits;
- prefer Reddit's public JSON endpoints, with Arctic Shift as a fallback;
- save raw artifacts under ``raw/<worker>/``;
- normalize every downloaded thread into Markdown;
- report corpus completeness and duplicate coverage.

The networked collector is intentionally conservative and slow by default.
The normalizer and summarizer are fully offline and deterministic.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PLAN = Path(__file__).with_name("reddit_corpus_queries.json")
DEFAULT_OUT = Path("/tmp/azizam-reddit-corpus")
USER_AGENT = "Mozilla/5.0 azizam-research"
ARCTIC_BASE = "https://arctic-shift.photon-reddit.com"
REDDIT_BASE = "https://www.reddit.com"


@dataclasses.dataclass
class FetchResult:
    status: int
    body: bytes
    url: str


@dataclasses.dataclass
class Comment:
    id: str
    author: str
    body: str
    score: int | None
    created_utc: float | int | None = None
    children: list["Comment"] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ThreadDoc:
    title: str
    subreddit: str
    thread_id: str
    author: str
    score: int | None
    created_utc: float | int | None
    num_comments: int | None
    url: str
    op_text: str
    comments: list[Comment]
    source_path: Path
    complete: bool
    completeness_reason: str


def _json_loads(data: bytes | str) -> Any:
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data)


def _request_json(url: str, timeout: float = 30.0) -> FetchResult:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return FetchResult(
                status=response.status,
                body=response.read(),
                url=response.geturl(),
            )
    except urllib.error.HTTPError as exc:
        return FetchResult(status=exc.code, body=exc.read(), url=url)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _slug(value: str, max_chars: int = 72) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return (slug or "untitled")[:max_chars].strip("-")


def _safe_name(value: str) -> str:
    return _slug(value, max_chars=96)


def _iso_utc(value: float | int | None) -> str:
    if value in (None, ""):
        return "unknown"
    try:
        return dt.datetime.fromtimestamp(float(value), tz=dt.UTC).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return str(value)


def _reddit_search_url(subreddit: str, query: str, limit: int) -> str:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "restrict_sr": "1",
            "sort": "relevance",
            "t": "year",
            "limit": str(limit),
            "raw_json": "1",
        }
    )
    return f"{REDDIT_BASE}/r/{subreddit}/search.json?{params}"


def _reddit_thread_url(subreddit: str, thread_id: str, limit: int) -> str:
    params = urllib.parse.urlencode({"limit": str(limit), "raw_json": "1"})
    return f"{REDDIT_BASE}/r/{subreddit}/comments/{thread_id}/.json?{params}"


def _arctic_post_search_url(subreddit: str, query: str, limit: int) -> str:
    # Arctic Shift's broad `query` search can be noisy for active subreddits.
    # The title search is closer to Reddit's launch-thread discovery use case.
    params = urllib.parse.urlencode(
        {
            "subreddit": subreddit,
            "title": query,
            "limit": str(limit),
            "sort": "desc",
        }
    )
    return f"{ARCTIC_BASE}/api/posts/search?{params}"


def _arctic_comment_tree_url(thread_id: str, limit: int) -> str:
    params = urllib.parse.urlencode(
        {"link_id": f"t3_{thread_id}", "limit": str(limit)}
    )
    return f"{ARCTIC_BASE}/api/comments/tree?{params}"


def _load_plan(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())


def _candidate_from_reddit_child(child: dict[str, Any]) -> dict[str, Any] | None:
    data = child.get("data") or {}
    if not data.get("id") or not data.get("subreddit"):
        return None
    return {
        "id": data.get("id"),
        "subreddit": data.get("subreddit"),
        "title": data.get("title") or "",
        "score": data.get("score") or 0,
        "num_comments": data.get("num_comments") or 0,
        "post": data,
    }


def _candidate_from_arctic_post(post: dict[str, Any]) -> dict[str, Any] | None:
    if not post.get("id") or not post.get("subreddit"):
        return None
    return {
        "id": post.get("id"),
        "subreddit": post.get("subreddit"),
        "title": post.get("title") or "",
        "score": post.get("score") or 0,
        "num_comments": post.get("num_comments") or 0,
        "post": post,
    }


def _load_candidates_from_search(
    raw: bytes, source: str
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        payload = _json_loads(raw)
    except json.JSONDecodeError as exc:
        return [], f"invalid JSON: {exc}"

    if source == "reddit":
        children = payload.get("data", {}).get("children", [])
        return [
            c
            for c in (_candidate_from_reddit_child(child) for child in children)
            if c
        ], None

    posts = payload.get("data", []) if isinstance(payload, dict) else []
    return [
        c for c in (_candidate_from_arctic_post(post) for post in posts) if c
    ], None


def collect(args: argparse.Namespace) -> int:
    out = Path(args.out)
    plan = _load_plan(Path(args.plan))
    selected_workers = set(args.worker or [])
    seen: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []

    for worker in plan:
        worker_name = worker["name"]
        if selected_workers and worker_name not in selected_workers:
            continue

        raw_dir = out / "raw" / worker_name
        raw_dir.mkdir(parents=True, exist_ok=True)
        print(f"== {worker_name} ==", flush=True)

        for subreddit in worker["subreddits"]:
            for query in worker["queries"]:
                search_slug = _safe_name(f"{subreddit}-{query}")
                candidates: list[dict[str, Any]] = []
                source_used = args.source

                if args.source in ("auto", "reddit"):
                    url = _reddit_search_url(subreddit, query, args.query_limit)
                    result = _request_json(url)
                    search_path = raw_dir / f"search-reddit-{search_slug}.json"
                    _write_bytes(search_path, result.body)
                    if result.status == 200:
                        candidates, error = _load_candidates_from_search(
                            result.body, "reddit"
                        )
                        if error:
                            skipped.append(f"{worker_name}: {url}: {error}")
                    elif args.source == "reddit":
                        skipped.append(
                            f"{worker_name}: Reddit search {result.status}: {url}"
                        )

                    if candidates:
                        source_used = "reddit"
                    elif args.source == "auto":
                        source_used = "arctic"

                    time.sleep(args.sleep)

                if source_used == "arctic":
                    url = _arctic_post_search_url(subreddit, query, args.query_limit)
                    result = _request_json(url)
                    search_path = raw_dir / f"search-arctic-{search_slug}.json"
                    _write_bytes(search_path, result.body)
                    if result.status == 200:
                        candidates, error = _load_candidates_from_search(
                            result.body, "arctic"
                        )
                        if error:
                            skipped.append(f"{worker_name}: {url}: {error}")
                    else:
                        skipped.append(
                            f"{worker_name}: Arctic search {result.status}: {url}"
                        )
                    time.sleep(args.sleep)

                for candidate in candidates:
                    if int(candidate.get("num_comments") or 0) < args.min_comments:
                        continue
                    key = f"{candidate['subreddit'].lower()}-{candidate['id']}"
                    current = seen.get(key)
                    if not current or _rank_candidate(candidate) > _rank_candidate(
                        current
                    ):
                        candidate["worker"] = worker_name
                        seen[key] = candidate

    candidates = sorted(seen.values(), key=_rank_candidate, reverse=True)
    by_worker: dict[str, int] = {}
    downloaded = 0
    for candidate in candidates:
        worker_name = candidate["worker"]
        if by_worker.get(worker_name, 0) >= args.threads_per_worker:
            continue
        by_worker[worker_name] = by_worker.get(worker_name, 0) + 1
        raw_dir = out / "raw" / worker_name
        thread_path = raw_dir / (
            f"thread-{candidate['subreddit']}-{candidate['id']}.json"
        )
        if thread_path.exists() and not args.refresh:
            downloaded += 1
            continue

        if args.source == "reddit":
            url = _reddit_thread_url(
                candidate["subreddit"], candidate["id"], args.comment_limit
            )
            result = _request_json(url, timeout=60)
            _write_bytes(thread_path, result.body)
            if result.status != 200:
                skipped.append(f"{worker_name}: Reddit thread {result.status}: {url}")
        else:
            url = _arctic_comment_tree_url(candidate["id"], args.comment_limit)
            result = _request_json(url, timeout=60)
            if result.status == 200:
                try:
                    comments = _json_loads(result.body).get("data", [])
                except json.JSONDecodeError:
                    comments = []
                _write_json(
                    thread_path,
                    {
                        "source": "arctic-shift",
                        "post": candidate["post"],
                        "comments_tree": comments,
                    },
                )
            else:
                skipped.append(f"{worker_name}: Arctic thread {result.status}: {url}")

        downloaded += 1
        print(
            f"downloaded {candidate['subreddit']}/{candidate['id']} "
            f"({candidate['num_comments']} comments)",
            flush=True,
        )
        time.sleep(args.sleep)

    report_dir = out / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / "collection-run.md"
    lines = [
        "# Reddit Corpus Collection Run",
        "",
        f"- Candidates selected: {len(candidates)}",
        f"- Threads downloaded or already present: {downloaded}",
        f"- Output: `{out}`",
        "",
        "## Per Worker",
        "",
    ]
    for worker_name, count in sorted(by_worker.items()):
        lines.append(f"- `{worker_name}`: {count}")
    if skipped:
        lines.extend(["", "## Skipped / Errors", ""])
        lines.extend(f"- {item}" for item in skipped)
    report.write_text("\n".join(lines) + "\n")
    print(f"wrote {report}", flush=True)
    return 0


def _rank_candidate(candidate: dict[str, Any]) -> tuple[int, int, str]:
    return (
        int(candidate.get("num_comments") or 0),
        int(candidate.get("score") or 0),
        str(candidate.get("title") or ""),
    )


def normalize(args: argparse.Namespace) -> int:
    out = Path(args.out)
    thread_dir = out / "threads"
    report_dir = out / "reports"
    thread_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    raw_files = sorted((out / "raw").glob("*/thread-*.json"))
    seen: set[str] = set()
    created: list[Path] = []
    existed: list[Path] = []
    parse_failures: list[tuple[Path, str]] = []
    duplicate_raw: list[Path] = []

    for raw_file in raw_files:
        try:
            doc = load_thread_doc(raw_file)
        except Exception as exc:  # noqa: BLE001
            parse_failures.append((raw_file, str(exc)))
            continue
        key = f"{doc.subreddit.lower()}-{doc.thread_id.lower()}"
        if key in seen:
            duplicate_raw.append(raw_file)
            if not args.keep_duplicate_markdown:
                continue
        seen.add(key)

        filename = (
            f"{doc.subreddit.lower()}-{doc.thread_id.lower()}-"
            f"{_slug(doc.title)}.md"
        )
        target = thread_dir / filename
        if target.exists() and not args.refresh:
            existed.append(target)
            continue
        target.write_text(render_markdown(doc))
        created.append(target)

    report = report_dir / "normalization-run.md"
    report.write_text(
        _render_normalization_report(
            raw_files=raw_files,
            unique_count=len(seen),
            created=created,
            existed=existed,
            duplicate_raw=duplicate_raw,
            parse_failures=parse_failures,
        )
    )
    print(f"wrote {report}", flush=True)
    print(f"created={len(created)} existed={len(existed)} failures={len(parse_failures)}")
    return 0 if not parse_failures or args.allow_failures else 1


def load_thread_doc(path: Path) -> ThreadDoc:
    raw_text = path.read_text()
    payload = json.loads(raw_text)
    if isinstance(payload, list):
        return _load_reddit_thread(path, payload)
    if isinstance(payload, dict) and "post" in payload:
        return _load_arctic_thread(path, payload)
    raise ValueError("unsupported thread JSON shape")


def _load_reddit_thread(path: Path, payload: list[Any]) -> ThreadDoc:
    try:
        post = payload[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"invalid Reddit thread payload: {exc}") from exc
    comments_root = payload[1].get("data", {}).get("children", []) if len(payload) > 1 else []
    comments, has_more = _parse_reddit_children(comments_root)
    num_comments = post.get("num_comments")
    visible = _count_comments(comments)
    partial_reason = ""
    if has_more:
        partial_reason = "has `more` placeholders in comment tree"
    elif isinstance(num_comments, int) and visible < num_comments:
        partial_reason = f"only {visible} visible comments parsed, {num_comments} reported"
    return ThreadDoc(
        title=post.get("title") or "Untitled",
        subreddit=post.get("subreddit") or _subreddit_from_filename(path),
        thread_id=post.get("id") or _thread_id_from_filename(path),
        author=post.get("author") or "[unknown]",
        score=post.get("score"),
        created_utc=post.get("created_utc"),
        num_comments=num_comments,
        url=_permalink(post),
        op_text=post.get("selftext") or post.get("url") or "",
        comments=comments,
        source_path=path,
        complete=not partial_reason,
        completeness_reason=partial_reason or "complete",
    )


def _load_arctic_thread(path: Path, payload: dict[str, Any]) -> ThreadDoc:
    post = payload.get("post") or {}
    if "comments_tree" in payload:
        comments, has_more = _parse_arctic_tree(payload.get("comments_tree") or [])
    else:
        comments, has_more = _parse_arctic_flat_comments(
            payload.get("comments") or [], post.get("id") or _thread_id_from_filename(path)
        )
    num_comments = post.get("num_comments")
    visible = _count_comments(comments)
    partial_reason = ""
    if has_more:
        partial_reason = "has collapsed `more` placeholders in Arctic Shift tree"
    elif isinstance(num_comments, int) and visible < num_comments:
        partial_reason = f"only {visible} visible comments parsed, {num_comments} reported"
    return ThreadDoc(
        title=post.get("title") or "Untitled",
        subreddit=post.get("subreddit") or _subreddit_from_filename(path),
        thread_id=post.get("id") or _thread_id_from_filename(path),
        author=post.get("author") or "[unknown]",
        score=post.get("score"),
        created_utc=post.get("created_utc"),
        num_comments=num_comments,
        url=_permalink(post),
        op_text=post.get("selftext") or post.get("url") or "",
        comments=comments,
        source_path=path,
        complete=not partial_reason,
        completeness_reason=partial_reason or "complete",
    )


def _parse_reddit_children(children: Iterable[dict[str, Any]]) -> tuple[list[Comment], bool]:
    comments: list[Comment] = []
    has_more = False
    for child in children:
        kind = child.get("kind")
        if kind == "more":
            has_more = True
            continue
        if kind != "t1":
            continue
        data = child.get("data") or {}
        nested: list[Comment] = []
        replies = data.get("replies")
        if isinstance(replies, dict):
            nested, child_has_more = _parse_reddit_children(
                replies.get("data", {}).get("children", [])
            )
            has_more = has_more or child_has_more
        comments.append(_comment_from_data(data, nested))
    return comments, has_more


def _parse_arctic_tree(items: Iterable[dict[str, Any]]) -> tuple[list[Comment], bool]:
    comments: list[Comment] = []
    has_more = False
    for item in items:
        kind = item.get("kind")
        if kind == "more":
            has_more = True
            continue
        data = item.get("data") if "data" in item else item
        if not isinstance(data, dict):
            continue
        child_items = data.get("children") or data.get("replies") or []
        nested, child_has_more = _parse_arctic_tree(child_items)
        has_more = has_more or child_has_more
        comments.append(_comment_from_data(data, nested))
    return comments, has_more


def _parse_arctic_flat_comments(
    comments: Iterable[dict[str, Any]], thread_id: str
) -> tuple[list[Comment], bool]:
    by_id: dict[str, Comment] = {}
    parents: dict[str, str] = {}
    top_level_parent_ids = {thread_id, f"t3_{thread_id}"}
    for raw in comments:
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        comment = _comment_from_data(data, [])
        if not comment.id:
            continue
        by_id[comment.id] = comment
        parents[comment.id] = str(data.get("parent_id") or "")

    roots: list[Comment] = []
    for comment_id, comment in by_id.items():
        parent_id = parents.get(comment_id, "")
        normalized_parent = parent_id.removeprefix("t1_")
        if parent_id in top_level_parent_ids or normalized_parent not in by_id:
            roots.append(comment)
        else:
            by_id[normalized_parent].children.append(comment)
    roots.sort(key=lambda c: (c.score or 0), reverse=True)
    return roots, False


def _comment_from_data(data: dict[str, Any], children: list[Comment]) -> Comment:
    return Comment(
        id=str(data.get("id") or ""),
        author=str(data.get("author") or "[unknown]"),
        body=str(data.get("body") or data.get("body_md") or ""),
        score=data.get("score"),
        created_utc=data.get("created_utc"),
        children=children,
    )


def _permalink(post: dict[str, Any]) -> str:
    permalink = post.get("permalink")
    if permalink:
        return urllib.parse.urljoin(REDDIT_BASE, permalink)
    subreddit = post.get("subreddit") or "unknown"
    thread_id = post.get("id") or "unknown"
    return f"{REDDIT_BASE}/r/{subreddit}/comments/{thread_id}/"


def _subreddit_from_filename(path: Path) -> str:
    match = re.match(r"thread-([^-]+)-", path.name)
    return match.group(1) if match else "unknown"


def _thread_id_from_filename(path: Path) -> str:
    match = re.match(r"thread-[^-]+-([^.]+)\.json", path.name)
    return match.group(1) if match else "unknown"


def _count_comments(comments: Iterable[Comment]) -> int:
    return sum(1 + _count_comments(comment.children) for comment in comments)


def render_markdown(doc: ThreadDoc) -> str:
    lines = [
        f"# {doc.title}",
        "",
        f"- URL: {doc.url}",
        f"- Subreddit: r/{doc.subreddit}",
        f"- Thread id: {doc.thread_id}",
        f"- Author: {doc.author}",
        f"- Score: {doc.score if doc.score is not None else 'unknown'}",
        f"- Created UTC: {_iso_utc(doc.created_utc)}",
        f"- Num comments: {doc.num_comments if doc.num_comments is not None else 'unknown'}",
        f"- Completeness: {doc.completeness_reason}",
        f"- Source raw JSON: {doc.source_path}",
        "- Relevance: selected for AI-gateway Reddit simulation grounding.",
        "",
        "## OP",
        "",
        doc.op_text.strip() or "[no selftext]",
        "",
        "## Comments",
        "",
    ]
    if doc.comments:
        for comment in doc.comments:
            _render_comment(lines, comment, depth=0)
    else:
        lines.append("[no comments parsed]")
    return "\n".join(lines).rstrip() + "\n"


def _render_comment(lines: list[str], comment: Comment, depth: int) -> None:
    heading = "#" * min(6, 3 + depth)
    lines.append(
        f"{heading} depth={depth} score="
        f"{comment.score if comment.score is not None else 'unknown'} "
        f"author={comment.author}"
    )
    lines.append(comment.body.strip() or "[empty]")
    lines.append("")
    for child in comment.children:
        _render_comment(lines, child, depth + 1)


def summarize(args: argparse.Namespace) -> int:
    out = Path(args.out)
    thread_files = sorted((out / "threads").glob("*.md"))
    raw_files = sorted((out / "raw").glob("*/thread-*.json"))
    unique_threads = {
        " ".join(match.groups()).lower()
        for path in thread_files
        if (match := re.match(r"([^-]+)-([^-]+)-", path.name))
    }
    completeness: dict[str, int] = {}
    for path in thread_files:
        text = path.read_text(errors="replace")
        match = re.search(r"^- Completeness: (.+)$", text, flags=re.MULTILINE)
        key = match.group(1) if match else "unknown"
        if key.startswith("complete"):
            key = "complete"
        elif "metadata" in key or "unavailable" in key:
            key = "metadata/stub"
        elif (
            key.startswith("partial")
            or key.startswith("only ")
            or "placeholder" in key
            or "collapsed" in key
        ):
            key = "partial comments"
        completeness[key] = completeness.get(key, 0) + 1

    print(f"Corpus: {out}")
    print(f"Raw thread files: {len(raw_files)}")
    print(f"Markdown files: {len(thread_files)}")
    print(f"Unique markdown thread IDs: {len(unique_threads)}")
    print("Completeness:")
    for key, count in sorted(completeness.items()):
        print(f"  {key}: {count}")
    return 0


def _render_normalization_report(
    *,
    raw_files: list[Path],
    unique_count: int,
    created: list[Path],
    existed: list[Path],
    duplicate_raw: list[Path],
    parse_failures: list[tuple[Path, str]],
) -> str:
    lines = [
        "# Reddit Corpus Normalization Run",
        "",
        f"- Raw thread JSON files found: {len(raw_files)}",
        f"- Unique threads normalized: {unique_count}",
        f"- Markdown files created: {len(created)}",
        f"- Markdown files already present: {len(existed)}",
        f"- Duplicate raw files skipped: {len(duplicate_raw)}",
        f"- Parse failures: {len(parse_failures)}",
        "",
        "## Created",
        "",
    ]
    if created:
        lines.extend(f"- `{path}`" for path in created)
    else:
        lines.append("- none")
    lines.extend(["", "## Parse Failures", ""])
    if parse_failures:
        lines.extend(f"- `{path}`: {error}" for path, error in parse_failures)
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Corpus directory")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_parser = sub.add_parser("collect", help="Search and download threads")
    collect_parser.add_argument("--plan", default=str(DEFAULT_PLAN))
    collect_parser.add_argument(
        "--source", choices=("auto", "reddit", "arctic"), default="auto"
    )
    collect_parser.add_argument("--worker", action="append", help="Worker name")
    collect_parser.add_argument("--query-limit", type=int, default=25)
    collect_parser.add_argument("--threads-per-worker", type=int, default=12)
    collect_parser.add_argument("--min-comments", type=int, default=10)
    collect_parser.add_argument("--comment-limit", type=int, default=9999)
    collect_parser.add_argument("--sleep", type=float, default=2.0)
    collect_parser.add_argument("--refresh", action="store_true")
    collect_parser.set_defaults(func=collect)

    normalize_parser = sub.add_parser("normalize", help="Normalize raw thread JSON")
    normalize_parser.add_argument("--refresh", action="store_true")
    normalize_parser.add_argument("--allow-failures", action="store_true")
    normalize_parser.add_argument("--keep-duplicate-markdown", action="store_true")
    normalize_parser.set_defaults(func=normalize)

    summary_parser = sub.add_parser("summarize", help="Print corpus counts")
    summary_parser.set_defaults(func=summarize)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
