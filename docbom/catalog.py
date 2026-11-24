import json
import re
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from . import __version__
from .util import (
    DocBOMError,
    format_error_path,
    load_yaml,
    parse_datetime,
    path_in_store,
    sha256_file,
    utc_now,
)


SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
LOCK_ARTIFACT_FIELDS = (
    "role",
    "kind",
    "repository_url",
    "commit",
    "source_path",
    "capture_id",
    "sha256",
    "resource_manifest_sha256",
)


def schema_errors(data, schema_name, origin="$", schema_dir=SCHEMA_DIR):
    schema_path = Path(schema_dir) / schema_name
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DocBOMError(f"schema not found: {schema_path}") from exc
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.absolute_path))
    return [
        f"{origin}: {format_error_path(error.absolute_path)}: {error.message}"
        for error in errors
    ]


def require_valid(data, schema_name, origin="$"):
    errors = schema_errors(data, schema_name, origin)
    if errors:
        raise DocBOMError("validation failed:\n" + "\n".join(f"- {error}" for error in errors))


def project_file(path):
    path = Path(path)
    return path / "docbom.yaml" if path.is_dir() else path


def lock_file(path):
    path = Path(path)
    return path / "docbom.lock.yaml" if path.is_dir() else path


class Catalog:
    def __init__(self, store):
        self.store = Path(store)
        self.subject_files = sorted((self.store / "subjects").glob("*.yaml"))
        self.document_files = sorted((self.store / "manifest").glob("*.yaml"))
        self.capture_profile_files = sorted((self.store / "capture-profiles").glob("*.yaml"))
        self.subjects = [(path, load_yaml(path)) for path in self.subject_files]
        self.documents = [(path, load_yaml(path)) for path in self.document_files]
        self.capture_profiles = [(path, load_yaml(path)) for path in self.capture_profile_files]

    def _relative(self, path):
        try:
            return str(path.relative_to(self.store))
        except ValueError:
            return str(path)

    def validate(self):
        errors = []
        for path, data in self.subjects:
            errors.extend(schema_errors(data, "subject.schema.json", self._relative(path)))
        for path, data in self.documents:
            errors.extend(schema_errors(data, "document.schema.json", self._relative(path)))
        for path, data in self.capture_profiles:
            errors.extend(schema_errors(data, "capture-profile.schema.json", self._relative(path)))
        if errors:
            raise DocBOMError("validation failed:\n" + "\n".join(f"- {error}" for error in errors))
        self._validate_semantics()
        return {
            "subjects": len(self.subjects),
            "documents": len(self.documents),
            "capture_profiles": len(self.capture_profiles),
        }

    def _validate_semantics(self):
        errors = []
        subject_ids = [data["id"] for _, data in self.subjects]
        document_ids = [data["id"] for _, data in self.documents]
        for kind, identifiers in (("subject", subject_ids), ("document", document_ids)):
            for identifier, count in Counter(identifiers).items():
                if count > 1:
                    errors.append(f"duplicate {kind} id: {identifier}")
        known_subjects = set(subject_ids)
        known_documents = set(document_ids)

        for path, document in self.documents:
            origin = self._relative(path)
            labels = [
                version["revision"]["label"]
                for version in document["versions"]
                if version["revision"]["label"] is not None
            ]
            for label, count in Counter(labels).items():
                if count > 1:
                    errors.append(f"{origin}: duplicate revision label: {label}")

            subject_entries = list(document.get("subjects", []))
            relation_entries = list(document.get("relations", []))
            for version in document["versions"]:
                subject_entries.extend(version.get("subjects", []))
                relation_entries.extend(version.get("relations", []))
                for artifact in version["artifacts"]:
                    self._check_artifact(document, artifact, origin, errors)
            for subject in subject_entries:
                if subject["ref"] not in known_subjects:
                    errors.append(f"{origin}: unknown subject: {subject['ref']}")
            for relation in relation_entries:
                target = relation["target"]
                allowed = known_documents | known_subjects if relation["type"] == "applies_to" else known_documents
                if target not in allowed:
                    errors.append(f"{origin}: unknown relation target: {target}")
        if errors:
            raise DocBOMError("validation failed:\n" + "\n".join(f"- {error}" for error in errors))

    def _check_artifact(self, document, artifact, origin, errors):
        label = f"{origin}: {document['id']}:{artifact['sha256']}"
        try:
            path = path_in_store(self.store, artifact["storage_path"])
            if not path.is_file():
                errors.append(f"{label}: file not found: {artifact['storage_path']}")
            else:
                actual = sha256_file(path)
                if actual != artifact["sha256"]:
                    errors.append(f"{label}: SHA-256 mismatch: actual {actual}")
        except DocBOMError as exc:
            errors.append(f"{label}: {exc}")
        if artifact["kind"] != "web_snapshot":
            return
        try:
            resource_path = path_in_store(self.store, artifact["resource_manifest_path"])
            if not resource_path.is_file():
                errors.append(f"{label}: resource manifest not found")
            else:
                actual = sha256_file(resource_path)
                if actual != artifact["resource_manifest_sha256"]:
                    errors.append(f"{label}: resource manifest SHA-256 mismatch: actual {actual}")
        except DocBOMError as exc:
            errors.append(f"{label}: {exc}")

    def document(self, identifier):
        matches = [data for _, data in self.documents if data["id"] == identifier]
        if not matches:
            raise DocBOMError(f"document not found: {identifier}")
        if len(matches) > 1:
            raise DocBOMError(f"duplicate document id: {identifier}")
        return matches[0]

    def validate_project(self, project, check_lock=True):
        path = project_file(project)
        data = load_yaml(path)
        require_valid(data, "project.schema.json", str(path))
        errors = []
        known_subjects = {subject["id"] for _, subject in self.subjects}
        if data["context"]["subject"] not in known_subjects:
            errors.append(f"unknown project subject: {data['context']['subject']}")
        identifiers = [entry["id"] for entry in data["documents"]]
        for identifier, count in Counter(identifiers).items():
            if count > 1:
                errors.append(f"duplicate project document id: {identifier}")
        for identifier in identifiers:
            try:
                self.document(identifier)
            except DocBOMError as exc:
                errors.append(str(exc))
        if errors:
            raise DocBOMError("validation failed:\n" + "\n".join(f"- {error}" for error in errors))
        if check_lock:
            candidate = path.parent / "docbom.lock.yaml"
            if candidate.is_file():
                self.validate_lock(candidate)
        return data

    def validate_lock(self, lock):
        path = lock_file(lock)
        data = load_yaml(path)
        require_valid(data, "lock.schema.json", str(path))
        errors = []
        for entry in data["documents"]:
            try:
                document = self.document(entry["id"])
            except DocBOMError as exc:
                errors.append(str(exc))
                continue
            versions = [
                version
                for version in document["versions"]
                if version["revision"]["label"] == entry["revision"]
            ]
            for locked in entry["artifacts"]:
                matches = [
                    artifact
                    for version in versions
                    for artifact in version["artifacts"]
                    if artifact["sha256"] == locked["sha256"] and artifact["kind"] == locked["kind"]
                ]
                if not matches:
                    errors.append(f"{path}: locked artifact not found: {entry['id']}:{locked['sha256']}")
                    continue
                artifact = matches[0]
                for field in LOCK_ARTIFACT_FIELDS:
                    if field in locked and locked[field] != artifact.get(field):
                        errors.append(f"{path}: locked {field} differs: {entry['id']}:{locked['sha256']}")
                if artifact["kind"] == "web_snapshot" and artifact["completeness"] != "complete_within_declared_scope":
                    errors.append(f"{path}: incomplete Web capture is locked: {artifact['capture_id']}")
        if errors:
            raise DocBOMError("validation failed:\n" + "\n".join(f"- {error}" for error in errors))
        return data

    def resolve(self, project, catalog_revision=None, generated_at=None):
        self.validate()
        requirements_path = project_file(project)
        requirements = self.validate_project(requirements_path, check_lock=False)
        catalog_revision = catalog_revision or self._git_revision()
        if not re.fullmatch(r"[0-9a-f]{40}", catalog_revision):
            raise DocBOMError("catalog_revision must be a 40-character lowercase Git commit")
        generated_at = generated_at or utc_now()
        parse_datetime(generated_at)

        entries = []
        for requirement in requirements["documents"]:
            document = self.document(requirement["id"])
            if "revision" in requirement:
                versions = [
                    version
                    for version in document["versions"]
                    if version["revision"]["label"] == requirement["revision"]
                ]
                if not versions:
                    raise DocBOMError(f"revision not found: {document['id']}:{requirement['revision']}")
                for version in versions:
                    self._require_applicable(document, version, requirements["context"])
                    self._require_complete(version)
                    entries.append(self._lock_entry(document, version, version["artifacts"]))
                continue

            if requirement["selection"] == "applicable":
                for version in document["versions"]:
                    if self._applicable(document, version, requirements["context"]):
                        self._require_complete(version)
                        entries.append(self._lock_entry(document, version, version["artifacts"]))
                continue

            if requirement["selection"] == "latest_capture":
                entries.append(self._latest_capture(document, requirements["context"]))
                continue
            raise DocBOMError(f"unsupported selection: {requirement['selection']}")

        lock = {
            "lock_version": 1,
            "catalog_revision": catalog_revision,
            "resolver_version": __version__,
            "generated_at": generated_at,
            "documents": entries,
        }
        require_valid(lock, "lock.schema.json", "generated lock")
        return lock

    def _git_revision(self):
        import subprocess

        result = subprocess.run(
            ["git", "-C", str(self.store), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise DocBOMError("cannot determine catalog revision; pass --catalog-revision")
        return result.stdout.strip()

    def _lock_entry(self, document, version, artifacts):
        return {
            "id": document["id"],
            "revision": version["revision"]["label"],
            "artifacts": [
                {field: artifact[field] for field in LOCK_ARTIFACT_FIELDS if field in artifact}
                for artifact in artifacts
            ],
        }

    def _latest_capture(self, document, context):
        candidates = []
        for version in document["versions"]:
            if not self._applicable(document, version, context):
                continue
            for artifact in version["artifacts"]:
                if artifact["kind"] == "web_snapshot":
                    candidates.append((version, artifact))
        if not candidates:
            raise DocBOMError(f"no applicable Web capture: {document['id']}")
        version, artifact = max(
            candidates,
            key=lambda candidate: (
                parse_datetime(candidate[1]["acquired_at"]),
                candidate[1]["capture_id"],
            ),
        )
        if artifact["completeness"] != "complete_within_declared_scope":
            raise DocBOMError(f"latest Web capture is incomplete: {artifact['capture_id']}")
        return self._lock_entry(document, version, [artifact])

    def _applicable(self, document, version, context):
        subjects = version.get("subjects", document.get("subjects", []))
        if not subjects:
            return True
        for subject in subjects:
            if subject["ref"] != context["subject"]:
                continue
            silicon_revs = subject.get("silicon_revs")
            if silicon_revs is not None and context.get("silicon_rev") not in silicon_revs:
                continue
            attributes = subject.get("attributes", {})
            if any(context.get(key) != value for key, value in attributes.items()):
                continue
            return True
        return False

    def _require_applicable(self, document, version, context):
        if not self._applicable(document, version, context):
            label = version["revision"]["label"]
            raise DocBOMError(f"document revision is not applicable: {document['id']}:{label}")

    def _require_complete(self, version):
        for artifact in version["artifacts"]:
            if artifact["kind"] == "web_snapshot" and artifact["completeness"] != "complete_within_declared_scope":
                raise DocBOMError(f"incomplete Web capture cannot be locked: {artifact['capture_id']}")

    def find_locked_artifact(self, lock, identifier, revision=None, sha256=None):
        self.validate()
        self.validate_lock(lock)
        lock_data = load_yaml(lock_file(lock))
        candidates = []
        for entry in lock_data["documents"]:
            if entry["id"] != identifier:
                continue
            if revision is not None and entry["revision"] != revision:
                continue
            for locked in entry["artifacts"]:
                if sha256 is not None and locked["sha256"] != sha256:
                    continue
                candidates.append((entry, locked))
        if not candidates:
            raise DocBOMError(f"locked artifact not found: {identifier}")
        if len(candidates) > 1:
            raise DocBOMError("multiple locked artifacts match; specify --revision or --sha256")

        entry, locked = candidates[0]
        document = self.document(identifier)
        matches = []
        for version in document["versions"]:
            if version["revision"]["label"] != entry["revision"]:
                continue
            for artifact in version["artifacts"]:
                if artifact["sha256"] == locked["sha256"] and artifact["kind"] == locked["kind"]:
                    matches.append((version, artifact))
        if not matches:
            raise DocBOMError(f"catalog artifact not found: {identifier}:{locked['sha256']}")
        storage_paths = {artifact["storage_path"] for _, artifact in matches}
        if len(storage_paths) != 1:
            raise DocBOMError(f"ambiguous catalog artifact: {identifier}:{locked['sha256']}")
        version, artifact = matches[0]
        path = path_in_store(self.store, artifact["storage_path"])
        if not path.is_file() or sha256_file(path) != locked["sha256"]:
            raise DocBOMError(f"artifact hash verification failed: {artifact['storage_path']}")
        return artifact, path
