import json
import re
from html.parser import HTMLParser

from .util import sha256_bytes


HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
NORMALIZATION_ALGORITHM = "html-visible-text-v1"
_SUPPRESSED_ELEMENTS = {"script", "style", "noscript", "template"}
_BLOCK_ELEMENTS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "td", "th", "title", "tr", "ul",
}


def media_type(content_type):
    return (content_type or "").split(";", 1)[0].strip().lower()


def decode_body(body, content_type):
    match = re.search(r"charset\s*=\s*['\"]?([^;\s'\"]+)", content_type or "", re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SUPPRESSED_ELEMENTS:
            self.suppressed_depth += 1
        elif not self.suppressed_depth and tag in _BLOCK_ELEMENTS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if not self.suppressed_depth and tag in _BLOCK_ELEMENTS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SUPPRESSED_ELEMENTS and self.suppressed_depth:
            self.suppressed_depth -= 1
        elif not self.suppressed_depth and tag in _BLOCK_ELEMENTS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppressed_depth:
            self.parts.append(data)


def normalize_html_text(body, content_type):
    parser = _VisibleTextParser()
    parser.feed(decode_body(body, content_type))
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def normalized_resource_digest(resource, body):
    if not 200 <= int(resource["status"]) < 300:
        return None
    if media_type(resource.get("content_type")) not in HTML_CONTENT_TYPES:
        return None
    text = normalize_html_text(body, resource.get("content_type"))
    return sha256_bytes(text.encode("utf-8"))


def aggregate_normalized_digest(resources):
    pages = [
        {"url": resource["url"], "sha256": resource["normalized_content_sha256"]}
        for resource in resources
        if resource.get("normalized_content_sha256") is not None
    ]
    if not pages:
        return None
    pages.sort(key=lambda page: page["url"])
    content = json.dumps(pages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(content)
