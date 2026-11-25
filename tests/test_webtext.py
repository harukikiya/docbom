import unittest

from docbom.webtext import media_type, normalize_html_text, normalized_resource_digest


class VisibleTextNormalizationTests(unittest.TestCase):
    def test_suppressed_elements_and_whitespace_are_normalized(self):
        body = b"""<!doctype html>
        <html><head><title> ACME   manual </title>
        <style>body { display: none; }</style>
        <script>document.write('script text')</script></head>
        <body><h1>Timer</h1><p>A\n B &amp; C</p>
        <noscript>fallback</noscript><template>template text</template></body></html>
        """
        self.assertEqual("ACME manual Timer A B & C", normalize_html_text(body, "text/html; charset=utf-8"))

    def test_normalized_digest_is_limited_to_successful_html(self):
        html = b"<style>ignored</style><h1>Same</h1><script>ignored</script>"
        resource = {"status": 200, "content_type": "Text/HTML; charset=UTF-8"}
        self.assertEqual("text/html", media_type(resource["content_type"]))
        digest = normalized_resource_digest(resource, html)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            digest,
            normalized_resource_digest(resource, b"<h1>Same</h1><noscript>different</noscript>"),
        )
        self.assertIsNone(normalized_resource_digest({"status": 404, "content_type": "text/html"}, html))
        self.assertIsNone(normalized_resource_digest({"status": 200, "content_type": "image/png"}, html))


if __name__ == "__main__":
    unittest.main()
