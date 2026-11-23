import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo-store"


def load_yaml(path):
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_schema(name):
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SchemaTests(unittest.TestCase):
    def validate(self, schema_name, path):
        instance = load_yaml(path)
        validator = Draft202012Validator(load_schema(schema_name), format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
        self.assertEqual([], errors, f"{path.relative_to(ROOT)}: {[e.message for e in errors]}")

    def test_schemas_are_valid(self):
        for path in (ROOT / "schemas").glob("*.json"):
            with self.subTest(schema=path.name):
                Draft202012Validator.check_schema(load_schema(path.name))

    def test_examples_and_templates_validate(self):
        mappings = {
            "subject.schema.json": list((DEMO / "subjects").glob("*.yaml")) + [ROOT / "templates" / "subject.yaml"],
            "document.schema.json": list((DEMO / "manifest").glob("*.yaml")) + list((ROOT / "templates").glob("document.*.yaml")),
            "capture-profile.schema.json": list((DEMO / "capture-profiles").glob("*.yaml")) + [ROOT / "templates" / "web-capture-profile.yaml"],
            "project.schema.json": list((ROOT / "examples").glob("project-*/docbom.yaml")) + [ROOT / "templates" / "project.docbom.yaml"],
            "lock.schema.json": list((ROOT / "examples").glob("project-*/docbom.lock.yaml")),
        }
        for schema_name, paths in mappings.items():
            self.assertTrue(paths, schema_name)
            for path in paths:
                with self.subTest(schema=schema_name, file=path.relative_to(ROOT)):
                    self.validate(schema_name, path)

    def test_missing_git_commit_is_rejected(self):
        schema = load_schema("document.schema.json")
        instance = load_yaml(ROOT / "tests" / "fixtures" / "document-missing-git-commit.yaml")
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self.assertFalse(validator.is_valid(instance))

    def test_project_rejects_two_selectors(self):
        schema = load_schema("project.schema.json")
        instance = load_yaml(ROOT / "examples" / "project-old" / "docbom.yaml")
        instance = copy.deepcopy(instance)
        instance["documents"][0]["selection"] = "latest_capture"
        self.assertFalse(Draft202012Validator(schema).is_valid(instance))


class ArtifactTests(unittest.TestCase):
    def test_artifact_and_resource_hashes_match(self):
        for manifest_path in (DEMO / "manifest").glob("*.yaml"):
            document = load_yaml(manifest_path)
            for version in document["versions"]:
                for artifact in version["artifacts"]:
                    with self.subTest(document=document["id"], path=artifact["storage_path"]):
                        artifact_path = DEMO / artifact["storage_path"]
                        self.assertTrue(artifact_path.is_file())
                        self.assertEqual(artifact["sha256"], sha256(artifact_path))
                    if artifact["kind"] == "web_snapshot":
                        with self.subTest(document=document["id"], resource_manifest=artifact["resource_manifest_path"]):
                            resource_path = DEMO / artifact["resource_manifest_path"]
                            self.assertTrue(resource_path.is_file())
                            self.assertEqual(artifact["resource_manifest_sha256"], sha256(resource_path))

    def test_source_formats_are_recognizable(self):
        for path in (DEMO / "store" / "source").rglob("*.pdf"):
            self.assertTrue(path.read_bytes().startswith(b"%PDF-1.4"))
        for path in (DEMO / "store" / "capture").rglob("*.warc"):
            self.assertTrue(path.read_bytes().startswith(b"WARC/1.0"))


class ExpectedProjectTests(unittest.TestCase):
    def documents(self, project):
        lock = load_yaml(ROOT / "examples" / project / "docbom.lock.yaml")
        return {entry["id"]: entry for entry in lock["documents"]}

    def test_old_project_uses_old_revisions_and_a1_errata(self):
        docs = self.documents("project-old")
        self.assertEqual("Rev. 1", docs["acme/mcu100/datasheet"]["revision"])
        self.assertIn("acme/mcu100/errata", docs)
        web = docs["acme/mcu100/web-manual"]["artifacts"][0]
        self.assertEqual("2026-10-01T103000Z-web1", web["capture_id"])

    def test_current_project_uses_new_revisions_without_a1_errata(self):
        docs = self.documents("project-current")
        self.assertEqual("Rev. 2", docs["acme/mcu100/datasheet"]["revision"])
        self.assertNotIn("acme/mcu100/errata", docs)
        web = docs["acme/mcu100/web-manual"]["artifacts"][0]
        self.assertEqual("2026-11-01T103000Z-web2", web["capture_id"])

    def test_git_document_is_pinned_to_commit_and_path(self):
        for project in ["project-old", "project-current"]:
            with self.subTest(project=project):
                docs = self.documents(project)
                artifact = docs["acme/mcu100/reference-manual"]["artifacts"][0]
                self.assertEqual("0123456789abcdef0123456789abcdef01234567", artifact["commit"])
                self.assertEqual("docs/reference-manual.md", artifact["source_path"])

    def test_old_lock_does_not_follow_new_web_capture_automatically(self):
        old_requirements = load_yaml(ROOT / "examples" / "project-old" / "docbom.yaml")
        web_requirement = next(d for d in old_requirements["documents"] if d["id"] == "acme/mcu100/web-manual")
        self.assertEqual("latest_capture", web_requirement["selection"])
        old_web = self.documents("project-old")["acme/mcu100/web-manual"]["artifacts"][0]
        current_web = self.documents("project-current")["acme/mcu100/web-manual"]["artifacts"][0]
        self.assertNotEqual(old_web["capture_id"], current_web["capture_id"])


if __name__ == "__main__":
    unittest.main()
