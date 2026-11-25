import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from docbom.catalog import Catalog
from docbom.ingest import ingest_git, ingest_pdf, init_project, init_store
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


def add_requirement(project, identifier, revision):
    path = Path(project) / "docbom.yaml"
    data = load_yaml(path)
    data["documents"].append({"id": identifier, "revision": revision})
    write_yaml(path, data)


def copy_demo_subject(store):
    shutil.copy(
        DEMO / "subjects" / "acme-mcu100.yaml",
        Path(store) / "subjects" / "acme-mcu100.yaml",
    )


class InitTests(unittest.TestCase):
    def test_init_store_and_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "store"
            project = root / "project"
            result = run_cli("init", "store", store)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((store / "store" / "source").is_dir())
            result = run_cli(
                "init",
                "project",
                project,
                "--subject",
                "acme/mcu100",
                "--silicon-rev",
                "A1",
                "--sdk",
                "1.2",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            requirements = load_yaml(project / "docbom.yaml")
            self.assertEqual("A1", requirements["context"]["silicon_rev"])
            copy_demo_subject(store)
            result = run_cli("validate", "--store", store, "--project", project)
            self.assertEqual(0, result.returncode, result.stderr)


class IngestAndGetTests(unittest.TestCase):
    def test_pdf_ingest_resolve_and_get_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "store"
            project = root / "project"
            init_store(store)
            copy_demo_subject(store)
            source = DEMO / "store" / "source" / "a3" / "3a" / "a33a52d356d7cc40f4c316ea718e8817acdaca9d43745783fa19b89b323f6e69.pdf"
            result = ingest_pdf(
                source,
                store,
                "acme/mcu100/datasheet",
                "ACME",
                "Rev. 1",
                document_type="datasheet",
                subject="acme/mcu100",
                source_url="https://example.invalid/acme/mcu100/datasheet.pdf",
                acquired_at="2026-02-01T09:00:00Z",
            )
            self.assertTrue(result["artifact"].is_file())
            init_project(project, "acme/mcu100", silicon_rev="A1")
            add_requirement(project, "acme/mcu100/datasheet", "Rev. 1")
            resolved = run_cli(
                "resolve",
                "--store",
                store,
                "--project",
                project,
                "--catalog-revision",
                "c" * 40,
                "--generated-at",
                "2026-11-24T00:00:00Z",
            )
            self.assertEqual(0, resolved.returncode, resolved.stderr)
            validated = run_cli("validate", "--store", store, "--project", project)
            self.assertEqual(0, validated.returncode, validated.stderr)
            retrieved = run_cli(
                "get",
                "acme/mcu100/datasheet",
                "--store",
                store,
                "--lock",
                project,
                "--text",
                "--page",
                "1",
            )
            self.assertEqual(0, retrieved.returncode, retrieved.stderr)
            self.assertIn("ACME MCU100 Datasheet Rev. 1", retrieved.stdout)

    def test_git_ingest_and_get_line_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet", repository], check=True)
            document = repository / "docs" / "manual.md"
            document.parent.mkdir()
            document.write_text("# Manual\n\nfirst line\nsecond line\n", encoding="utf-8")
            subprocess.run(["git", "-C", repository, "add", "docs/manual.md"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "-c",
                    "user.name=DocBOM Test",
                    "-c",
                    "user.email=docbom@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "Add manual",
                ],
                check=True,
            )
            commit = subprocess.run(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            store = root / "store"
            project = root / "project"
            init_store(store)
            copy_demo_subject(store)
            result = ingest_git(
                repository.as_uri(),
                commit,
                "docs/manual.md",
                store,
                "acme/mcu100/reference-manual",
                "ACME",
                "docs-v1.0",
                document_type="reference-manual",
                subject="acme/mcu100",
                acquired_at="2026-11-24T00:00:00Z",
            )
            manifest = load_yaml(result["manifest"])
            artifact = manifest["versions"][0]["artifacts"][0]
            self.assertEqual(commit, artifact["commit"])
            self.assertEqual("docs/manual.md", artifact["source_path"])

            init_project(project, "acme/mcu100")
            add_requirement(project, "acme/mcu100/reference-manual", "docs-v1.0")
            lock = Catalog(store).resolve(project, "d" * 40, "2026-11-24T00:00:00Z")
            write_yaml(project / "docbom.lock.yaml", lock)
            retrieved = run_cli(
                "get",
                "acme/mcu100/reference-manual",
                "--store",
                store,
                "--lock",
                project,
                "--text",
                "--lines",
                "3:4",
            )
            self.assertEqual(0, retrieved.returncode, retrieved.stderr)
            self.assertEqual("first line\nsecond line\n", retrieved.stdout)


class ResolveTests(unittest.TestCase):
    def test_old_project_resolution_is_explicit_and_updates_capture(self):
        catalog = Catalog(DEMO)
        old_lock_path = ROOT / "examples" / "project-old" / "docbom.lock.yaml"
        before = old_lock_path.read_bytes()
        lock = catalog.resolve(
            ROOT / "examples" / "project-old",
            "a" * 40,
            "2026-11-24T00:00:00Z",
        )
        documents = {entry["id"]: entry for entry in lock["documents"]}
        self.assertEqual("Rev. 1", documents["acme/mcu100/datasheet"]["revision"])
        self.assertIn("acme/mcu100/errata", documents)
        web = documents["acme/mcu100/web-manual"]["artifacts"][0]
        self.assertEqual("2026-11-01T103000Z-web2", web["capture_id"])
        self.assertEqual(before, old_lock_path.read_bytes())
        catalog.validate_lock(old_lock_path)

    def test_current_project_resolution_excludes_a1_errata(self):
        lock = Catalog(DEMO).resolve(
            ROOT / "examples" / "project-current",
            "b" * 40,
            "2026-11-24T00:00:00Z",
        )
        documents = {entry["id"]: entry for entry in lock["documents"]}
        self.assertEqual("Rev. 2", documents["acme/mcu100/datasheet"]["revision"])
        self.assertNotIn("acme/mcu100/errata", documents)
        web = documents["acme/mcu100/web-manual"]["artifacts"][0]
        self.assertEqual("2026-11-01T103000Z-web2", web["capture_id"])

    def test_fixed_revision_cannot_bypass_applicability(self):
        project = {
            "context": {"subject": "acme/mcu100", "silicon_rev": "A2"},
            "documents": [{"id": "acme/mcu100/errata", "revision": "Rev. 1"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "docbom.yaml"
            write_yaml(path, project)
            with self.assertRaisesRegex(DocBOMError, "not applicable"):
                Catalog(DEMO).resolve(path, "b" * 40, "2026-11-24T00:00:00Z")

    def test_incomplete_latest_capture_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "store"
            shutil.copytree(DEMO, store)
            manifest_path = store / "manifest" / "acme-mcu100-web-manual.yaml"
            manifest = load_yaml(manifest_path)
            manifest["versions"][1]["artifacts"][0]["completeness"] = "partial"
            write_yaml(manifest_path, manifest)
            with self.assertRaisesRegex(DocBOMError, "latest Web capture is incomplete"):
                Catalog(store).resolve(
                    ROOT / "examples" / "project-current",
                    "b" * 40,
                    "2026-11-24T00:00:00Z",
                )


class ValidationTests(unittest.TestCase):
    def test_cli_validates_store_project_and_lock(self):
        for project in ("project-old", "project-current"):
            with self.subTest(project=project):
                result = run_cli(
                    "validate",
                    "--store",
                    DEMO,
                    "--project",
                    ROOT / "examples" / project,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("OK:", result.stdout)

    def test_validate_detects_artifact_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "store"
            shutil.copytree(DEMO, store)
            artifact = store / "store" / "source" / "a3" / "3a" / "a33a52d356d7cc40f4c316ea718e8817acdaca9d43745783fa19b89b323f6e69.pdf"
            artifact.write_bytes(artifact.read_bytes() + b"\n% changed\n")
            with self.assertRaisesRegex(DocBOMError, "SHA-256 mismatch"):
                Catalog(store).validate()

    def test_validate_detects_unknown_relation_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "store"
            shutil.copytree(DEMO, store)
            manifest_path = store / "manifest" / "acme-mcu100-reference-manual.yaml"
            manifest = load_yaml(manifest_path)
            manifest["relations"][0]["target"] = "acme/mcu100/missing"
            write_yaml(manifest_path, manifest)
            with self.assertRaisesRegex(DocBOMError, "unknown relation target"):
                Catalog(store).validate()


if __name__ == "__main__":
    unittest.main()
