import io
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .catalog import require_valid
from .util import (
    DocBOMError,
    atomic_write,
    load_yaml,
    sha256_bytes,
    utc_now,
    validate_relative_path,
    write_yaml,
)


STORE_DIRECTORIES = (
    "subjects",
    "manifest",
    "capture-profiles",
    "store/source",
    "store/capture",
    "derived",
    "state",
    "index",
    "reports",
)


def init_store(path):
    path = Path(path)
    if path.exists() and not path.is_dir():
        raise DocBOMError(f"store path is not a directory: {path}")
    for directory in STORE_DIRECTORIES:
        (path / directory).mkdir(parents=True, exist_ok=True)
    return path


def init_project(path, subject, silicon_rev=None, sdk=None):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    target = path / "docbom.yaml"
    if target.exists():
        raise DocBOMError(f"project file already exists: {target}")
    context = {"subject": subject}
    if silicon_rev is not None:
        context["silicon_rev"] = silicon_rev
    if sdk is not None:
        context["sdk"] = sdk
    data = {"context": context, "documents": []}
    require_valid(data, "project.schema.json", str(target))
    write_yaml(target, data)
    return target


def _title_from_pdf(content):
    try:
        from pypdf import PdfReader

        metadata = PdfReader(io.BytesIO(content)).metadata
        if metadata and metadata.title:
            return metadata.title
    except Exception:
        return None
    return None


def ingest_pdf(
    source,
    store,
    identifier,
    publisher,
    revision,
    title=None,
    document_type="manual",
    document_number=None,
    subject=None,
    source_url=None,
    published=None,
    acquired_at=None,
):
    source = Path(source)
    try:
        content = source.read_bytes()
    except FileNotFoundError as exc:
        raise DocBOMError(f"PDF not found: {source}") from exc
    if not content.startswith(b"%PDF-"):
        raise DocBOMError(f"not a PDF file: {source}")
    title = title or _title_from_pdf(content) or source.stem
    return ingest_bytes(
        content=content,
        suffix=".pdf",
        store=store,
        identifier=identifier,
        publisher=publisher,
        revision=revision,
        title=title,
        document_type=document_type,
        document_number=document_number,
        subject=subject,
        kind="pdf",
        source_url=source_url,
        published=published,
        acquired_at=acquired_at,
    )


def ingest_git(
    repository,
    commit,
    source_path,
    store,
    identifier,
    publisher,
    revision,
    title=None,
    document_type="manual",
    document_number=None,
    subject=None,
    tag=None,
    published=None,
    acquired_at=None,
):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DocBOMError("Git commit must be a 40-character lowercase commit ID")
    validate_relative_path(source_path, "source_path")
    with tempfile.TemporaryDirectory(prefix="docbom-git-") as temporary:
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--no-checkout", "--", repository, temporary],
            capture_output=True,
            text=True,
            check=False,
        )
        if clone.returncode != 0:
            raise DocBOMError(f"git clone failed: {clone.stderr.strip()}")
        show = subprocess.run(
            ["git", "-C", temporary, "show", f"{commit}:{source_path}"],
            capture_output=True,
            check=False,
        )
        if show.returncode != 0:
            message = show.stderr.decode("utf-8", errors="replace").strip()
            raise DocBOMError(f"git show failed: {message}")
        content = show.stdout
    title = title or PurePosixPath(source_path).stem
    return ingest_bytes(
        content=content,
        suffix=PurePosixPath(source_path).suffix,
        store=store,
        identifier=identifier,
        publisher=publisher,
        revision=revision,
        title=title,
        document_type=document_type,
        document_number=document_number,
        subject=subject,
        kind="git_file",
        repository_url=repository,
        commit=commit,
        source_path=source_path,
        tag=tag,
        published=published,
        acquired_at=acquired_at,
    )


def ingest_bytes(
    content,
    suffix,
    store,
    identifier,
    publisher,
    revision,
    title,
    document_type,
    document_number,
    subject,
    kind,
    source_url=None,
    repository_url=None,
    commit=None,
    source_path=None,
    tag=None,
    published=None,
    acquired_at=None,
):
    store = Path(store)
    if not (store / "manifest").is_dir() or not (store / "store" / "source").is_dir():
        raise DocBOMError(f"store is not initialized: {store}")
    if not title or not publisher:
        raise DocBOMError("title and publisher must not be empty")
    acquired_at = acquired_at or utc_now()
    digest = sha256_bytes(content)
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix or ""):
        suffix = ""
    storage_path = f"store/source/{digest[:2]}/{digest[2:4]}/{digest}{suffix}"
    artifact = {
        "role": "source",
        "kind": kind,
    }
    if source_url is not None:
        artifact["source_url"] = source_url
    if repository_url is not None:
        artifact["repository_url"] = repository_url
        artifact["commit"] = commit
        artifact["source_path"] = source_path
    if tag is not None:
        artifact["tag"] = tag
    artifact.update(
        {
            "acquired_at": acquired_at,
            "storage_path": storage_path,
            "sha256": digest,
        }
    )

    manifest = store / "manifest" / f"{identifier.replace('/', '-')}.yaml"
    if manifest.exists():
        document = load_yaml(manifest)
        if document["id"] != identifier:
            raise DocBOMError(f"manifest id mismatch: {manifest}")
    else:
        document = {
            "id": identifier,
            "document_type": document_type,
            "title": title,
            "publisher": publisher,
            "document_number": document_number,
            "subjects": [{"ref": subject}] if subject else [],
            "relations": [],
            "access": {
                "source_access": "unknown",
                "confidentiality": "unknown",
                "redistribution": "unknown",
                "ai_egress": "unknown",
            },
            "versions": [],
        }

    versions = [
        version
        for version in document["versions"]
        if version["revision"]["label"] == revision
    ]
    if versions:
        version = versions[0]
    else:
        version = {
            "revision": {
                "label": revision,
                "label_raw": revision,
                "published": published,
            },
            "artifacts": [],
        }
        document["versions"].append(version)
    if not any(existing["sha256"] == digest for existing in version["artifacts"]):
        version["artifacts"].append(artifact)
    require_valid(document, "document.schema.json", str(manifest))

    destination = store.joinpath(*storage_path.split("/"))
    if destination.exists():
        if sha256_bytes(destination.read_bytes()) != digest:
            raise DocBOMError(f"hash-addressed file collision: {destination}")
    else:
        atomic_write(destination, content)
    write_yaml(manifest, document)
    return {
        "manifest": manifest,
        "artifact": destination,
        "storage_path": storage_path,
        "sha256": digest,
    }
