# techmeme-readable

Generates `docs/feed.xml`: a copy of the [Techmeme RSS feed](https://www.techmeme.com/feed.xml)
(same title, pubDate, guid, description per item) titled **"Techmeme (readable links)"**, where each
item's `<link>` points to a readable version of the article instead of the Techmeme permalink.

## How it works

In Techmeme's feed, `<link>` and `<guid>` are the *Techmeme permalink*. The real article is the bold
headline link (`<B><A HREF="...">`) inside the item's HTML `<description>`. For that article URL the
script tries, in order:

1. **Free-domain allowlist** (and publisher "gift" links such as `unlocked_article_code=`) → keep the original URL.
2. **Wayback Machine** availability API → use the snapshot URL, if one exists.
3. **archive.ph** `/newest/` lookup (best effort, short timeout; often blocked by captchas/timeouts).
4. **Fallback** → original article URL.

The original URL is appended to each description as `Original: <url>`. The `guid` is unchanged, so
feed readers won't see duplicates.

Results are cached in `cache.json` (`url -> resolved, method, ts`). Wayback/archive.ph hits are cached
30 days; failures (fallbacks) are retried after 3 hours. At most `MAX_NEW_URLS` (default 30) uncached
URLs are looked up per run; the rest are deferred to the next run. Requests use a User-Agent, timeouts,
retries with backoff, and a 1 s delay between lookups.

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
| `--no-archive-ph` / `--archive-ph` | `ARCHIVE_PH=0/1` | enabled |
| `--free-domains a.com,b.com` | `FREE_DOMAINS` | built-in list (see `DEFAULT_FREE_DOMAINS`) |
| – | `FREE_DOMAINS_FILE` | extra domains, one per line |
| `--output`, `--cache`, `--input`, `--report` | `OUTPUT`, `CACHE_FILE` | `docs/feed.xml`, `cache.json` |

## Deploy on GitHub

1. Create a GitHub repo and push this directory to the `main` branch.
2. **Settings → Actions → General → Workflow permissions**: allow read and write (the workflow also declares `contents: write`).
3. **Settings → Pages → Build and deployment**: Source = *Deploy from a branch*, Branch = `main`, folder = `/docs`.
4. Run the workflow once from the **Actions** tab (*Update feed → Run workflow*); afterwards it runs every 20 minutes
   (`.github/workflows/update-feed.yml`) and commits `docs/feed.xml` and `cache.json` only when they change.
5. Subscribe your reader to:

   `https://<user>.github.io/<repo>/feed.xml`

## Caveats

- Wayback often has **no snapshot yet** for brand-new articles; those fall back to the original link and are retried every 3 hours.
- A Wayback snapshot of a paywalled page may itself show the paywall, and Wayback snapshots can be slightly stale.
- archive.ph frequently times out or serves captchas, especially from cloud IPs; set `ARCHIVE_PH=0` to skip it.
- GitHub may pause scheduled workflows in repos with no activity for 60 days, and cron timing is best-effort.
- Whether bypassing paywalls via archives is acceptable is up to you; consider supporting publishers you read.
