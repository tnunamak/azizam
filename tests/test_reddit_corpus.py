from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.research import reddit_corpus  # noqa: E402


class TestRedditCorpusNormalization(unittest.TestCase):
    def test_reddit_thread_json_preserves_nested_comments(self) -> None:
        payload = [
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "id": "abc123",
                                "subreddit": "LocalLLaMA",
                                "title": "OpenAI-compatible local gateway?",
                                "author": "poster",
                                "score": 42,
                                "created_utc": 1770000000,
                                "num_comments": 2,
                                "permalink": "/r/LocalLLaMA/comments/abc123/example/",
                                "selftext": "Does this exist?",
                            }
                        }
                    ]
                }
            },
            {
                "data": {
                    "children": [
                        {
                            "kind": "t1",
                            "data": {
                                "id": "c1",
                                "author": "skeptic",
                                "body": "Why not just use LiteLLM?",
                                "score": 5,
                                "replies": {
                                    "data": {
                                        "children": [
                                            {
                                                "kind": "t1",
                                                "data": {
                                                    "id": "c2",
                                                    "author": "op",
                                                    "body": "Local media workflows.",
                                                    "score": 3,
                                                    "replies": "",
                                                },
                                            }
                                        ]
                                    }
                                },
                            },
                        }
                    ]
                }
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "thread-LocalLLaMA-abc123.json"
            path.write_text(json.dumps(payload))

            doc = reddit_corpus.load_thread_doc(path)
            markdown = reddit_corpus.render_markdown(doc)

        self.assertTrue(doc.complete)
        self.assertEqual(reddit_corpus._count_comments(doc.comments), 2)
        self.assertIn("### depth=0 score=5 author=skeptic", markdown)
        self.assertIn("#### depth=1 score=3 author=op", markdown)
        self.assertIn("Why not just use LiteLLM?", markdown)

    def test_arctic_flat_comments_are_reconstructed_into_tree(self) -> None:
        payload = {
            "post": {
                "id": "def456",
                "subreddit": "selfhosted",
                "title": "Ollama has no auth",
                "author": "builder",
                "score": 9,
                "created_utc": 1770000001,
                "num_comments": 2,
                "selftext": "I built a fix.",
            },
            "comments": [
                {
                    "id": "root",
                    "parent_id": "t3_def456",
                    "author": "admin",
                    "body": "Can it do OIDC groups?",
                    "score": 4,
                },
                {
                    "id": "child",
                    "parent_id": "t1_root",
                    "author": "builder",
                    "body": "Yes, via role mappings.",
                    "score": 2,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "thread-selfhosted-def456.json"
            path.write_text(json.dumps(payload))

            doc = reddit_corpus.load_thread_doc(path)
            markdown = reddit_corpus.render_markdown(doc)

        self.assertTrue(doc.complete)
        self.assertEqual(len(doc.comments), 1)
        self.assertEqual(doc.comments[0].children[0].author, "builder")
        self.assertIn("Can it do OIDC groups?", markdown)
        self.assertIn("Yes, via role mappings.", markdown)

    def test_reddit_more_placeholder_marks_thread_partial(self) -> None:
        payload = [
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "id": "ghi789",
                                "subreddit": "comfyui",
                                "title": "Workflow API",
                                "num_comments": 10,
                            }
                        }
                    ]
                }
            },
            {"data": {"children": [{"kind": "more", "data": {"children": []}}]}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "thread-comfyui-ghi789.json"
            path.write_text(json.dumps(payload))

            doc = reddit_corpus.load_thread_doc(path)

        self.assertFalse(doc.complete)
        self.assertIn("more", doc.completeness_reason)


if __name__ == "__main__":
    unittest.main()
