import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from docbom.capture import capture_web
from docbom.ingest import init_store
from docbom.util import DocBOMError, load_yaml, write_yaml


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo-store"


def run_cli(*args):
    return subprocess.run(
        ["python3", "-m", "docbom", *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(self.path)
        route = self.server.routes.get(self.path)
        if route is None:
            route = (404, "text/plain; charset=utf-8", b"not found", {})
        status, content_type, body, extra_headers = route
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@contextmanager
def local_site(routes):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.routes = routes
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def site_routes(missing_link=False):
    missing = '<a href="/docs/missing.html">Missing</a>' if missing_link else ""
    index = f"""<!doctype html>
<html><head><link rel="stylesheet" href="/docs/site.css"></head><body>
<h1>ACME static manual</h1>
<a href="guide.html">Guide</a>
<img src="/docs/pixel.png">
<a href="/outside.html">Outside</a>
<a href="/docs/private.html">Excluded</a>
<a href="https://example.invalid/external.html">External</a>
{missing}
<script>document.write('<a href="/docs/script-created.html">not crawled</a>')</script>
</body></html>
""".encode()
    return {
        "/docs/index.html": (200, "text/html; charset=utf-8", index, {}),
        "/docs/guide.html": (200, "text/html; charset=utf-8", b"<h1>Timer guide</h1>", {}),
        "/docs/site.css": (200, "text/css", b"body { background: url('/docs/background.png'); }", {}),
        "/docs/pixel.png": (200, "image/png", b"\x89PNG\r\n\x1a\npixel", {}),
        "/docs/background.png": (200, "image/png", b"\x89PNG\r\n\x1a\nbackground", {}),
        "/docs/private.html": (200, "text/html", b"must not be fetched", {}),
        "/docs/script-created.html": (200, "text/html", b"must not be fetched", {}),
        "/outside.html": (200, "text/html", b"must not be fetched", {}),
    }


def write_profile(path, entrypoint, include_assets=True):
    write_yaml(
        path,
        {
            "id": "acme-static-manual",
            "version": 1,
            "entrypoints": [entrypoint],
            "scope": {
                "allowed_paths": ["/docs/"],
                "include_assets": include_assets,
                "excluded_patterns": ["*/private.html"],
            },
            "environment": {
                "tool_version": "docbom-test",
                "browser_version": "not-used-static-capture",
                "locale": "en-US",
                "timezone": "UTC",
                "viewport": "1280x720",
                "clean_browser_profile": True,
            },
            "access_context": {
                "authentication": "none",
                "documentation_version": None,
                "language": "en",
            },
        },
    )


def parse_warc(content):
    records = []
    offset = 0
    while offset < len(content):
        assert content.startswith(b"WARC/1.0\r\n", offset)
        header_end = content.index(b"\r\n\r\n", offset)
        warc_headers = {}
        for line in content[offset:header_end].decode("utf-8").split("\r\n")[1:]:
            name, value = line.split(":", 1)
            warc_headers[name] = value.strip()
        payload_start = header_end + 4
        payload_length = int(warc_headers["Content-Length"])
        payload = content[payload_start:payload_start + payload_length]
        http_header_end = payload.index(b"\r\n\r\n")
        http_headers = {}
        for line in payload[:http_header_end].decode("utf-8").split("\r\n")[1:]:
            name, value = line.split(":", 1)
            http_headers[name.lower()] = value.strip()
        body = payload[http_header_end + 4:]
        assert int(http_headers["content-length"]) == len(body)
        records.append((warc_headers, payload[:http_header_end].split(b"\r\n", 1)[0], body))
        offset = payload_start + payload_length
        assert content[offset:offset + 4] == b"\r\n\r\n"
        offset += 4
    return records


class StaticWebCaptureTests(unittest.TestCase):
    def test_capture_resources_warc_and_recapture_immutability(self):
        with tempfile.TemporaryDirectory() as temporary, local_site(site_routes()) as (base, server):
            root = Path(temporary)
            store = root / "store"
            init_store(store)
            manifest = store / "manifest" / "existing-document.yaml"
            manifest.write_bytes((DEMO / "manifest" / "acme-mcu100-web-manual.yaml").read_bytes())
            lock = root / "project" / "docbom.lock.yaml"
            lock.parent.mkdir()
            lock.write_bytes((ROOT / "examples" / "project-old" / "docbom.lock.yaml").read_bytes())
            manifest_before = manifest.read_bytes()
            lock_before = lock.read_bytes()
            profile = root / "profile.yaml"
            write_profile(profile, f"{base}/docs/index.html")

            first = run_cli(
                "capture", "web", profile,
                "--store", store,
                "--capture-id", "capture-1",
                "--acquired-at", "2026-11-25T10:00:00Z",
            )
            self.assertEqual(0, first.returncode, first.stderr)
            first_report_path = store / "state" / "capture-1.capture.yaml"
            report = load_yaml(first_report_path)
            self.assertEqual({"fetched": 5, "succeeded": 5, "failed": 0, "excluded": 3}, report["summary"])
            self.assertEqual("complete_within_declared_scope", report["artifact"]["completeness"])
            resources = json.loads((store / report["artifact"]["resource_manifest_path"]).read_text(encoding="utf-8"))
            by_url = {entry["url"]: entry for entry in resources}
            self.assertEqual(5, len(by_url))
            for path, route in site_routes().items():
                if path in {"/docs/index.html", "/docs/guide.html", "/docs/site.css", "/docs/pixel.png", "/docs/background.png"}:
                    entry = by_url[f"{base}{path}"]
                    self.assertEqual(200, entry["status"])
                    self.assertEqual(route[1], entry["content_type"])
                    import hashlib
                    self.assertEqual(hashlib.sha256(route[2]).hexdigest(), entry["sha256"])
            excluded = {event["url"]: event["reason"] for event in report["events"] if event["outcome"] == "excluded"}
            self.assertEqual("outside_allowed_paths", excluded[f"{base}/outside.html"])
            self.assertEqual("excluded_pattern", excluded[f"{base}/docs/private.html"])
            self.assertEqual("cross_origin", excluded["https://example.invalid/external.html"])
            self.assertNotIn("/outside.html", server.requests)
            self.assertNotIn("/docs/private.html", server.requests)
            self.assertNotIn("/docs/script-created.html", server.requests)

            warc_path = store / report["artifact"]["storage_path"]
            warc_before = warc_path.read_bytes()
            report_before = first_report_path.read_bytes()
            records = parse_warc(warc_before)
            self.assertEqual(5, len(records))
            self.assertEqual(5, len({headers["WARC-Record-ID"] for headers, _, _ in records}))
            for headers, _, body in records:
                entry = by_url[headers["WARC-Target-URI"]]
                import hashlib
                self.assertEqual(entry["sha256"], hashlib.sha256(body).hexdigest())

            second = capture_web(
                profile, store,
                capture_id="capture-2",
                acquired_at="2026-11-25T11:00:00Z",
            )
            self.assertTrue(second["complete"])
            self.assertEqual(report["artifact"]["resource_manifest_sha256"], second["artifact"]["resource_manifest_sha256"])
            self.assertNotEqual(report["artifact"]["sha256"], second["artifact"]["sha256"])
            self.assertEqual(warc_before, warc_path.read_bytes())
            self.assertEqual(report_before, first_report_path.read_bytes())
            self.assertEqual(manifest_before, manifest.read_bytes())
            self.assertEqual(lock_before, lock.read_bytes())
            request_count = len(server.requests)
            with self.assertRaisesRegex(DocBOMError, "capture report already exists"):
                capture_web(profile, store, capture_id="capture-1")
            self.assertEqual(request_count, len(server.requests))

    def test_include_assets_false_captures_only_html(self):
        with tempfile.TemporaryDirectory() as temporary, local_site(site_routes()) as (base, server):
            root = Path(temporary)
            store = root / "store"
            init_store(store)
            profile = root / "profile.yaml"
            write_profile(profile, f"{base}/docs/index.html", include_assets=False)
            result = capture_web(profile, store, capture_id="html-only")
            self.assertTrue(result["complete"])
            report = load_yaml(result["report"])
            resources = json.loads(result["resources"].read_text(encoding="utf-8"))
            self.assertEqual({f"{base}/docs/index.html", f"{base}/docs/guide.html"}, {item["url"] for item in resources})
            asset_events = [event for event in report["events"] if event.get("reason") == "assets_disabled"]
            self.assertEqual({f"{base}/docs/site.css", f"{base}/docs/pixel.png"}, {event["url"] for event in asset_events})
            self.assertNotIn("/docs/site.css", server.requests)
            self.assertNotIn("/docs/pixel.png", server.requests)
            self.assertNotIn("/docs/background.png", server.requests)

    def test_http_failure_is_saved_as_partial_capture(self):
        with tempfile.TemporaryDirectory() as temporary, local_site(site_routes(missing_link=True)) as (base, server):
            root = Path(temporary)
            store = root / "store"
            init_store(store)
            profile = root / "profile.yaml"
            write_profile(profile, f"{base}/docs/index.html")
            result = run_cli("capture", "web", profile, "--store", store, "--capture-id", "with-failure")
            self.assertEqual(1, result.returncode)
            self.assertIn("status: partial", result.stdout)
            report = load_yaml(store / "state" / "with-failure.capture.yaml")
            self.assertEqual(1, report["summary"]["failed"])
            self.assertEqual("partial", report["artifact"]["completeness"])
            missing = next(event for event in report["events"] if event["url"] == f"{base}/docs/missing.html")
            self.assertEqual("failed", missing["outcome"])
            self.assertEqual(404, missing["status"])
            resources = json.loads((store / report["artifact"]["resource_manifest_path"]).read_text(encoding="utf-8"))
            missing_resource = next(item for item in resources if item["url"] == missing["url"])
            self.assertEqual(404, missing_resource["status"])
            records = parse_warc((store / report["artifact"]["storage_path"]).read_bytes())
            self.assertIn(b"HTTP/1.1 404 Not Found", [status for _, status, _ in records])

    def test_redirect_is_not_followed_or_silently_accepted(self):
        routes = site_routes()
        routes["/docs/index.html"] = (302, "text/plain", b"redirect", {"Location": "/docs/guide.html"})
        with tempfile.TemporaryDirectory() as temporary, local_site(routes) as (base, server):
            root = Path(temporary)
            store = root / "store"
            init_store(store)
            profile = root / "profile.yaml"
            write_profile(profile, f"{base}/docs/index.html")
            result = capture_web(profile, store, capture_id="redirect")
            self.assertFalse(result["complete"])
            self.assertEqual(["/docs/index.html"], server.requests)
            report = load_yaml(result["report"])
            self.assertEqual(302, report["events"][0]["status"])
            self.assertEqual("failed", report["events"][0]["outcome"])


if __name__ == "__main__":
    unittest.main()
