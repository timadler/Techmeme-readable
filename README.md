# techmeme-readable

Generates `docs/feed.xml`: a copy of the [Techmeme RSS feed](https://www.techmeme.com/feed.xml)
(same title and pubDate per item; guid is the Techmeme permalink plus a stable suffix) titled **"Techmeme (readable links)"**, where each
item's `<link>` — and the headline and thumbnail links inside the description — point to a readable
version of the article instead of the Techmeme permalink or the publisher URL.

## How it works

In Techmeme's feed, `<link>` and `<guid>` are the *Techmeme permalink*. The real article is the bold
headline link (`<B><A HREF="...">`) inside the item's HTML `<description>`. For that article URL the
script tries, in order:

1. **Free-domain allowlist** (and publisher "gift" links such as `unlocked_article_code=`) → keep the original URL.
2. **Paywalled domains** → a URL built from `READABLE_URL_TEMPLATE`. The default is
   `https://www.removepaywall.com/search?url=` plus the article URL, percent-encoded the way the
   RemovePaywall browser extension does (`encodeURIComponent`). This is preferred even when a
   Wayback snapshot is already cached: RemovePaywall searches public archives in the reader's
   browser and handles paywalls better than a raw Wayback snapshot. **No request is made** to
   removepaywall.com (or to archive.ph) to build the link.
3. **Other domains:** **Wayback Machine** availability API → snapshot URL, if one exists.
4. **archive.ph** `/newest/` *network lookup* (best effort, short timeout; often blocked by captchas).
   Off on GitHub Actions (`ARCHIVE_PH=0`). Not used for paywalled domains.
5. **Fallback** → original article URL.

Every `HREF` in the description that equals the article URL — the headline and the thumbnail — is
rewritten to that same readable URL. Other links (the publisher homepage, the Techmeme permalink)
are left alone. The original publisher URL is appended after the readable headline as plain text,
`Original (direct link): <url>`, not an `<a>` element, so a reader does not open the publisher from
that line. Each item's `<guid>` is the Techmeme permalink plus a stable suffix (`GUID_SUFFIX`,
default `readable-v2`, producing `<permalink>?readable-v2`) with `isPermaLink="false"`. The suffix
changes the guid once so readers treat the item as new and pick up the rewritten link; it stays
the same on later runs, so regenerating the feed does not create duplicates.

Wayback and archive.ph *lookup* results are cached in `cache.json` (`url -> resolved, method, ts`).
Hits are cached 30 days; failures (fallbacks) are retried after 3 hours. Paywalled resolutions are
**not** cached: they are computed from the template on every run, so a template or domain-list
change applies to URLs that were cached under the old logic. At most `MAX_NEW_URLS` (default 30)
uncached non-paywalled URLs are looked up per run; the rest are deferred to the next run. Requests
use a User-Agent, timeouts, retries with backoff, and a 1 s delay between lookups.

To point paywalled links at archive.ph instead (still constructed locally, no network call):

```bash
READABLE_URL_TEMPLATE='https://archive.ph/newest/{url_raw}' python generate_feed.py
```

`{url}` is the encoded article URL. `{url_raw}` is the article URL appended unchanged.

## Run locally

```bash
pip install -r requirements.txt
python generate_feed.py            # writes docs/feed.xml and cache.json
python generate_feed.py --help
```

Config (flag or env var):

| Flag | Env | Default |
|---|---|---|
| `--max-new` | `MAX_NEW_URLS` | 30 |
| `--delay` | `REQUEST_DELAY` | 1.0 s |
| `--timeout` | `TIMEOUT` | 15 s |
| `--ph-timeout` | `ARCHIVE_PH_TIMEOUT` | 6 s |
| `--no-archive-ph` / `--archive-ph` | `ARCHIVE_PH=0/1` | enabled (network lookup for non-paywalled URLs only; Actions sets `0`) |
| `--free-domains a.com,b.com` | `FREE_DOMAINS` | built-in list (see `DEFAULT_FREE_DOMAINS`) |
| – | `FREE_DOMAINS_FILE` | extra free domains, one per line |
| `--paywalled-domains a.com,b.com/pro` | `PAYWALLED_DOMAINS` | built-in list (see `DEFAULT_PAYWALLED_DOMAINS`) |
| – | `PAYWALLED_DOMAINS_FILE` | extra paywalled hosts, one per line (`host` or `host/path`) |
| `--readable-url-template` | `READABLE_URL_TEMPLATE` | `https://www.removepaywall.com/search?url={url}` |
| `--guid-suffix` | `GUID_SUFFIX` | `readable-v2` (guid = Techmeme permalink + `?` + suffix, `isPermaLink="false"`) |
| `--output`, `--cache`, `--input`, `--report` | `OUTPUT`, `CACHE_FILE` | `docs/feed.xml`, `cache.json` |

## Deploy on GitHub

1. Create a GitHub repo and push this directory to the `main` branch.
2. **Settings → Actions → General → Workflow permissions**: allow read and write (the workflow also declares `contents: write`).
3. **Settings → Pages → Build and deployment**: Source = *Deploy from a branch*, Branch = `main`, folder = `/docs`.
4. Run the workflow once from the **Actions** tab (*Update feed → Run workflow*). The workflow file
   schedules it every 20 minutes (`.github/workflows/update-feed.yml`). GitHub's scheduler delays
   cron jobs, so on this repo it often runs only every few hours; the cron expression is left as-is.
   Each run commits `docs/feed.xml` and `cache.json` only when they change.
5. Subscribe your reader to:

   `https://<user>.github.io/<repo>/feed.xml`

## Caveats

- Paywalled links open RemovePaywall in the reader's browser. A brand-new article may not be in any archive yet; the site searches several archives and can still be used to submit the URL. Actions never calls removepaywall.com.
- Set `READABLE_URL_TEMPLATE=https://archive.ph/newest/{url_raw}` to construct archive.ph links instead. That also makes no network call. archive.ph's separate *lookup* API frequently times out or serves captchas from cloud IPs; Actions sets `ARCHIVE_PH=0` to skip that lookup.
- For everyone else, Wayback often has **no snapshot yet** for brand-new articles; those fall back to the original link and are retried every 3 hours. A Wayback snapshot of a paywalled page may itself show the paywall, which is why paywalled domains do not use it.
- GitHub may pause scheduled workflows in repos with no activity for 60 days. Scheduled cron here is best-effort and in practice often runs only every few hours, not every 20 minutes.
- Whether reading via public archives is acceptable is up to you; consider supporting publishers you read.
