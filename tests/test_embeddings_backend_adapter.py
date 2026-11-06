from __future__ import annotations

import json
import unittest

from gateway.backends.embeddings import build_embeddings_loopback_request


class EmbeddingsBackendAdapterTests(unittest.TestCase):
    def test_build_loopback_request_preserves_existing_wire_contract(self) -> None:
        body = {"model": "default", "input": ["hello", "world"]}

        request = build_embeddings_loopback_request(
            proxy_port=5000,
            body=body,
            internal_forward_header="x-internal-forward",
            internal_forward_secret="secret",
            source_headers={
                "Authorization": "Bearer caller",
                "X-API-Key": "legacy-key",
                "X-Other": "not-forwarded",
            },
            read_timeout=123.0,
        )

        self.assertEqual(request.url, "http://127.0.0.1:5000/v1/embeddings")
        self.assertEqual(
            request.headers,
            {
                "content-type": "application/json",
                "x-internal-forward": "secret",
                "authorization": "Bearer caller",
                "x-api-key": "legacy-key",
            },
        )
        self.assertEqual(json.loads(request.body.decode("utf-8")), body)
        self.assertEqual(request.timeout.connect, 10.0)
        self.assertEqual(request.timeout.read, 123.0)
        self.assertEqual(request.timeout.write, 60.0)

    def test_build_loopback_request_omits_absent_auth_headers(self) -> None:
        request = build_embeddings_loopback_request(
            proxy_port=5001,
            body={"model": "default", "input": "hello"},
            internal_forward_header="x-internal-forward",
            internal_forward_secret="secret",
            source_headers={},
            read_timeout=30.0,
        )

        self.assertNotIn("authorization", request.headers)
        self.assertNotIn("x-api-key", request.headers)


if __name__ == "__main__":
    unittest.main()
