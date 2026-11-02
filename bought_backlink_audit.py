#!/usr/bin/env python3
"""Audit a list of bought backlinks: is each page still live and open to indexing?

For every URL in the input CSV the script sends ONE request with the
Googlebot Smartphone user agent, does NOT follow redirects, and records:

  - the HTTP status and the Location header (redirects, redirects to the homepage);
  - DNS, SSL, timeout and connection failures;
  - a Cloudflare challenge (reported as unverified, not as dead);
  - noindex in <meta name="robots"> / <meta name="googlebot"> and in X-Robots-Tag;
  - a robots.txt Disallow for Googlebot (urllib.robotparser);
  - rel="canonical" (HTML tag or Link header) pointing to another URL;
  - optionally, whether the page still links to your site and with which rel.

It does not tell you whether a page is in Google's index: check that separately.

Usage:
    pip install requests
    python bought_backlink_audit.py links.csv report.csv [--workers 8] [--timeout 20]

Input CSV: a `url` column (required) and an optional `target` column with the
domain or URL the bought link should point to (e.g. example.com).
"""
import argparse
import csv
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from threading import Lock
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

UA = ("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.6778.139 Mobile Safari/537.36 "
      "(compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
MAX_BODY = 2_000_000  # bytes of HTML to read per page
FIELDS = ["url", "verdict", "reasons", "status", "location", "x_robots_tag",
          "meta_robots", "canonical", "robots_txt", "link_found", "link_rel", "error"]
DIRECTIVES = {"all", "index", "follow", "noindex", "nofollow", "none", "nosnippet",
              "noarchive", "nocache", "notranslate", "noimageindex", "indexifembedded",
              "unavailable_after", "max-snippet", "max-image-preview", "max-video-preview"}
DNS_ERRORS = ("NameResolutionError", "Name or service not known", "nodename nor servname",
              "getaddrinfo failed", "No address associated", "Temporary failure in name resolution")


def robots_directives(value, bot="googlebot"):
    """Directives from a robots meta `content` or one X-Robots-Tag value that apply to `bot`.

    Handles user-agent scoped values such as "googlebot: noindex" and
    "bingbot: noindex, googlebot: nofollow".
    """
    found, scope = set(), None
    for part in value.split(","):
        part = part.strip().lower()
        head, sep, rest = part.partition(":")
        if sep and head.strip() not in DIRECTIVES:  # a user-agent prefix
            scope, part = head.strip(), rest.strip()
        if part and scope in (None, bot):
            found.add(part.split(":")[0].strip())
    return found


def is_noindex(directives):
    return bool(directives & {"noindex", "none"})


class PageParser(HTMLParser):
    """Collects robots meta tags, the first rel=canonical and links to `target`."""

    def __init__(self, target=""):
        super().__init__(convert_charrefs=True)
        self.meta, self.canonical, self.rels = [], "", []
        self.target = target.lower()

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        rel = a.get("rel", "").lower().split()
        if tag == "meta" and a.get("name", "").lower() in ("robots", "googlebot"):
            self.meta.append(a.get("content", ""))
        elif tag == "link" and "canonical" in rel and not self.canonical:
            self.canonical = a.get("href", "").strip()
        elif tag == "a" and self.target and self.target in a.get("href", "").lower():
            self.rels.append(" ".join(rel) or "(none)")


def norm(url):
    p = urlsplit(url)
    host = p.netloc.lower()
    for port in (":443", ":80"):
        host = host.removesuffix(port)
    return p.scheme.lower(), host, p.path or "/", p.query


def is_homepage_redirect(url, location):
    dest = urlsplit(urljoin(url, location))
    return dest.path in ("", "/") and not dest.query and urlsplit(url).path not in ("", "/")


def is_cloudflare_challenge(resp, body):
    if resp.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    return (resp.status_code in (403, 503)
            and "cloudflare" in resp.headers.get("server", "").lower()
            and ("challenge-platform" in body or "Just a moment" in body))


_robots, _robots_lock = {}, Lock()


def robots_txt(url, session, timeout):
    """'allowed', 'disallowed' or 'unknown' (robots.txt unreachable or 5xx) for Googlebot."""
    p = urlsplit(url)
    base = f"{p.scheme}://{p.netloc}"
    with _robots_lock:
        parser = _robots.get(base)
    if parser is None:
        try:
            r = session.get(base + "/robots.txt", timeout=timeout)
            if r.status_code >= 500:
                parser = "unknown"
            else:  # a 4xx means "no robots.txt": everything is allowed
                parser = RobotFileParser()
                parser.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            parser = "unknown"
        with _robots_lock:
            _robots[base] = parser
    if parser == "unknown":
        return "unknown"
    return "allowed" if parser.can_fetch("Googlebot", url) else "disallowed"


def check(row, session, timeout):
    url, target = row["url"].strip(), (row.get("target") or "").strip()
    out = dict.fromkeys(FIELDS, "")
    out["url"] = url

    def done(verdict, reasons="", error=""):
        out.update(verdict=verdict, reasons=reasons, error=str(error)[:300])
        return out

    try:
        resp = session.get(url, timeout=timeout, allow_redirects=False, stream=True)
    except requests.exceptions.SSLError as exc:
        return done("error", "ssl_error", exc)
    except requests.exceptions.Timeout as exc:
        return done("error", "timeout", exc)
    except requests.exceptions.ConnectionError as exc:
        if any(s in str(exc) for s in DNS_ERRORS):
            return done("dead", "dns_error", exc)
        return done("error", "connection_error", exc)
    except requests.RequestException as exc:
        return done("error", "request_error", exc)

    status = resp.status_code
    out["status"] = status
    raw = b""
    if status in (200, 403, 503):
        try:
            for chunk in resp.iter_content(65536):
                raw += chunk
                if len(raw) > MAX_BODY:
                    break
        except requests.RequestException as exc:
            resp.close()
            return done("error", "read_error", exc)
    resp.close()
    try:
        body = raw.decode(resp.encoding or "utf-8", errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")

    if is_cloudflare_challenge(resp, body):
        return done("cloudflare_challenge", "unverified")
    if 300 <= status < 400:
        out["location"] = resp.headers.get("location", "")
        if is_homepage_redirect(url, out["location"]):
            return done("dead", "redirect_to_homepage")
        return done("redirect", f"http_{status}")
    if status in (404, 410):
        return done("dead", f"http_{status}")
    if status != 200:
        return done("error", f"http_{status}")

    reasons = []
    header_values = resp.raw.headers.getlist("X-Robots-Tag")  # one entry per header line
    out["x_robots_tag"] = " | ".join(header_values)
    if any(is_noindex(robots_directives(v)) for v in header_values):
        reasons.append("x_robots_tag_noindex")

    page = PageParser(target)
    try:
        page.feed(body)
    except Exception:  # broken markup: keep what was parsed so far
        pass
    out["meta_robots"] = " | ".join(page.meta)
    if any(is_noindex(robots_directives(v)) for v in page.meta):
        reasons.append("meta_noindex")

    out["robots_txt"] = robots_txt(url, session, timeout)
    if out["robots_txt"] == "disallowed":
        reasons.append("robots_txt_disallow")

    canonical = page.canonical or resp.links.get("canonical", {}).get("url", "")
    if canonical:
        out["canonical"] = urljoin(url, canonical)
        if norm(out["canonical"]) != norm(url):
            reasons.append("canonical_elsewhere")

    if target:
        out["link_found"] = "yes" if page.rels else "no"
        out["link_rel"] = ";".join(sorted(set(page.rels)))
    return done("blocked" if reasons else "live_open", ";".join(reasons))


def main():
    ap = argparse.ArgumentParser(description="Check whether bought backlinks are live and indexable.")
    ap.add_argument("input", help="CSV with a 'url' column and an optional 'target' column")
    ap.add_argument("output", help="CSV report to write")
    ap.add_argument("--workers", type=int, default=8, help="parallel requests (default 8)")
    ap.add_argument("--timeout", type=float, default=20, help="seconds per request (default 20)")
    args = ap.parse_args()

    with open(args.input, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if "url" not in (reader.fieldnames or []):
            sys.exit("Input CSV needs a 'url' column.")
        rows = [r for r in reader if (r.get("url") or "").strip()]

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    adapter = requests.adapters.HTTPAdapter(pool_connections=args.workers, pool_maxsize=args.workers)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    counts = Counter()
    with open(args.output, "w", newline="", encoding="utf-8") as f, \
            ThreadPoolExecutor(max_workers=args.workers) as pool:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for result in pool.map(lambda r: check(r, session, args.timeout), rows):
            writer.writerow(result)
            f.flush()
            counts[result["verdict"]] += 1

    total = sum(counts.values()) or 1
    for verdict, n in counts.most_common():
        print(f"{verdict:22} {n:6}  {n / total:6.1%}")


if __name__ == "__main__":
    main()
