import argparse
import sys
from pathlib import Path

from . import __version__
from .capture import capture_web
from .catalog import Catalog, lock_file
from .compare import compare_web_captures
from .ingest import ingest_git, ingest_pdf, init_project, init_store
from .util import DocBOMError, atomic_write, write_yaml


DOCUMENT_TYPES = (
    "datasheet",
    "reference-manual",
    "errata",
    "manual",
    "app-note",
    "release-note",
    "guide",
)


def _add_ingest_metadata(parser):
    parser.add_argument("--store", required=True, help="document store directory")
    parser.add_argument("--id", dest="identifier", required=True, help="document ID")
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--revision", required=True, help="official or Git document revision")
    parser.add_argument("--title")
    parser.add_argument("--document-type", choices=DOCUMENT_TYPES, default="manual")
    parser.add_argument("--document-number")
    parser.add_argument("--subject", help="subject ID")
    parser.add_argument("--published", help="publication date (YYYY-MM-DD)")
    parser.add_argument("--acquired-at", help="acquisition timestamp; defaults to current UTC time")


def build_parser():
    parser = argparse.ArgumentParser(prog="docbom", description="External technical documentation lockfile tool")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    init = commands.add_parser("init", help="initialize a document store or project")
    init_commands = init.add_subparsers(dest="init_command", required=True)
    init_store_parser = init_commands.add_parser("store")
    init_store_parser.add_argument("path")
    init_project_parser = init_commands.add_parser("project")
    init_project_parser.add_argument("path")
    init_project_parser.add_argument("--subject", required=True)
    init_project_parser.add_argument("--silicon-rev")
    init_project_parser.add_argument("--sdk")

    ingest = commands.add_parser("ingest", help="ingest a PDF or Git document")
    ingest_commands = ingest.add_subparsers(dest="ingest_command", required=True)
    pdf = ingest_commands.add_parser("pdf")
    pdf.add_argument("source")
    pdf.add_argument("--source-url")
    _add_ingest_metadata(pdf)
    git = ingest_commands.add_parser("git")
    git.add_argument("repository", help="Git repository URL")
    git.add_argument("--commit", required=True)
    git.add_argument("--path", dest="source_path", required=True)
    git.add_argument("--tag")
    _add_ingest_metadata(git)

    capture = commands.add_parser("capture", help="capture static Web documentation")
    capture_commands = capture.add_subparsers(dest="capture_command", required=True)
    web = capture_commands.add_parser("web", help="capture static HTML, CSS, and images")
    web.add_argument("profile", help="Capture Profile YAML path")
    web.add_argument("--store", required=True, help="document store directory")
    web.add_argument("--capture-id", help="unique capture ID; generated from the timestamp by default")
    web.add_argument("--acquired-at", help="capture timestamp; defaults to current UTC time")
    web.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")

    compare = commands.add_parser("compare", help="compare two captures without changing document selections")
    compare_commands = compare.add_subparsers(dest="compare_command", required=True)
    web_compare = compare_commands.add_parser("web", help="compare static Web capture responses and failures")
    web_compare.add_argument("--store", required=True, help="document store directory")
    web_compare.add_argument("--previous", required=True, help="earlier capture ID")
    web_compare.add_argument("--candidate", required=True, help="new capture ID")
    web_compare.add_argument("--output", help="new comparison report path; defaults to state/CANDIDATE.diff.yaml")
    web_compare.add_argument("--compared-at", help="comparison timestamp; defaults to current UTC time")

    validate = commands.add_parser("validate", help="validate a store, project, and lock")
    validate.add_argument("--store", required=True)
    validate.add_argument("--project")
    validate.add_argument("--lock")

    resolve = commands.add_parser("resolve", help="resolve project requirements into a lockfile")
    resolve.add_argument("--store", required=True)
    resolve.add_argument("--project", required=True)
    resolve.add_argument("--output")
    resolve.add_argument("--catalog-revision", help="defaults to the store's Git HEAD")
    resolve.add_argument("--generated-at", help="defaults to current UTC time")

    get = commands.add_parser("get", help="retrieve an artifact locked by a project")
    get.add_argument("identifier", help="document ID")
    get.add_argument("--store", required=True)
    get.add_argument("--lock", required=True)
    get.add_argument("--revision")
    get.add_argument("--sha256")
    get.add_argument("--text", action="store_true", help="print extracted text")
    get.add_argument("--page", type=int, help="1-based PDF page; requires --text")
    get.add_argument("--lines", help="1-based inclusive line range, for example 2:5")
    get.add_argument("--output", help="copy the locked artifact to this new file")
    return parser


def _extract_text(artifact, path, page=None, lines=None):
    if artifact["kind"] == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if page is not None:
            if page < 1 or page > len(reader.pages):
                raise DocBOMError(f"PDF page out of range: {page} (1..{len(reader.pages)})")
            text = reader.pages[page - 1].extract_text() or ""
        else:
            text = "\n\n".join((item.extract_text() or "") for item in reader.pages)
    elif artifact["kind"] in {"git_file", "file"}:
        if page is not None:
            raise DocBOMError("--page is only valid for PDF artifacts")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise DocBOMError(f"artifact is not UTF-8 text: {path}") from exc
    else:
        raise DocBOMError(f"text extraction is not supported for {artifact['kind']} artifacts")

    if lines is not None:
        try:
            start_text, end_text = lines.split(":", 1)
            start, end = int(start_text), int(end_text)
        except ValueError as exc:
            raise DocBOMError("--lines must have the form START:END") from exc
        if start < 1 or end < start:
            raise DocBOMError("--lines must be a positive, ascending range")
        text = "\n".join(text.splitlines()[start - 1:end])
        if text:
            text += "\n"
    return text


def _run(args):
    if args.command == "init":
        if args.init_command == "store":
            print(init_store(args.path))
        else:
            print(init_project(args.path, args.subject, args.silicon_rev, args.sdk))
        return 0

    if args.command == "ingest":
        common = {
            "store": args.store,
            "identifier": args.identifier,
            "publisher": args.publisher,
            "revision": args.revision,
            "title": args.title,
            "document_type": args.document_type,
            "document_number": args.document_number,
            "subject": args.subject,
            "published": args.published,
            "acquired_at": args.acquired_at,
        }
        if args.ingest_command == "pdf":
            result = ingest_pdf(args.source, source_url=args.source_url, **common)
        else:
            result = ingest_git(
                args.repository,
                args.commit,
                args.source_path,
                tag=args.tag,
                **common,
            )
        print(f"manifest: {result['manifest']}")
        print(f"artifact: {result['artifact']}")
        print(f"sha256: {result['sha256']}")
        return 0

    if args.command == "capture":
        result = capture_web(
            args.profile,
            args.store,
            capture_id=args.capture_id,
            acquired_at=args.acquired_at,
            timeout=args.timeout,
        )
        print(f"capture_id: {result['capture_id']}")
        print(f"warc: {result['warc']}")
        print(f"resources: {result['resources']}")
        print(f"report: {result['report']}")
        print(f"status: {'complete' if result['complete'] else 'partial'}")
        if not result["complete"]:
            print("error: Web capture has failed resources; see the capture report", file=sys.stderr)
            return 1
        return 0

    if args.command == "compare":
        result = compare_web_captures(
            args.store,
            args.previous,
            args.candidate,
            output=args.output,
            compared_at=args.compared_at,
        )
        print(f"report: {result['report']}")
        print(f"result: {result['result']}")
        print(f"changes: {len(result['data']['changes'])}")
        for warning in result["data"]["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        return 0

    if args.command == "validate":
        catalog = Catalog(args.store)
        counts = catalog.validate()
        if args.project:
            catalog.validate_project(args.project)
        if args.lock:
            catalog.validate_lock(args.lock)
        print(
            "OK: "
            f"{counts['subjects']} subjects, "
            f"{counts['documents']} documents, "
            f"{counts['capture_profiles']} capture profiles"
        )
        return 0

    if args.command == "resolve":
        catalog = Catalog(args.store)
        lock = catalog.resolve(args.project, args.catalog_revision, args.generated_at)
        output = Path(args.output) if args.output else lock_file(args.project)
        write_yaml(output, lock)
        print(output)
        return 0

    if args.command == "get":
        if args.output and args.text:
            raise DocBOMError("--output and --text cannot be used together")
        if (args.page is not None or args.lines is not None) and not args.text:
            raise DocBOMError("--page and --lines require --text")
        catalog = Catalog(args.store)
        artifact, path = catalog.find_locked_artifact(
            args.lock,
            args.identifier,
            revision=args.revision,
            sha256=args.sha256,
        )
        if args.output:
            output = Path(args.output)
            if output.exists():
                raise DocBOMError(f"output already exists: {output}")
            atomic_write(output, path.read_bytes())
            print(output)
        elif args.text:
            text = _extract_text(artifact, path, args.page, args.lines)
            print(text, end="" if text.endswith("\n") else "\n")
        else:
            print(path)
        return 0

    raise DocBOMError(f"unknown command: {args.command}")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        return _run(args)
    except DocBOMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
