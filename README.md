# bought-backlink-audit

A small Python script that checks a list of bought backlinks the way Googlebot meets them: one request per URL with the Googlebot Smartphone user agent, redirects not followed, then the HTTP status, `noindex` in meta robots and `X-Robots-Tag`, robots.txt and `rel="canonical"`. The output is a CSV with one verdict per link: is the page still there, and is it open to indexing?

It answers the question a seller's placement report cannot: what is left of the package months later. No paid APIs, no browser, no Search Console access needed, since the pages belong to other sites.

## What it checks

| Verdict | Meaning |
|---|---|
| `dead` | DNS does not resolve, HTTP 404 or 410, or a redirect to the homepage |
| `redirect` | any other 301, 302, 307 or 308 (the `Location` is recorded, not followed) |
| `cloudflare_challenge` | a Cloudflare JS challenge; unverified, not dead |
| `error` | SSL failure, timeout, 403, 5xx, other network errors |
| `blocked` | HTTP 200, but `noindex`/`none` in `<meta name="robots">` or `<meta name="googlebot">`, the same in `X-Robots-Tag` (scoped values like `googlebot: noindex` included), a robots.txt Disallow for Googlebot, or a canonical (HTML or `Link` header) pointing to another URL |
| `live_open` | HTTP 200 and none of the blocks above |

The `reasons` column lists every signal found on the page. With an optional `target` column the script also reports whether the page still links to your site (`link_found`) and with which `rel` (`link_rel`).

## Installation

```bash
git clone https://github.com/uskotoivo81-netizen/bought-backlink-audit.git
cd bought-backlink-audit
pip install -r requirements.txt
```

Python 3.9 or newer. The only dependency is `requests`; robots.txt is parsed with `urllib.robotparser` from the standard library.

## Usage

```bash
python bought_backlink_audit.py links.csv report.csv --workers 8 --timeout 20
```

Input format (`links.example.csv`):

```text
url,target
https://some-web20-site.example/my-post/,yoursite.com
https://forum.example/profile/12345,yoursite.com
```

`url` is required, `target` is optional (a domain or URL matched as a substring of `href`). The output CSV has the columns:

```text
url, verdict, reasons, status, location, x_robots_tag, meta_robots,
canonical, robots_txt, link_found, link_rel, error
```

Rows are flushed as they are written, so an interrupted run keeps its results. A summary by verdict is printed at the end. Tests run offline against a local HTTP server: `python -m unittest discover -s tests`.

## Why these checks

The checks mirror the crawl method of a study of 15,389 links bought on Fiverr and Kwork, re-checked a year later (crawl on August 21, 2026 with a Googlebot Smartphone user agent, index check on October 29, 2026). Its main findings, from the [bought backlinks study (SpeedyIndex)](https://en.speedyindex.com/cases/marketplace-backlinks-one-year-later/):

- only 562 of 15,388 links were in Google's index (3.7%); live, open and indexed: 1.8%;
- 33.7% of links were dead: DNS failure, 404 or 410, parked domain or redirect to the homepage;
- 14.8% were blocked from indexing once every signal was counted;
- of 4,750 URLs that returned HTTP 200, 15.7% carried noindex and 32.4% had an indexing block of some kind;
- among links older than 12 months, 18.5% no longer resolved in DNS, against 2.3% of links aged 6 to 12 months.

A 200 status is not enough, and a single check is not enough either: re-run the same file at 3, 6 and 12 months.

## Limitations

- **No index check.** The script tells you whether a page *can* be indexed, not whether it *is*. Even clean pages are often not indexed (8.6% in the study), so pass the `live_open` rows to an index check (site: and URL queries, or a bulk checker).
- **TLS fingerprint.** The study crawler used a Chrome TLS fingerprint; `requests` does not, so some sites may answer with a 403 or a challenge.
- **Other bot challenges.** Only Cloudflare challenges are recognised. During testing, another CDN served its challenge with HTTP 200; a `live_open` row with `link_found = no` deserves a manual look.
- **robots.txt precedence.** `urllib.robotparser` applies rules in file order rather than Google's longest-match rule, so mixed `Allow`/`Disallow` files can be misread.
- **Not detected:** parked domains that answer 200, and noindex injected by JavaScript.

## A note on policy

Google's [spam policies](https://developers.google.com/search/docs/essentials/spam-policies) treat buying or selling links for ranking purposes as link spam unless the links carry `rel="nofollow"` or `rel="sponsored"`. This tool does not make bought links safer; it shows what is left of them. The `link_rel` column tells you which attribute the seller actually used.

## License

MIT, see [LICENSE](LICENSE).
