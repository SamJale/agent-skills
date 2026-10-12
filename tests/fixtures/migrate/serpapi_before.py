"""Before: a SerpApi call (legacy google-search-results client). Converted offline in test_searchapi.py."""
import os

from serpapi import GoogleSearch


def top_links(query, page=2):
    search = GoogleSearch({
        "engine": "google",
        "q": query,
        "location": "Austin, Texas, United States",
        "gl": "us",
        "hl": "en",
        "num": 10,
        "start": (page - 1) * 10,
        "api_key": os.environ.get("SERPAPI_API_KEY"),
    })
    results = search.get_dict()
    if "error" in results:
        raise RuntimeError(results["error"])
    return [(r["position"], r["title"], r["link"]) for r in results.get("organic_results", [])]
