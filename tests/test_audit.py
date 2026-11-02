"""Offline tests: parsers on HTML strings and a full run against a local HTTP server.

Run: python -m unittest discover -s tests
"""
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requests  # noqa: E402

import bought_backlink_audit as audit  # noqa: E402


class ParserTests(unittest.TestCase):
    def test_meta_robots_and_googlebot(self):
        p = audit.PageParser("example.com")
        p.feed('<html><head><META NAME="Robots" content="index,follow">'
               '<meta name="googlebot" content="noindex">'
               '<link rel="canonical" href="/other/"></head>'
               '<body><a href="https://example.com/x" rel="nofollow ugc">a</a>'
               '<a href="https://www.example.com/">b</a></body></html>')
        self.assertEqual(p.meta, ["index,follow", "noindex"])
        self.assertTrue(audit.is_noindex(audit.robots_directives(p.meta[1])))
        self.assertEqual(p.canonical, "/other/")
        self.assertEqual(sorted(p.rels), ["(none)", "nofollow ugc"])

    def test_x_robots_tag_scopes(self):
        d = audit.robots_directives
        self.assertTrue(audit.is_noindex(d("noindex, nofollow")))
        self.assertTrue(audit.is_noindex(d("none")))
        self.assertTrue(audit.is_noindex(d("googlebot: noindex")))
        self.assertFalse(audit.is_noindex(d("bingbot: noindex")))
        self.assertFalse(audit.is_noindex(d("bingbot: noindex, googlebot: nofollow")))
        self.assertTrue(audit.is_noindex(d("otherbot: nofollow, googlebot: noindex, nosnippet")))
        self.assertFalse(audit.is_noindex(d("unavailable_after: 25 Jun 2030 15:00:00 PST")))
        self.assertFalse(audit.is_noindex(d("max-snippet:-1, max-image-preview:large")))

    def test_canonical_and_redirect_helpers(self):
        self.assertEqual(audit.norm("HTTPS://Example.com:443/a?b=1"), audit.norm("https://example.com/a?b=1"))
        self.assertNotEqual(audit.norm("https://example.com/a/"), audit.norm("https://example.com/a"))
        self.assertTrue(audit.is_homepage_redirect("https://site.com/post/1", "/"))
        self.assertTrue(audit.is_homepage_redirect("https://site.com/post/1", "https://site.com"))
        self.assertFalse(audit.is_homepage_redirect("https://site.com/post/1", "/post/1/"))
        self.assertFalse(audit.is_homepage_redirect("http://site.com/", "https://site.com/"))


PAGES = {
    "/ok": (200, {}, '<html><head><link rel="canonical" href="/ok"></head>'
                     '<a href="https://money.example/" rel="sponsored">x</a></html>'),
    "/meta-noindex": (200, {}, '<meta name="robots" content="noindex,follow">'),
    "/header-noindex": (200, {"X-Robots-Tag": "googlebot: noindex"}, "<p>hi</p>"),
    "/other-bot-noindex": (200, {"X-Robots-Tag": "bingbot: noindex"}, "<p>hi</p>"),
    "/canonical": (200, {}, '<link rel="canonical" href="https://elsewhere.example/page">'),
    "/private/page": (200, {}, "<p>blocked by robots.txt</p>"),
    "/moved": (301, {"Location": "/new-place"}, ""),
    "/to-home": (302, {"Location": "/"}, ""),
    "/gone": (410, {}, ""),
    "/forbidden": (403, {}, "no"),
    "/challenge": (403, {"Server": "cloudflare", "cf-mitigated": "challenge"}, "Just a moment..."),
    "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /private/\n"),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, headers, body = PAGES.get(self.path, (404, {}, "not found"))
        self.send_response(status)
        headers.setdefault("Content-Type", "text/html; charset=utf-8")
        for k, v in headers.items():
            self.send_header(k, v)
        data = body.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.session = requests.Session()
        cls.session.trust_env = False
        cls.session.headers["User-Agent"] = audit.UA

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_one(self, path, target=""):
        return audit.check({"url": self.base + path, "target": target}, self.session, 5)

    def test_verdicts(self):
        expected = {
            "/ok": ("live_open", ""),
            "/meta-noindex": ("blocked", "meta_noindex"),
            "/header-noindex": ("blocked", "x_robots_tag_noindex"),
            "/other-bot-noindex": ("live_open", ""),
            "/canonical": ("blocked", "canonical_elsewhere"),
            "/private/page": ("blocked", "robots_txt_disallow"),
            "/moved": ("redirect", "http_301"),
            "/to-home": ("dead", "redirect_to_homepage"),
            "/missing": ("dead", "http_404"),
            "/gone": ("dead", "http_410"),
            "/forbidden": ("error", "http_403"),
            "/challenge": ("cloudflare_challenge", "unverified"),
        }
        for path, (verdict, reasons) in expected.items():
            with self.subTest(path=path):
                r = self.run_one(path)
                self.assertEqual((r["verdict"], r["reasons"]), (verdict, reasons))

    def test_link_to_target(self):
        r = self.run_one("/ok", target="money.example")
        self.assertEqual((r["link_found"], r["link_rel"]), ("yes", "sponsored"))
        r = self.run_one("/meta-noindex", target="money.example")
        self.assertEqual(r["link_found"], "no")

    def test_dns_failure_is_dead(self):
        r = audit.check({"url": "http://no-such-host.invalid/page"}, self.session, 5)
        self.assertEqual((r["verdict"], r["reasons"]), ("dead", "dns_error"))


if __name__ == "__main__":
    unittest.main()
