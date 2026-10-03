"""Deterministic OpenAI-compatible engine; no model download or paid calls."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json

MODEL = "fresh-test"
ANSWER = "Fresh instance chat works."


class Engine(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log request headers or bodies.

    def reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self.reply(200, {"object": "list", "data": [
                {"id": MODEL, "object": "model", "created": 0, "owned_by": "test"}
            ]})
        else:
            self.reply(404, {"error": {"message": "Unknown route"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.reply(404, {"error": {"message": "Unknown route"}})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if body.get("model") != MODEL or not body.get("messages") or body.get("stream"):
            self.reply(400, {"error": {"message": "Unexpected test request"}})
            return
        self.reply(200, {
            "id": "chatcmpl-fresh", "object": "chat.completion", "created": 0,
            "model": MODEL, "choices": [{"index": 0, "message": {
                "role": "assistant", "content": ANSWER}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 5, "total_tokens": 6},
        })


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Engine).serve_forever()
