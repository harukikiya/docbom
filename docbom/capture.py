import fnmatch
import json
import re
import uuid
from collections import deque
from datetime import timezone
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .catalog import require_valid
from .util import DocBOMError, atomic_write, load_yaml, parse_datetime, sha256_bytes, utc_now
from .webtext import (
    HTML_CONTENT_TYPES,
    NORMALIZATION_ALGORITHM,
    aggregate_normalized_digest,
    decode_body,
    media_type,
    normalized_resource_digest,
)


CSS_CONTENT_TYPES = {"text/css"}
CSS_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
CSS_IMPORT_PATTERN = re.compile(r"@import\s+(['\"])(.*?)\1", re.IGNORECASE)
CAPTURE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _HTMLReferenceParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base = None
        self.references = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "base" and self.base is None:
            self.base = attributes.get("href")
        elif tag in {"a", "area"} and attributes.get("href"):
            self.references.append((attributes["href"], False))
        elif tag == "link" and attributes.get("href"):
            relations = set(attributes.get("rel", "").lower().split())
            if relations & {"stylesheet", "icon", "shortcut"}:
                self.references.append((attributes["href"], True))
        elif tag in {"img", "source"} and attributes.get("src"):
            self.references.append((attributes["src"], True))


def _normalize_url(value, base=None):
    value = urljoin(base, value) if base else value
    value, _ = urldefrag(value)
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise DocBOMError(f"invalid URL: {value}") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise DocBOMError(f"only absolute HTTP(S) URLs are supported: {value}")
    if parts.username is not None or parts.password is not None:
        raise DocBOMError(f"URLs containing credentials are not supported: {value}")
    scheme = parts.scheme.lower()
    host = parts.hostname.encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    default_port = 80 if scheme == "http" else 443
    netloc = host if port is None or port == default_port else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _origin(url):
    parts = urlsplit(url)
    port = parts.port or (80 if parts.scheme == "http" else 443)
    return parts.scheme, parts.hostname, port


def _exclusion(url, profile, origins, is_asset):
    scope = profile["scope"]
    if is_asset and not scope["include_assets"]:
        return "assets_disabled", None
    if _origin(url) not in origins:
        return "cross_origin", None
    path = urlsplit(url).path
    if not any(path.startswith(prefix) for prefix in scope["allowed_paths"]):
        return "outside_allowed_paths", None
    for pattern in scope["excluded_patterns"]:
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(url, pattern):
            return "excluded_pattern", pattern
    return None, None


def _content_type(headers):
    for name, value in headers:
        if name.lower() == "content-type":
            return value
    return None


def _discover_references(body, content_type):
    current_media_type = media_type(content_type)
    if current_media_type in HTML_CONTENT_TYPES:
        parser = _HTMLReferenceParser()
        parser.feed(decode_body(body, content_type))
        parser.close()
        return parser.base, parser.references
    if current_media_type in CSS_CONTENT_TYPES:
        text = decode_body(body, content_type)
        urls = [match.group(2).strip() for match in CSS_URL_PATTERN.finditer(text)]
        urls.extend(match.group(2).strip() for match in CSS_IMPORT_PATTERN.finditer(text))
        return None, [(url, True) for url in urls if url]
    return None, []


def _clean_header_value(value):
    return re.sub(r"[\r\n]+", " ", str(value)).strip()


def _warc_record(url, captured_at, capture_id, status, reason, headers, body):
    status = int(status)
    reason = _clean_header_value(reason or "")
    http_lines = [f"HTTP/1.1 {status} {reason}\r\n".encode("utf-8")]
    for name, value in headers:
        if name.lower() in {"content-length", "transfer-encoding"}:
            continue
        http_lines.append(
            f"{_clean_header_value(name)}: {_clean_header_value(value)}\r\n".encode("utf-8")
        )
    http_lines.append(f"Content-Length: {len(body)}\r\n".encode("ascii"))
    http_payload = b"".join(http_lines) + b"\r\n" + body
    record_id = uuid.uuid5(uuid.NAMESPACE_URL, f"docbom:{capture_id}:{url}")
    warc_headers = (
        "WARC/1.0\r\n"
        "WARC-Type: response\r\n"
        f"WARC-Record-ID: <urn:uuid:{record_id}>\r\n"
        f"WARC-Target-URI: {url}\r\n"
        f"WARC-Date: {captured_at}\r\n"
        "Content-Type: application/http; msgtype=response\r\n"
        f"Content-Length: {len(http_payload)}\r\n"
        "\r\n"
    ).encode("utf-8")
    return warc_headers + http_payload + b"\r\n\r\n"


def _store_immutable(store, content, suffix):
    digest = sha256_bytes(content)
    relative = f"store/capture/{digest[:2]}/{digest[2:4]}/{digest}{suffix}"
    destination = Path(store).joinpath(*relative.split("/"))
    if destination.exists():
        if sha256_bytes(destination.read_bytes()) != digest:
            raise DocBOMError(f"hash-addressed file collision: {destination}")
    else:
        atomic_write(destination, content)
    return destination, relative, digest


def _default_capture_id(acquired_at, profile_id):
    timestamp = acquired_at.replace("-", "").replace(":", "")
    safe_profile = re.sub(r"[^A-Za-z0-9._-]+", "-", profile_id).strip("-.") or "web"
    return f"{timestamp}-{safe_profile}"


def _request(opener, url, timeout):
    request = Request(url, headers={"User-Agent": f"DocBOM/{__version__}"})
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        return None, str(exc)
    try:
        with response:
            body = response.read()
            return (response.status, response.reason, list(response.headers.items()), body), None
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        return None, str(exc)


def capture_web(profile_path, store, capture_id=None, acquired_at=None, timeout=10.0):
    store = Path(store)
    if not (store / "store" / "capture").is_dir() or not (store / "state").is_dir():
        raise DocBOMError(f"store is not initialized: {store}")
    if timeout <= 0:
        raise DocBOMError("timeout must be positive")
    profile_path = Path(profile_path)
    profile = load_yaml(profile_path)
    require_valid(profile, "capture-profile.schema.json", str(profile_path))
    if profile["access_context"]["authentication"] != "none":
        raise DocBOMError("static Web capture supports only Capture Profiles with authentication: none")

    captured_at = acquired_at or utc_now()
    parsed_time = parse_datetime(captured_at)
    captured_at = parsed_time.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    entrypoints = [_normalize_url(url) for url in profile["entrypoints"]]
    origins = {_origin(url) for url in entrypoints}
    for url in entrypoints:
        reason, pattern = _exclusion(url, profile, origins, False)
        if reason:
            detail = f" ({pattern})" if pattern else ""
            raise DocBOMError(f"entrypoint is excluded by the Capture Profile: {url}: {reason}{detail}")

    report_path = store / "state" / "pending"
    if capture_id is not None and not CAPTURE_ID_PATTERN.fullmatch(capture_id):
        raise DocBOMError("capture_id must contain only letters, digits, '.', '_' or '-'")
    base_id = capture_id or _default_capture_id(captured_at, profile["id"])
    for suffix_number in range(1, 1000):
        candidate = base_id if suffix_number == 1 else f"{base_id}-{suffix_number}"
        candidate_path = store / "state" / f"{candidate}.capture.yaml"
        if not candidate_path.exists():
            capture_id = candidate
            report_path = candidate_path
            break
        if capture_id is not None:
            raise DocBOMError(f"capture report already exists: {candidate_path}")
    else:
        raise DocBOMError("could not allocate a capture ID")

    queue = deque((url, False) for url in entrypoints)
    seen = set()
    events = []
    resources = []
    warc_records = []
    opener = build_opener(_NoRedirect)

    while queue:
        candidate, is_asset = queue.popleft()
        try:
            url = _normalize_url(candidate)
        except DocBOMError:
            events.append({"url": candidate, "outcome": "excluded", "reason": "invalid_or_unsupported_url"})
            continue
        if url in seen:
            continue
        seen.add(url)
        reason, pattern = _exclusion(url, profile, origins, is_asset)
        if reason:
            event = {"url": url, "outcome": "excluded", "reason": reason}
            if pattern is not None:
                event["pattern"] = pattern
            events.append(event)
            continue

        response, error = _request(opener, url, timeout)
        if error is not None:
            events.append({"url": url, "outcome": "failed", "error": error})
            continue
        status, reason_text, headers, body = response
        content_type = _content_type(headers)
        body_digest = sha256_bytes(body)
        resource = {
            "url": url,
            "status": status,
            "content_type": content_type,
            "sha256": body_digest,
        }
        normalized_digest = normalized_resource_digest(resource, body)
        if normalized_digest is not None:
            resource["normalized_content_sha256"] = normalized_digest
        resources.append(resource)
        warc_records.append(_warc_record(url, captured_at, capture_id, status, reason_text, headers, body))
        succeeded = 200 <= status < 300
        event = {
            "url": url,
            "outcome": "success" if succeeded else "failed",
            "status": status,
            "content_type": content_type,
            "sha256": body_digest,
        }
        if not succeeded:
            event["error"] = f"HTTP status {status}"
            if 300 <= status < 400:
                event["error"] += "; redirects are not followed by static Web capture"
        events.append(event)
        if succeeded:
            base, references = _discover_references(body, content_type)
            reference_base = urljoin(url, base) if base else url
            queue.extend((urljoin(reference_base, reference), asset) for reference, asset in references)

    failed = sum(event["outcome"] == "failed" for event in events)
    succeeded = sum(event["outcome"] == "success" for event in events)
    excluded = sum(event["outcome"] == "excluded" for event in events)
    warc_path, warc_storage_path, warc_digest = _store_immutable(store, b"".join(warc_records), ".warc")
    resource_content = (json.dumps(resources, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    resource_path, resource_storage_path, resource_digest = _store_immutable(
        store, resource_content, ".resources.json"
    )
    completeness = "partial" if failed else "complete_within_declared_scope"
    normalized_digest = aggregate_normalized_digest(resources)
    artifact = {
        "role": "capture",
        "kind": "web_snapshot",
        "capture_id": capture_id,
        "capture_profile": f"{profile['id']}/v{profile['version']}",
        "acquired_at": captured_at,
        "storage_path": warc_storage_path,
        "sha256": warc_digest,
        "resource_manifest_path": resource_storage_path,
        "resource_manifest_sha256": resource_digest,
        "completeness": completeness,
    }
    if normalized_digest is not None:
        artifact["normalized_content_sha256"] = normalized_digest
    report = {
        "capture_id": capture_id,
        "capture_profile": artifact["capture_profile"],
        "profile": profile,
        "normalization_algorithm": NORMALIZATION_ALGORITHM,
        "acquired_at": captured_at,
        "entrypoints": entrypoints,
        "summary": {
            "fetched": len(resources),
            "succeeded": succeeded,
            "failed": failed,
            "excluded": excluded,
        },
        "artifact": artifact,
        "events": events,
    }
    if report_path.exists():
        raise DocBOMError(f"capture report already exists: {report_path}")
    import yaml

    atomic_write(report_path, yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return {
        "capture_id": capture_id,
        "warc": warc_path,
        "resources": resource_path,
        "report": report_path,
        "complete": not failed,
        "artifact": artifact,
    }
