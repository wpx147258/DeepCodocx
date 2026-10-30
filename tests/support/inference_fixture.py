"""Local HTTP inference fixture. It validates protocols without real model calls."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@contextmanager
def inference_server():
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append({"path": self.path, "body": body})
            if self.path.endswith("/embeddings"):
                if body.get("model") != "Qwen3-VL-Embedding-8B":
                    self.send_error(400)
                    return
                result = {
                    "object": "list",
                    "model": body["model"],
                    "data": [
                        {"index": i, "embedding": [1.0] + [0.0] * (body["dimensions"] - 1)}
                        for i, _ in enumerate(body["input"])
                    ],
                }
            elif self.path.endswith("/chat/completions"):
                result = {
                    "model": body["model"],
                    "choices": [
                        {
                            "message": {
                                "content": "A scientific diagram of memory supporting action."
                            }
                        }
                    ],
                }
            else:
                self.send_error(404)
                return
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
