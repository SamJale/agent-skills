"""After: the same function on SearchApi, converted with skills/searchapi-migrate/references/mapping.md."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("SEARCHAPI_BASE_URL", "https://www.searchapi.io")   # the test points this at the mock


def top_links(query, page=2):
    params = {
        "engine": "google",
        "q": query,
        "location": "Austin,Texas,United States",
        "gl": "us",
        "hl": "en",
        "page": page,              # was start=(page - 1) * 10; num is ignored on SearchApi, so it is gone
    }
    key = os.environ.get("SEARCHAPI_API_KEY")
    if not key:                    # fail fast: never send an empty Bearer token
        raise RuntimeError("SEARCHAPI_API_KEY is not set")
    headers = {"Authorization": "Bearer " + key}
    req = urllib.request.Request(BASE + "/api/v1/search?" + urllib.parse.urlencode(params), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=100) as resp:
            results = json.load(resp)
    except urllib.error.HTTPError as e:
        results = json.load(e)
    if results.get("error"):
        raise RuntimeError(results["error"])
    # position restarts at 1 on each SearchApi page: add the offset to keep a rank across pages
    return [((page - 1) * 10 + r["position"], r["title"], r["link"]) for r in results.get("organic_results", [])]
