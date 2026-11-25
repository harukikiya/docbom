import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml


class DocBOMError(Exception):
    """A user-facing DocBOM error."""


class DocBOMLoader(yaml.SafeLoader):
    """Safe YAML loader that keeps dates and timestamps as strings."""


DocBOMLoader.yaml_implicit_resolvers = {
    key: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag != "tag:yaml.org,2002:timestamp"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_yaml(path):
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.load(stream, Loader=DocBOMLoader)
    except FileNotFoundError as exc:
        raise DocBOMError(f"file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise DocBOMError(f"invalid YAML: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DocBOMError(f"YAML root must be an object: {path}")
    return data


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    binary = content.encode("utf-8") if isinstance(content, str) else content
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(binary)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_yaml(path, data):
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    atomic_write(path, text)


def sha256_bytes(content):
    return hashlib.sha256(content).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise DocBOMError(f"invalid date-time: {value!r}") from exc
    if parsed.tzinfo is None:
        raise DocBOMError(f"date-time must include a timezone: {value!r}")
    return parsed


def validate_relative_path(value, field="path"):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise DocBOMError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise DocBOMError(f"{field} must not be absolute or contain '..': {value}")
    return path


def path_in_store(store, storage_path):
    relative = validate_relative_path(storage_path, "storage_path")
    store = Path(store).resolve()
    candidate = store.joinpath(*relative.parts)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(store):
        raise DocBOMError(f"storage_path escapes the store: {storage_path}")
    return candidate


def format_error_path(path):
    parts = list(path)
    if not parts:
        return "$"
    result = "$"
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result
