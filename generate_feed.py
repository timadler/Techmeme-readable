#!/usr/bin/env python3
"""Generate a "readable links" version of the Techmeme RSS feed.

For every item, the Techmeme permalink <link> is replaced with a readable
URL for the article's main headline link:

  1. domains in the free-domain allowlist (or "unlocked" gift links) -> keep original
  2. known paywalled domains -> READABLE_URL_TEMPLATE (default
     https://www.removepaywall.com/search?url=<encoded article URL>).
     Constructed locally; no request is made to that host. Preferred even
     when a Wayback snapshot is already cached. Not stored in the cache.
  3. otherwise, Wayback Machine availability API -> snapshot URL
  4. archive.ph "newest" lookup (best effort, short timeout, can be disabled)
     for non-paywalled domains only
  5. fall back to the original article URL

Headline and image HREFs in the description that point at the article are
rewritten to the same URL. The original URL is appended as "Original: <url>".
Wayback/archive.ph lookup results are cached in cache.json. See README.md.
"""
import argparse
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

FEED_URL = "https://www.techmeme.com/feed.xml"
USER_AGENT = (
    "techmeme-readable/1.0 (+https://github.com/; personal RSS link rewriter; "
    "polite, cached, rate-limited)"
)
FEED_TITLE = "Techmeme (readable links)"

# Domains that are generally readable without a subscription. Override with
# --free-domains / FREE_DOMAINS (comma separated) or a file via FREE_DOMAINS_FILE.
DEFAULT_FREE_DOMAINS = [
    "blog.google", "github.com", "arstechnica.com", "techcrunch.com",
    "theverge.com", "mashable.com", "polygon.com", "engadget.com",
    "9to5mac.com", "9to5google.com", "macrumors.com", "theblock.co",
    "hollywoodreporter.com", "startupfortune.com", "substack.com",
    "x.com", "twitter.com", "youtube.com", "bsky.app", "reddit.com",
    "wikipedia.org", "sec.gov", "arxiv.org", "openai.com", "anthropic.com",
    "apple.com", "microsoft.com", "aws.amazon.com", "cloudflare.com",
    "techmeme.com",
]

# Known paywalled or metered publishers. Hosts match subdomains
# (www.bloomberg.com, asia.nikkei.com, ...). An entry may include one path
# prefix, "axios.com/pro", so the rest of that host stays on the normal path.
# semafor.com is included. lemonde.fr is intentionally omitted.
# Override with --paywalled-domains / PAYWALLED_DOMAINS or PAYWALLED_DOMAINS_FILE.
DEFAULT_PAYWALLED_DOMAINS = [
    "bloomberg.com",
    "wsj.com",
    "ft.com",
    "nytimes.com",
    "theinformation.com",
    "forbes.com",
    "washingtonpost.com",
    "economist.com",
    "businessinsider.com",
    "theatlantic.com",
    "wired.com",
    "newyorker.com",
    "reuters.com",
    "telegraph.co.uk",
    "barrons.com",
    "fortune.com",
    "axios.com/pro",
    "semafor.com",
    "statnews.com",
    "puck.news",
    "newcomer.co",
    "stratechery.com",
    "bizjournals.com",
    "nikkei.com",
    "asia.nikkei.com",
    "scmp.com",
    "latimes.com",
    "bostonglobe.com",
    "technologyreview.com",
    "hbr.org",
    "thetimes.co.uk",
]

# {url} is the article URL percent-encoded like JavaScript encodeURIComponent
# (how the RemovePaywall extension builds this link). {url_raw} is the article
# URL appended unchanged, for swapping back to archive.ph:
#   https://archive.ph/newest/{url_raw}
# The URL is only constructed. Nothing is fetched from the template host.
DEFAULT_READABLE_URL_TEMPLATE = "https://www.removepaywall.com/search?url={url}"

SUCCESS_TTL = 30 * 24 * 3600      # successful lookups: 30 days
FAILURE_TTL = 3 * 3600            # failed lookups: retry after 3 hours
PRUNE_AGE = 90 * 24 * 3600        # drop cache entries older than this

HEADLINE_RE = re.compile(
    r'<B>\s*<A\s+HREF="([^"]+)"', re.IGNORECASE)
FIRST_HREF_RE = re.compile(r'<A\s+HREF="([^"]+)"', re.IGNORECASE)
HREF_ATTR_RE = re.compile(
    r"""(?is)(\bHREF\s*=\s*)(["'])(.*?)\2""")


def env_bool(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def make_session(timeout_retries=2):
    """requests session with UA and limited retries (retries=0 for best-effort hosts)."""
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    retry = Retry(total=timeout_retries, backoff_factor=1.0,
                  status_forcelist=(500, 502, 503, 504), allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def article_url(item_desc_html, item_link):
    """Find the real article URL: the main bold headline link in the description.

    Techmeme's <link>/<guid> is just the Techmeme permalink; the article is the
    <B><A HREF=...> headline link. Fall back to the first non-techmeme href.
    """
    m = HEADLINE_RE.search(item_desc_html or "")
    if m:
        return html.unescape(m.group(1))
    for href in FIRST_HREF_RE.findall(item_desc_html or ""):
        href = html.unescape(href)
        if "techmeme.com" not in urlparse(href).netloc:
            return href
    return None


def host_matches(url, domains):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def parse_paywall_spec(entries):
    """Return (host, path_prefix or None) rules. path_prefix is lowercase, leading slash."""
    rules = []
    for entry in entries:
        entry = entry.strip().lower()
        if not entry or entry.startswith("#"):
            continue
        if "/" in entry:
            host, _, path = entry.partition("/")
            path = "/" + path.strip("/")
            rules.append((host, path))
        else:
            rules.append((entry, None))
    return rules


def is_paywalled(url, rules):
    """True when url's host (including subdomains) matches a paywall rule.

    Rules with a path prefix match only that path (axios.com/pro matches
    /pro and /pro/... but not /professional or other axios.com paths).
    """
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    path = (parts.path or "/").lower()
    norm = path.rstrip("/") or "/"
    for rule_host, path_prefix in rules:
        if not (host == rule_host or host.endswith("." + rule_host)):
            continue
        if path_prefix is None:
            return True
        prefix = path_prefix.rstrip("/") or "/"
        if norm == prefix or path.startswith(prefix + "/"):
            return True
    return False


def is_free(url, domains):
    if host_matches(url, domains):
        return True
    # Publisher-issued gift links (e.g. NYT) are already readable.
    if "unlocked_article_code=" in url or "gift=" in url:
        return True
    return False


def encode_uri_component(url):
    """Percent-encode like JavaScript encodeURIComponent."""
    return quote(url, safe="!~*'()")


def render_readable_url(template, url):
    """Fill READABLE_URL_TEMPLATE. Does not perform any network I/O.

    {url} is encodeURIComponent(article). {url_raw} is the article unchanged.
    A template with neither placeholder is treated as a prefix and the
    encoded article URL is appended (RemovePaywall's ?url= form).
    """
    if "{url}" not in template and "{url_raw}" not in template:
        return template + encode_uri_component(url)
    # {url_raw} first so a raw URL is not processed by the {url} replacement.
    return (template
            .replace("{url_raw}", url)
            .replace("{url}", encode_uri_component(url)))


def lookup_wayback(session, url, timeout):
    r = session.get("https://archive.org/wayback/available",
                    params={"url": url}, timeout=timeout)
    r.raise_for_status()
    snap = (r.json().get("archived_snapshots") or {}).get("closest") or {}
    if snap.get("available") and snap.get("url"):
        u = snap["url"]
        return re.sub(r"^http://", "https://", u, count=1)
    return None


def lookup_archive_ph(session, url, timeout):
    """Best-effort network lookup of an existing archive.ph snapshot.

    Not used for paywalled domains (those get a constructed template URL)
    and disabled on GitHub Actions, where archive.ph is captcha-blocked.
    """
    r = session.get("https://archive.ph/newest/" + quote(url, safe=":/?&=%"),
                    timeout=timeout, allow_redirects=False)
    loc = r.headers.get("Location", "")
    if r.status_code in (301, 302, 303, 307, 308) and loc:
        # a real snapshot looks like https://archive.ph/AbCde (not /newest/ again)
        if "/newest/" not in loc and re.match(
                r"^https?://archive\.(ph|today|is|md|vn|fo|li)/[A-Za-z0-9]+/?$", loc):
            return loc
    return None


def rewrite_article_hrefs(desc, article, readable):
    """Point every HREF equal to the article URL at the readable URL.

    Publisher homepages, Techmeme permalinks, and other hrefs are left alone.
    The caller appends the Original: line afterwards so that line keeps the
    publisher URL.
    """
    if not desc or not article or readable == article:
        return desc
    escaped = html.escape(readable, quote=True)

    def repl(m):
        if html.unescape(m.group(3)) == article:
            return f"{m.group(1)}{m.group(2)}{escaped}{m.group(2)}"
        return m.group(0)

    return HREF_ATTR_RE.sub(repl, desc)


class Resolver:
    def __init__(self, args, cache):
        self.args = args
        self.cache = cache
        self.session = make_session()
        self.ph_session = make_session(0)  # best effort: no retries
        self.new_lookups = 0
        self.stats = {"free": 0, "paywall": 0, "cached": 0, "wayback": 0,
                      "archive_ph": 0, "fallback": 0, "deferred": 0}
        self._last_net = 0.0

    def _sleep(self):
        wait = self.args.delay - (time.time() - self._last_net)
        if wait > 0:
            time.sleep(wait)
        self._last_net = time.time()

    def _fresh(self, entry, now):
        age = now - entry.get("ts", 0)
        return age < (SUCCESS_TTL if entry.get("method") in ("wayback", "archive_ph")
                      else FAILURE_TTL)

    def resolve(self, url):
        """Return (resolved_url, method).

        method is free, paywall, wayback, archive_ph, fallback, or deferred.
        Paywalled URLs are computed from the template and are not read from
        or written to the cache, so a template or domain-list change applies
        to URLs that were cached under the old logic.
        """
        now = time.time()
        if is_free(url, self.args.free_domains):
            self.stats["free"] += 1
            return url, "free"
        if is_paywalled(url, self.args.paywalled_rules):
            resolved = render_readable_url(self.args.readable_url_template, url)
            self.stats["paywall"] += 1
            return resolved, "paywall"
        entry = self.cache.get(url)
        if entry and self._fresh(entry, now):
            self.stats["cached"] += 1
            m = entry["method"]
            self.stats["wayback" if m == "wayback" else
                       "archive_ph" if m == "archive_ph" else "fallback"] += 1
            return entry["resolved"], m
        if self.new_lookups >= self.args.max_new:
            self.stats["deferred"] += 1
            # use stale entry if any, else original
            return (entry["resolved"] if entry else url), "deferred"
        self.new_lookups += 1

        resolved, method = None, "fallback"
        try:
            self._sleep()
            resolved = lookup_wayback(self.session, url, self.args.timeout)
            if resolved:
                method = "wayback"
        except Exception as e:  # network/JSON errors -> try next / fall back
            print(f"  wayback error for {url}: {e}", file=sys.stderr)
        if not resolved and self.args.archive_ph:
            try:
                self._sleep()
                resolved = lookup_archive_ph(self.ph_session, url, self.args.ph_timeout)
                if resolved:
                    method = "archive_ph"
            except Exception as e:
                print(f"  archive.ph error for {url}: {e}", file=sys.stderr)
        if not resolved:
            resolved = url
        self.cache[url] = {"resolved": resolved, "method": method, "ts": int(now)}
        self.stats[method] += 1
        return resolved, method


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_cache(path, cache):
    cutoff = time.time() - PRUNE_AGE
    pruned = {k: v for k, v in cache.items() if v.get("ts", 0) >= cutoff}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pruned, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def build_feed(source_xml, resolver):
    root = ET.fromstring(source_xml)
    channel = root.find("channel")
    out_root = ET.Element("rss", {"version": "2.0"})
    out_ch = ET.SubElement(out_root, "channel")
    # Channel metadata: copy, but retitle. lastBuildDate is taken from the source
    # (not "now") so unchanged feeds produce byte-identical output.
    for tag in ("title", "link", "description", "language", "pubDate", "lastBuildDate"):
        src = channel.find(tag)
        if src is None:
            continue
        el = ET.SubElement(out_ch, tag)
        el.text = FEED_TITLE if tag == "title" else src.text
    log = []
    for item in channel.findall("item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        desc = item.findtext("description") or ""
        art = article_url(desc, link)
        new_link, method = link, "no-article-link"
        new_desc = desc
        if art:
            new_link, method = resolver.resolve(art)
            new_desc = rewrite_article_hrefs(desc, art, new_link)
            new_desc = (new_desc.rstrip() +
                        f'\n<P>Original: <A HREF="{html.escape(art, quote=True)}">'
                        f'{html.escape(art)}</A></P>\n')
        o = ET.SubElement(out_ch, "item")
        ET.SubElement(o, "title").text = title
        ET.SubElement(o, "link").text = new_link
        ET.SubElement(o, "description").text = new_desc
        ET.SubElement(o, "pubDate").text = item.findtext("pubDate")
        g = item.find("guid")
        ge = ET.SubElement(o, "guid")
        ge.text = g.text if g is not None else link
        if g is not None and g.get("isPermaLink"):
            ge.set("isPermaLink", g.get("isPermaLink"))
        log.append({"title": title, "techmeme_link": link, "article": art,
                    "new_link": new_link, "method": method})
    ET.indent(out_root)
    xml = ET.tostring(out_root, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n", log


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    e = os.environ.get
    p.add_argument("--feed-url", default=e("FEED_URL", FEED_URL))
    p.add_argument("--input", help="read feed XML from this file instead of fetching")
    p.add_argument("--output", default=e("OUTPUT", "docs/feed.xml"))
    p.add_argument("--cache", default=e("CACHE_FILE", "cache.json"))
    p.add_argument("--max-new", type=int, default=int(e("MAX_NEW_URLS", "30")),
                   help="max uncached URLs to look up per run (default 30)")
    p.add_argument("--delay", type=float, default=float(e("REQUEST_DELAY", "1.0")),
                   help="min seconds between archive lookups (default 1.0)")
    p.add_argument("--timeout", type=float, default=float(e("TIMEOUT", "15")))
    p.add_argument("--ph-timeout", type=float, default=float(e("ARCHIVE_PH_TIMEOUT", "6")))
    p.add_argument("--free-domains", default=e("FREE_DOMAINS"),
                   help="comma-separated list replacing the default free-domain allowlist")
    p.add_argument("--paywalled-domains", default=e("PAYWALLED_DOMAINS"),
                   help="comma-separated list replacing the default paywalled domains; "
                        "use host/path for a path prefix (axios.com/pro)")
    p.add_argument("--readable-url-template",
                   default=e("READABLE_URL_TEMPLATE", DEFAULT_READABLE_URL_TEMPLATE),
                   help="template for paywalled links. {url}=encoded article, "
                        "{url_raw}=article as-is. No network call. "
                        "Default: removepaywall search URL. "
                        "Archive.ph swap: https://archive.ph/newest/{url_raw}")
    ph = p.add_mutually_exclusive_group()
    ph.add_argument("--archive-ph", dest="archive_ph", action="store_true",
                    default=env_bool("ARCHIVE_PH", True))
    ph.add_argument("--no-archive-ph", dest="archive_ph", action="store_false",
                    help="disable the archive.ph network lookup (or ARCHIVE_PH=0). "
                         "Does not affect paywalled template links.")
    p.add_argument("--report", help="write per-item JSON report to this file")
    a = p.parse_args(argv)
    if a.free_domains is not None:
        a.free_domains = [d.strip().lower() for d in a.free_domains.split(",") if d.strip()]
    else:
        a.free_domains = list(DEFAULT_FREE_DOMAINS)
    ff = e("FREE_DOMAINS_FILE")
    if ff and os.path.exists(ff):
        with open(ff, encoding="utf-8") as f:
            a.free_domains += [l.strip().lower() for l in f
                               if l.strip() and not l.startswith("#")]
    if a.paywalled_domains is not None:
        raw_paywall = [d.strip().lower() for d in a.paywalled_domains.split(",") if d.strip()]
    else:
        raw_paywall = list(DEFAULT_PAYWALLED_DOMAINS)
    pf = e("PAYWALLED_DOMAINS_FILE")
    if pf and os.path.exists(pf):
        with open(pf, encoding="utf-8") as f:
            raw_paywall += [l.strip().lower() for l in f
                            if l.strip() and not l.startswith("#")]
    a.paywalled_rules = parse_paywall_spec(raw_paywall)
    return a


def main(argv=None):
    args = parse_args(argv)
    if args.input:
        with open(args.input, "rb") as f:
            source = f.read()
    else:
        s = make_session()
        r = s.get(args.feed_url, timeout=30)
        r.raise_for_status()
        source = r.content
    cache = load_cache(args.cache)
    resolver = Resolver(args, cache)
    xml, log = build_feed(source, resolver)
    ET.fromstring(xml.encode("utf-8"))  # sanity: output must parse
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    tmp = args.output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(xml)
    os.replace(tmp, args.output)
    save_cache(args.cache, cache)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=1, ensure_ascii=False)
    by_method = dict(Counter(row["method"] for row in log))
    st = resolver.stats
    print(f"items={len(log)} new_lookups={resolver.new_lookups} "
          f"by_method={by_method} stats={st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
