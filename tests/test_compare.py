import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from docbom.capture import capture_web
from docbom.compare import compare_web_captures
from docbom.ingest import init_store
from docbom.util import DocBOMError, load_yaml, write_yaml
from test_capture import ROOT, DEMO, local_site, run_cli, site_routes, write_profile


class WebCaptureCompareTests(unittest.TestCase):
    def prepare(self, root, base):
        store = root / "store"
        init_store(store)
        profile = root / "profile.yaml"
        write_profile(profile, f"{base}/docs/index.html")
        return store, profile

    def capture_pair(self, store, profile, first_routes, second_routes, server):
        server.routes = first_routes
        capture_web(profile, store, capture_id="capture-1", acquired_at="2026-11-25T10:00:00Z")
        server.routes = second_routes
        capture_web(profile, store, capture_id="capture-2", acquired_at="2026-11-25T11:00:00Z")

    def test_identical_recapture_ignores_warc_metadata_and_preserves_selection_files(self):
        with tempfile.TemporaryDirectory() as temporary, local_site(site_routes()) as (base, server):
            root = Path(temporary)
            store, profile = self.prepare(root, base)
            manifest = store / "manifest" / "existing-document.yaml"
            manifest.write_bytes((DEMO / "manifest" / "acme-mcu100-web-manual.yaml").read_bytes())
            lock = root / "project" / "docbom.lock.yaml"
            lock.parent.mkdir()
            lock.write_bytes((ROOT / "examples" / "project-old" / "docbom.lock.yaml").read_bytes())
            before = (manifest.read_bytes(), lock.read_bytes())
            self.capture_pair(store, profile, site_routes(), site_routes(), server)

            result = run_cli(
                "compare", "web", "--store", store,
                "--previous", "capture-1", "--candidate", "capture-2",
                "--compared-at", "2026-11-25T12:00:00Z",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("result: unchanged", result.stdout)
            report = load_yaml(store / "state" / "capture-2.diff.yaml")
            self.assertEqual("unchanged", report["result"])
            self.assertEqual("not_required", report["review_status"])
            self.assertEqual([], report["changes"])
            self.assertEqual(8, report["summary"]["unchanged"])  # five responses and three excluded URLs
            self.assertNotEqual(report["previous"]["warc_sha256"], report["candidate"]["warc_sha256"])
            self.assertEqual(
                report["previous"]["normalized_content_sha256"],
                report["candidate"]["normalized_content_sha256"],
            )
            self.assertFalse(report["summary"]["resource_bytes_changed"])
            self.assertFalse(report["summary"]["normalized_content_changed"])
            self.assertEqual("exact", report["profile_comparison"])
            comparison_path = store / "state" / "capture-2.diff.yaml"
            comparison_before = comparison_path.read_bytes()
            repeated = run_cli(
                "compare", "web", "--store", store,
                "--previous", "capture-1", "--candidate", "capture-2",
            )
            self.assertEqual(1, repeated.returncode)
            self.assertIn("comparison report already exists", repeated.stderr)
            self.assertEqual(comparison_before, comparison_path.read_bytes())
            self.assertEqual(before, (manifest.read_bytes(), lock.read_bytes()))

    def test_markup_only_change_is_raw_change_but_not_normalized_text_change(self):
        changed_routes = site_routes()
        status, content_type, body, headers = changed_routes["/docs/index.html"]
        changed_routes["/docs/index.html"] = (
            status, content_type, body.replace(b"<h1>ACME", b'<h1 class="new">ACME'), headers,
        )
        with tempfile.TemporaryDirectory() as temporary, local_site(site_routes()) as (base, server):
            root = Path(temporary)
            store, profile = self.prepare(root, base)
            self.capture_pair(store, profile, site_routes(), changed_routes, server)
            result = compare_web_captures(store, "capture-1", "capture-2")
            report = result["data"]
            self.assertEqual("changed", report["result"])
            self.assertEqual(1, len(report["changes"]))
            change = report["changes"][0]
            self.assertEqual(f"{base}/docs/index.html", change["url"])
            self.assertEqual(["sha256"], change["fields"])
            self.assertEqual(
                change["previous"]["normalized_content_sha256"],
                change["candidate"]["normalized_content_sha256"],
            )
            self.assertTrue(report["summary"]["resource_bytes_changed"])
            self.assertFalse(report["summary"]["normalized_content_changed"])

    def test_image_only_change_is_not_hidden_by_equal_normalized_html(self):
        changed_routes = site_routes()
        changed_routes["/docs/pixel.png"] = (200, "image/png", b"\x89PNG\r\n\x1a\nchanged-pixel", {})
        with tempfile.TemporaryDirectory() as temporary, local_site(site_routes()) as (base, server):
            root = Path(temporary)
            store, profile = self.prepare(root, base)
            self.capture_pair(store, profile, site_routes(), changed_routes, server)
            report = compare_web_captures(store, "capture-1", "capture-2")["data"]
            self.assertEqual("changed", report["result"])
            self.assertEqual(1, len(report["changes"]))
            change = report["changes"][0]
            self.assertEqual(f"{base}/docs/pixel.png", change["url"])
            self.assertEqual(["sha256"], change["fields"])
            self.assertNotIn("normalized_content_sha256", change["previous"])
            self.assertTrue(report["summary"]["resource_bytes_changed"])
            self.assertFalse(report["summary"]["normalized_content_changed"])
            self.assertEqual("pending", report["review_status"])

    def test_text_url_and_http_failure_changes_are_reported(self):
        previous_routes = site_routes()
        status, content_type, body, headers = previous_routes["/docs/index.html"]
        previous_routes["/docs/index.html"] = (
            status, content_type,
            body.replace(b'<a href="guide.html">Guide</a>', b'<a href="guide.html">Guide</a><a href="old.html">Old</a>'),
            headers,
        )
        previous_routes["/docs/old.html"] = (200, "text/html", b"<h1>Old page</h1>", {})

        candidate_routes = site_routes(missing_link=True)
        status, content_type, body, headers = candidate_routes["/docs/index.html"]
        candidate_routes["/docs/index.html"] = (
            status, content_type,
            body.replace(b'<a href="guide.html">Guide</a>', b'<a href="guide.html">Guide</a><a href="new.html">New</a>'),
            headers,
        )
        candidate_routes["/docs/guide.html"] = (200, "text/html; charset=utf-8", b"<h1>Updated timer guide</h1>", {})
        candidate_routes["/docs/new.html"] = (200, "text/html", b"<h1>New page</h1>", {})

        with tempfile.TemporaryDirectory() as temporary, local_site(previous_routes) as (base, server):
            root = Path(temporary)
            store, profile = self.prepare(root, base)
            self.capture_pair(store, profile, previous_routes, candidate_routes, server)
            result = run_cli(
                "compare", "web", "--store", store,
                "--previous", "capture-1", "--candidate", "capture-2",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("result: changed", result.stdout)
            report = load_yaml(store / "state" / "capture-2.diff.yaml")
            changes = {change["url"]: change for change in report["changes"]}
            self.assertEqual("added", changes[f"{base}/docs/new.html"]["change"])
            self.assertEqual("removed", changes[f"{base}/docs/old.html"]["change"])
            missing = changes[f"{base}/docs/missing.html"]
            self.assertEqual("added", missing["change"])
            self.assertEqual("failed", missing["candidate"]["outcome"])
            self.assertEqual(404, missing["candidate"]["status"])
            guide = changes[f"{base}/docs/guide.html"]
            self.assertEqual("changed", guide["change"])
            self.assertIn("sha256", guide["fields"])
            self.assertIn("normalized_content_sha256", guide["fields"])
            self.assertTrue(report["summary"]["resource_set_changed"])
            self.assertTrue(report["summary"]["resource_bytes_changed"])
            self.assertTrue(report["summary"]["normalized_content_changed"])
            self.assertTrue(report["summary"]["failures_changed"])
            self.assertTrue(report["summary"]["completeness_changed"])
            self.assertEqual("partial", report["candidate"]["completeness"])
            self.assertIsNone(report["review_cause"])

    def make_legacy_capture(self, store, capture_id):
        report_path = store / "state" / f"{capture_id}.capture.yaml"
        report = load_yaml(report_path)
        report.pop("profile")
        report.pop("normalization_algorithm")
        report["artifact"].pop("normalized_content_sha256", None)
        resource_path = store / report["artifact"]["resource_manifest_path"]
        resources = json.loads(resource_path.read_text(encoding="utf-8"))
        for resource in resources:
            resource.pop("normalized_content_sha256", None)
        content = (json.dumps(resources, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        legacy_path = store / "store" / "capture" / digest[:2] / digest[2:4] / f"{digest}.resources.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_bytes(content)
        report["artifact"]["resource_manifest_path"] = legacy_path.relative_to(store).as_posix()
        report["artifact"]["resource_manifest_sha256"] = digest
        write_yaml(report_path, report)

    def test_m3a_legacy_and_current_reports_compare_with_explicit_warning(self):
        with tempfile.TemporaryDirectory() as temporary, local_site(site_routes()) as (base, server):
            root = Path(temporary)
            store, profile = self.prepare(root, base)
            self.capture_pair(store, profile, site_routes(), site_routes(), server)
            self.make_legacy_capture(store, "capture-1")
            report = compare_web_captures(store, "capture-1", "capture-2")["data"]
            self.assertEqual("unchanged", report["result"])
            self.assertEqual("legacy_id_and_entrypoints", report["profile_comparison"])
            self.assertEqual(1, len(report["warnings"]))
            self.assertIn("does not embed the full Capture Profile", report["warnings"][0])
            self.assertIsNotNone(report["previous"]["normalized_content_sha256"])

    def test_different_profile_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, local_site(site_routes()) as (base, server):
            root = Path(temporary)
            store, profile = self.prepare(root, base)
            capture_web(profile, store, capture_id="capture-1")
            profile_data = load_yaml(profile)
            profile_data["scope"]["include_assets"] = False
            write_yaml(profile, profile_data)
            capture_web(profile, store, capture_id="capture-2")
            with self.assertRaisesRegex(DocBOMError, "Capture Profile content differs"):
                compare_web_captures(store, "capture-1", "capture-2")

    def test_tampered_capture_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, local_site(site_routes()) as (base, server):
            root = Path(temporary)
            store, profile = self.prepare(root, base)
            self.capture_pair(store, profile, site_routes(), site_routes(), server)
            report = load_yaml(store / "state" / "capture-2.capture.yaml")
            resource_path = store / report["artifact"]["resource_manifest_path"]
            original_resources = resource_path.read_bytes()
            resources = json.loads(original_resources)
            resources[0]["sha256"] = "0" * 64
            resource_path.write_text(json.dumps(resources), encoding="utf-8")
            with self.assertRaisesRegex(DocBOMError, "resource manifest hash verification failed"):
                compare_web_captures(store, "capture-1", "capture-2")
            resource_path.write_bytes(original_resources)
            warc_path = store / report["artifact"]["storage_path"]
            original_warc = warc_path.read_bytes()
            warc_path.write_bytes(original_warc + b"tampered")
            with self.assertRaisesRegex(DocBOMError, "capture WARC hash verification failed"):
                compare_web_captures(store, "capture-1", "capture-2")
            warc_path.write_bytes(original_warc)
            report["normalization_algorithm"] = "unsupported-v2"
            write_yaml(store / "state" / "capture-2.capture.yaml", report)
            with self.assertRaisesRegex(DocBOMError, "unsupported normalization algorithm"):
                compare_web_captures(store, "capture-1", "capture-2")


if __name__ == "__main__":
    unittest.main()
