---
name: searchapi-migrate
description: "SearchApi migration: moves SerpApi, Serper, DataForSEO or Bright Data SERP API code to SearchApi.io. Use when a user wants to switch SERP API providers, port a scraper or compare outputs. Maps engines, params, response fields and auth, then checks one side-by-side call per endpoint. Not for writing new search code (use the searchapi skill)."
license: MIT
metadata:
  version: "1.0.0"
  verified_on: "2026-10-12"
---

# SearchApi migrate

Ports existing SERP API code to SearchApi.io. The `searchapi` skill owns the API mechanics (setup, calls, trimming, errors) and the script this skill runs, so install it beside this one. This skill owns the move. The mapping lives in `references/mapping.md`. Paths are relative to this skill's folder.

## When to Use

The user's code calls SerpApi, Serper, DataForSEO or Bright Data's SERP API and they want it on SearchApi, or want the two compared.

## Steps

1. **Find the calls.** Search code, config and env files, ignoring case, for:
   - SerpApi: `serpapi`, `serp_api_key`, `google_search_results`, `google-search-results`, `GoogleSearch(`
   - Serper: `serper`; `X-API-KEY` counts only in a file that also says `serper`
   - DataForSEO: `dataforseo`, `/v3/serp/`, `/v3/merchant/`, `RestClient`
   - Bright Data: `brightdata`, `bright_data`, `bdclient`, `brd-customer-`, `brd.superproxy.io`, `brd_json`, `x-unblock-data-format`, `gd_mfz5x93lmsjjjylob`

   List each call site with its engine, params and the response fields the code reads.
2. **Map each call** with `references/mapping.md`: read SearchApi basics and only that vendor's section. Map the engine, then each param, then each response field. When a param is unclear, check it with the searchapi skill's `searchapi.py params ENGINE PARAM` or the MCP tool schema.
3. **Switch auth.** Commit first, or keep a copy of the old code and its key, so step 4 can still run it. The key comes from `SEARCHAPI_API_KEY` (fail fast when it is unset) and goes only in the `Authorization: Bearer` header, never in the URL. The endpoint is `https://www.searchapi.io/api/v1/search` with `engine` plus params as a GET query. Remove the old vendor key from the ported code and config, and keep the user's error handling: a failed search has a top-level `error`.
4. **Check side by side**, with the user's OK (each check costs a search on both services). Per endpoint, run one identical query on both: the old code as it was, and SearchApi through the searchapi skill's script with `--skill searchapi-migrate`. Compare result counts, the top 5 links, and every field the code reads. Report the differences in a short table. If the user runs the old side themselves, offer one SearchApi smoke call per endpoint the same way; it costs credits, so run it only on their OK.
5. **List the gaps**: anything the mapping marks "no equivalent" or "docs only", and what you did instead. Never drop a feature silently.

Python shape of a ported call (keep the user's HTTP library):

```python
key = os.environ.get("SEARCHAPI_API_KEY")
if not key:
    raise RuntimeError("SEARCHAPI_API_KEY is not set")
headers = {"Authorization": "Bearer " + key}
params = {"engine": "google", "q": query, "location": "Austin,Texas,United States", "gl": "us", "hl": "en"}
resp = requests.get("https://www.searchapi.io/api/v1/search", params=params, headers=headers, timeout=100)
data = resp.json()
if resp.status_code != 200 or data.get("error"):
    raise RuntimeError(data.get("error", resp.status_code))
```

## Traps

- SerpApi and SearchApi look alike, but some names differ (`search_query`, `start`, `mkt`, result keys): check every row, not just the engine. SerpApi `google_news` is the news.google.com portal: port it to `google_news_portal`, not `google_news`.
- `num` is ignored on SearchApi's `google` and `bing`: port depth as `page` loops (up to 10 rows each), or one `google_rank_tracking` call for a top 100.
- `position` restarts at 1 on every page: to rank across pages add the rows of earlier pages (often under 10). There is no rank across result blocks (DataForSEO `rank_absolute`, Bright Data `global_rank`).
- Locations: `location` wants a canonical `City,Region,Country`. A short name resolves to the most popular match (`London` becomes `London TV Region,England,United Kingdom`, with results from the wrong cities) and a name with no match is a 400. Look names up with the searchapi skill's free `searchapi.py locations QUERY`, and read `location_used` in the script's status line. Turn numeric codes and plain-name `uule` values into names; an encoded `uule` can stay. `google_maps` has no `location`: put the place in `q` or use `ll`.
- Links: by default (`link=raw`) SearchApi `google_news` and `google_videos` pass on Google's link as served, which can be a `google.com/goto` redirect. Send `link=resolved` when the code needs publisher URLs.
- POST bodies, task queues and batches (DataForSEO tasks, Serper arrays, Bright Data async) become one GET per query. For many queries use the searchapi skill's `batch`.
- Response keys change case and nesting (Serper camelCase, DataForSEO one mixed `items[]`): port every field the code reads, using the mapping's response table.

Done when every call site is ported, the user's tests or a sample run pass, and the user has the side-by-side table and the gap list.
