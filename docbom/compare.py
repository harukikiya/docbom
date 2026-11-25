import json
import re
from datetime import timezone
from pathlib import Path

import yaml

from . import __version__
from .capture import CAPTURE_ID_PATTERN
from .util import (
    DocBOMError,
    atomic_write,
    load_yaml,
    parse_datetime,
    path_in_store,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from .webtext import NORMALIZATION_ALGORITHM, aggregate_normalized_digest, normalized_resource_digest


STATE_FIELDS = (
    "outcome",
    "status",
    "content_type",
    "sha256",
    "normalized_content_sha256",
    "reason",
    "pattern",
    "error",
)


def _parse_headers(block):
    lines = block.decode("utf-8").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, value = line.split(":", 1)
        headers[name.lower()] = value.strip()
    return lines[0], headers


def _read_warc(content):
    records = []
    offset = 0
    try:
        while offset < len(content):
            if not content.startswith(b"WARC/1.0\r\n", offset):
                raise ValueError("expected WARC/1.0 record with CRLF headers")
            header_end = content.index(b"\r\n\r\n", offset)
            _, warc_headers = _parse_headers(content[offset:header_end])
            if warc_headers.get("warc-type") != "response":
                raise ValueError("only WARC response records are supported")
            payload_start = header_end + 4
            payload_length = int(warc_headers["content-length"])
            payload_end = payload_start + payload_length
            if payload_end + 4 > len(content):
                raise ValueError("truncated WARC payload")
            payload = content[payload_start:payload_end]
            http_header_end = payload.index(b"\r\n\r\n")
            status_line, http_headers = _parse_headers(payload[:http_header_end])
            status_match = re.fullmatch(r"HTTP/[0-9.]+ ([0-9]{3})(?: .*)?", status_line)
            if not status_match:
                raise ValueError(f"invalid HTTP status line: {status_line}")
            body = payload[http_header_end + 4:]
            if int(http_headers["content-length"]) != len(body):
                raise ValueError("HTTP Content-Length differs from the body length")
            if content[payload_end:payload_end + 4] != b"\r\n\r\n":
                raise ValueError("missing WARC record separator")
            records.append(
                {
                    "url": warc_headers["warc-target-uri"],
                    "status": int(status_match.group(1)),
                    "content_type": http_headers.get("content-type"),
                    "sha256": sha256_bytes(body),
                    "body": body,
                }
            )
            offset = payload_end + 4
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise DocBOMError(f"invalid DocBOM WARC: {exc}") from exc
    return records


def _load_json_resources(path):
    try:
        resources = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DocBOMError(f"invalid resource manifest: {path}: {exc}") from exc
    if not isinstance(resources, list):
        raise DocBOMError(f"resource manifest must be a JSON array: {path}")
    return resources


def _load_capture(store, capture_id):
    if not CAPTURE_ID_PATTERN.fullmatch(capture_id):
        raise DocBOMError("capture IDs must contain only letters, digits, '.', '_' or '-'")
    store = Path(store)
    report_path = store / "state" / f"{capture_id}.capture.yaml"
    report = load_yaml(report_path)
    if report.get("capture_id") != capture_id:
        raise DocBOMError(f"capture ID mismatch: {report_path}")
    algorithm = report.get("normalization_algorithm")
    if algorithm is not None and algorithm != NORMALIZATION_ALGORITHM:
        raise DocBOMError(f"unsupported normalization algorithm in {report_path}: {algorithm}")
    artifact = report.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("kind") != "web_snapshot":
        raise DocBOMError(f"capture report has no Web artifact: {report_path}")

    warc_path = path_in_store(store, artifact["storage_path"])
    resource_path = path_in_store(store, artifact["resource_manifest_path"])
    if not warc_path.is_file() or sha256_file(warc_path) != artifact["sha256"]:
        raise DocBOMError(f"capture WARC hash verification failed: {capture_id}")
    if not resource_path.is_file() or sha256_file(resource_path) != artifact["resource_manifest_sha256"]:
        raise DocBOMError(f"capture resource manifest hash verification failed: {capture_id}")

    resources = _load_json_resources(resource_path)
    warc_records = _read_warc(warc_path.read_bytes())
    records_by_url = {}
    for record in warc_records:
        if record["url"] in records_by_url:
            raise DocBOMError(f"duplicate WARC URL in capture {capture_id}: {record['url']}")
        records_by_url[record["url"]] = record

    normalized_resources = []
    resource_urls = set()
    for resource in resources:
        if not isinstance(resource, dict) or not {"url", "status", "content_type", "sha256"} <= set(resource):
            raise DocBOMError(f"invalid resource entry in capture {capture_id}")
        url = resource["url"]
        if url in resource_urls:
            raise DocBOMError(f"duplicate resource URL in capture {capture_id}: {url}")
        resource_urls.add(url)
        record = records_by_url.get(url)
        if record is None:
            raise DocBOMError(f"resource has no WARC response in capture {capture_id}: {url}")
        for field in ("status", "content_type", "sha256"):
            if resource[field] != record[field]:
                raise DocBOMError(f"resource/WARC {field} mismatch in capture {capture_id}: {url}")
        normalized = normalized_resource_digest(resource, record["body"])
        recorded = resource.get("normalized_content_sha256")
        if recorded is not None and recorded != normalized:
            raise DocBOMError(f"normalized content hash mismatch in capture {capture_id}: {url}")
        resource = dict(resource)
        if normalized is not None:
            resource["normalized_content_sha256"] = normalized
        normalized_resources.append(resource)
    if resource_urls != set(records_by_url):
        raise DocBOMError(f"resource manifest and WARC URL sets differ: {capture_id}")

    normalized_digest = aggregate_normalized_digest(normalized_resources)
    recorded_digest = artifact.get("normalized_content_sha256")
    if recorded_digest is not None and recorded_digest != normalized_digest:
        raise DocBOMError(f"aggregate normalized content hash mismatch: {capture_id}")

    states = {}
    for resource in normalized_resources:
        states[resource["url"]] = {
            field: resource[field]
            for field in STATE_FIELDS
            if field in resource
        }
        states[resource["url"]]["outcome"] = "success" if 200 <= resource["status"] < 300 else "failed"
    events = report.get("events", [])
    if not isinstance(events, list):
        raise DocBOMError(f"capture events must be an array: {report_path}")
    for event in events:
        if not isinstance(event, dict) or "url" not in event or "outcome" not in event:
            raise DocBOMError(f"invalid capture event: {report_path}")
        url = event["url"]
        state = states.get(url)
        if state is None:
            state = {}
            states[url] = state
        for field in STATE_FIELDS:
            if field in event:
                state[field] = event[field]
    return {
        "report": report,
        "artifact": artifact,
        "resources": normalized_resources,
        "states": states,
        "normalized_content_sha256": normalized_digest,
    }


def _capture_summary(capture):
    report = capture["report"]
    artifact = capture["artifact"]
    return {
        "capture_id": report["capture_id"],
        "capture_profile": report["capture_profile"],
        "acquired_at": report["acquired_at"],
        "completeness": artifact["completeness"],
        "warc_sha256": artifact["sha256"],
        "resource_manifest_sha256": artifact["resource_manifest_sha256"],
        "normalized_content_sha256": capture["normalized_content_sha256"],
    }


def _check_comparability(previous, candidate):
    previous_report = previous["report"]
    candidate_report = candidate["report"]
    if previous_report["capture_profile"] != candidate_report["capture_profile"]:
        raise DocBOMError("cannot compare captures with different Capture Profile IDs or versions")
    if previous_report.get("entrypoints") != candidate_report.get("entrypoints"):
        raise DocBOMError("cannot compare captures with different entrypoints")
    if "profile" in previous_report and "profile" in candidate_report:
        if previous_report["profile"] != candidate_report["profile"]:
            raise DocBOMError("Capture Profile content differs; use a consistent versioned profile")
        return "exact", []
    return "legacy_id_and_entrypoints", [
        "At least one capture report does not embed the full Capture Profile; only its ID, version, and entrypoints were compared."
    ]


def _state_changed(previous, candidate):
    return [field for field in STATE_FIELDS if previous.get(field) != candidate.get(field)]


def compare_web_captures(store, previous_id, candidate_id, output=None, compared_at=None):
    if previous_id == candidate_id:
        raise DocBOMError("previous and candidate captures must be different")
    store = Path(store)
    previous = _load_capture(store, previous_id)
    candidate = _load_capture(store, candidate_id)
    profile_comparison, warnings = _check_comparability(previous, candidate)

    compared_at = compared_at or utc_now()
    compared_at = parse_datetime(compared_at).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    previous_states = previous["states"]
    candidate_states = candidate["states"]
    changes = []
    unchanged_urls = []
    for url in sorted(set(previous_states) | set(candidate_states)):
        before = previous_states.get(url)
        after = candidate_states.get(url)
        if before is None:
            changes.append({"url": url, "change": "added", "candidate": after})
        elif after is None:
            changes.append({"url": url, "change": "removed", "previous": before})
        else:
            fields = _state_changed(before, after)
            if fields:
                changes.append(
                    {"url": url, "change": "changed", "fields": fields, "previous": before, "candidate": after}
                )
            else:
                unchanged_urls.append(url)

    def response_urls(capture):
        return {url for url, state in capture["states"].items() if "sha256" in state}

    def failed_urls(capture):
        return {url for url, state in capture["states"].items() if state.get("outcome") == "failed"}

    resource_bytes_changed = any(
        (
            change["change"] in {"added", "removed"}
            and "sha256" in (change.get("previous") or change.get("candidate") or {})
        )
        or (change["change"] == "changed" and "sha256" in change["fields"])
        for change in changes
    )
    failures_changed = failed_urls(previous) != failed_urls(candidate) or any(
        change["change"] == "changed"
        and "failed" in {change["previous"].get("outcome"), change["candidate"].get("outcome")}
        for change in changes
    )
    normalized_changed = previous["normalized_content_sha256"] != candidate["normalized_content_sha256"]
    result = "changed" if changes else "unchanged"
    report = {
        "comparison_version": 1,
        "comparison_tool": f"docbom/{__version__}",
        "compared_at": compared_at,
        "previous": _capture_summary(previous),
        "candidate": _capture_summary(candidate),
        "profile_comparison": profile_comparison,
        "normalization_algorithm": NORMALIZATION_ALGORITHM,
        "result": result,
        "review_status": "pending" if changes else "not_required",
        "review_cause": None,
        "summary": {
            "added": sum(change["change"] == "added" for change in changes),
            "removed": sum(change["change"] == "removed" for change in changes),
            "changed": sum(change["change"] == "changed" for change in changes),
            "unchanged": len(unchanged_urls),
            "resource_set_changed": response_urls(previous) != response_urls(candidate),
            "resource_bytes_changed": resource_bytes_changed,
            "normalized_content_changed": normalized_changed,
            "failures_changed": failures_changed,
            "completeness_changed": previous["artifact"]["completeness"] != candidate["artifact"]["completeness"],
        },
        "changes": changes,
        "unchanged_urls": unchanged_urls,
        "warnings": warnings,
        "interpretation": "WARC hashes identify capture events and are not used as update evidence by themselves. Review whether changes are acquisition nondeterminism, acquisition gaps, or official content updates. Normalized text equality does not establish whole-document equality.",
    }
    output_path = Path(output) if output else store / "state" / f"{candidate_id}.diff.yaml"
    if output_path.exists():
        raise DocBOMError(f"comparison report already exists: {output_path}")
    atomic_write(output_path, yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return {"report": output_path, "result": result, "data": report}
