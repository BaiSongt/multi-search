import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

from multisearch_client import MultiSearchClient, MultiSearchError

JOB_ID = "12345678-0000-0000-0000-abcdef123456"
TRACE = "a" * 32
TOKEN = "test-sdk-key"


class MultiSearchClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []
        cls.foreign_received = []

        class ForeignHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                cls.foreign_received.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            do_POST = do_GET

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.handle_request()

            do_POST = do_GET

            def handle_request(self):
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size)) if size else None
                cls.received.append(
                    (self.command, self.path, self.headers.get("Authorization"), body)
                )
                query = body.get("query") if body else None
                if query == "redirect":
                    self.send_response(307)
                    self.send_header("Location", cls.foreign + "/collect")
                    self.end_headers()
                    return
                if query == "timeout":
                    time.sleep(0.15)
                if query == "oversize":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"x" * 1024)
                    return
                if query == "throttle":
                    status, data = (
                        429,
                        {
                            "request_id": "from-body",
                            "code": "RATE_LIMITED",
                            "message": f"Unsafe echoed content {TOKEN}",
                            "retryable": True,
                        },
                    )
                elif self.path.endswith("/capabilities"):
                    status, data = 200, {"modes": {"quick": "available"}}
                elif (
                    self.path.endswith(("/fetch", "/research"))
                    or (body or {}).get("mode") == "deep"
                ):
                    status, data = (
                        202,
                        {
                            "job_id": JOB_ID,
                            "status": "queued",
                            "status_url": cls.foreign,
                        },
                    )
                elif "/jobs/" in self.path:
                    status, data = (
                        200,
                        {
                            "job_id": JOB_ID,
                            "status": "cancelled"
                            if self.path.endswith("/cancel")
                            else "running",
                        },
                    )
                else:
                    status, data = 200, {"request_id": "from-body", "results": []}
                raw = json.dumps(data).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("X-Trace-ID", TRACE)
                    if status == 429:
                        self.send_header("Retry-After", "2")
                    else:
                        self.send_header("X-Request-ID", "from-header")
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

        cls._servers = [
            ThreadingHTTPServer(("127.0.0.1", 0), Handler),
            ThreadingHTTPServer(("127.0.0.1", 0), ForeignHandler),
        ]
        cls.base, cls.foreign = (
            f"http://127.0.0.1:{server.server_port}" for server in cls._servers
        )
        cls._threads = []
        for server in cls._servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls._threads.append(thread)

    @classmethod
    def tearDownClass(cls):
        for server, thread in zip(cls._servers, cls._threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join()

    def test_fixed_paths_and_typed_envelopes(self):
        client = MultiSearchClient(self.base + "/proxy/api/v1/", TOKEN)
        capabilities = client.capabilities()
        self.assertEqual(capabilities.body["modes"], {"quick": "available"})
        self.assertEqual(
            self.received[-1], ("GET", "/proxy/api/v1/capabilities", None, None)
        )
        response = client.search(
            {"query": "moon"}, request_id="caller-id", idempotency_key="search-001"
        )
        self.assertEqual(
            (response.status, response.request_id, response.trace_id),
            (200, "from-header", TRACE),
        )
        self.assertEqual(
            self.received[-1],
            (
                "POST",
                "/proxy/api/v1/search",
                f"Bearer {TOKEN}",
                {
                    "query": "moon",
                    "request_id": "caller-id",
                    "idempotency_key": "search-001",
                },
            ),
        )
        fetched = client.fetch({"url": "https://example.org"})
        self.assertEqual(fetched.status, 202)
        self.assertEqual(
            client.research(
                {"query": "moon", "idempotency_key": "research-001"}
            ).status,
            202,
        )
        self.assertEqual(client.search({"query": "moon", "mode": "deep"}).status, 202)
        self.assertEqual(
            client.get_job(fetched.body["job_id"]).body["status"], "running"
        )
        self.assertEqual(client.cancel_job(UUID(JOB_ID)).body["status"], "cancelled")
        self.assertEqual(self.received[-1][1], f"/proxy/api/v1/jobs/{JOB_ID}/cancel")
        self.assertFalse(self.foreign_received)

    def test_rejects_redirect_without_second_request(self):
        client = MultiSearchClient(self.base, TOKEN)
        with self.assertRaises(MultiSearchError) as error:
            client.search({"query": "redirect"})
        self.assertEqual(
            (error.exception.code, error.exception.status), ("REDIRECT_BLOCKED", 307)
        )
        self.assertFalse(self.foreign_received)

    def test_structured_rate_error_does_not_echo_raw_message(self):
        client = MultiSearchClient(self.base, TOKEN)
        with self.assertRaises(MultiSearchError) as error:
            client.search({"query": "throttle"})
        result = error.exception
        self.assertEqual((result.code, result.status), ("RATE_LIMITED", 429))
        self.assertEqual((result.request_id, result.trace_id), ("from-body", TRACE))
        self.assertTrue(result.retryable)
        self.assertEqual(result.retry_after_ms, 2000)
        self.assertNotIn(TOKEN, repr(result))

    def test_bounds_response_and_local_timeout(self):
        with self.assertRaisesRegex(MultiSearchError, "RESPONSE_TOO_LARGE"):
            MultiSearchClient(self.base, TOKEN, max_response_bytes=64).search(
                {"query": "oversize"}
            )
        with self.assertRaisesRegex(MultiSearchError, "TIMEOUT"):
            MultiSearchClient(self.base, TOKEN, timeout_s=0.025).search(
                {"query": "timeout"}
            )

    def test_input_failures_do_not_make_a_request(self):
        client = MultiSearchClient(self.base, TOKEN)
        before = len(self.received)
        for address in (
            "http://example.org",
            "http://localhost:8765",
            "https://user@example.org",
            "https://example.org?a=b",
        ):
            with self.subTest(address=address), self.assertRaises(ValueError):
                MultiSearchClient(address, TOKEN)
        with self.assertRaises(ValueError):
            MultiSearchClient(self.base, "key\r\nX-Header: injected")
        with self.assertRaises(ValueError):
            client.get_job("../other")
        with self.assertRaises(ValueError):
            client.search({"query": "a", "request_id": "one"}, request_id="two")
        with self.assertRaisesRegex(MultiSearchError, "REQUEST_TOO_LARGE"):
            client.search({"query": "月" * 12_000})
        with self.assertRaisesRegex(MultiSearchError, "API_KEY_REQUIRED"):
            MultiSearchClient(self.base).search({"query": "moon"})
        self.assertEqual(len(self.received), before)
