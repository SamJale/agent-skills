"""Local mock of the SearchApi API, built from the saved real responses in tests/fixtures. No credits.

    python3 tests/mock_server.py [--port 8765]
    SEARCHAPI_BASE_URL=http://127.0.0.1:8765 SEARCHAPI_API_KEY=test_key python3 skills/searchapi/scripts/searchapi.py google q=coffee

Routes: /api/v1/search (fixture by engine), /api/v1/locations (q matched on name or canonical_name, limit),
/api/v1/me, /openapi/ENGINE.yaml, /_mock/requests (the log).
Keys starting with "bad" get a 401; keys starting with "low" see 2 credits left, "zero" 0 credits;
keys starting with "down" get a 503 from /api/v1/me. Faults are set through q (or any param) values:
  q=mock:503 or mock:503,503   fail that many times first (429 works too), then answer
  q=mock:400|401|402|403|500   always fail with that status
  q=mock:400,msg=Some text     the same, with this error message (no commas)
  q=mock:echokey               a 400 whose message quotes the key back
  q=mock:trickle               the body arrives in 20 pieces over about 5 s
  q=mock:error200              HTTP 200 with a top-level error
  q=mock:empty                 a valid response with no results
  q=mock:slow=1.5              wait before answering
  q=mock:notjson               HTTP 200 with a body that is not JSON
  q=mock:503,ra=-1             send this Retry-After value with each 429 or 503
  q=mock:redirect              302 to another local host name (a client must not follow it)
  q=mock:truncated             a body cut short (Content-Length says more)
  q=mock:noid                  a valid response without search_metadata.id
  q=mock:nan                   a valid response holding a bare NaN
  q=mock:plan200               HTTP 200 with a plan error, as some engines send
Like the live API: params missing from the engine's saved OpenAPI file are dropped from the echo;
google and google_light echo num as 10 whatever was sent; bing drops num; google_rank_tracking keeps it.
On google engines a location is echoed with location_used: London becomes "London TV Region,England,United
Kingdom", a place name or full City,Region,Country value its canonical name, anything else (leeds, uk) a 400.
"""
import glob
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
PREFERRED = {   # engine -> fixture, where several saved responses exist
    "google": "rest/brand-monitoring/google.json",
    "google_flights": "rest/flight-price-monitor/google_flights_round_trip.json",
    "google_maps": "rest/local-leads-scraper/google_maps_page1.json",
    "google_rank_tracking": "rest/rank-tracking/google_rank_tracking.json",
    "bing": "rest/rank-tracking/bing.json",
    "google_ai_overview": "rest/deep-research/google_ai_overview.json",
}
DROPPED = {"bing": {"num"}}
OVERRIDDEN = {"google": {"num": 10}, "google_light": {"num": 10}}
PERMANENT = (400, 401, 402, 403, 500)
RESOLVED = {"london": "London TV Region,England,United Kingdom"}   # live 2026-10-12: a bare London is a TV region
NOT_FOUND = "Location was not found. Double check location parameter."


def resolve(value, places):
    """The location the live API would search from, or None where it answers 400."""
    v = re.sub(r"\s*,\s*", ",", str(value).strip())
    if v.lower() in RESOLVED:
        return RESOLVED[v.lower()]
    for p in places:
        if v.lower() in (p["name"].lower(), p["canonical_name"].lower()):
            return p["canonical_name"]
    return v if v.count(",") >= 2 else None


def known_params(engine):
    """Param names in the engine's saved OpenAPI file, or None when there is no file."""
    spec = os.path.join(FIX, "openapi", engine + ".yaml")
    if not os.path.isfile(spec):
        return None
    with open(spec, encoding="utf-8") as f:
        return set(re.findall(r"(?m)^\s*(?:- )?name:\s*['\"]?([a-z_0-9]+)", f.read())) | {"engine"}


def load_fixtures():
    by_engine = {}
    for path in sorted(glob.glob(os.path.join(FIX, "rest", "*", "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except ValueError:
            continue
        engine = (doc.get("search_parameters") or {}).get("engine") if isinstance(doc, dict) else None
        if engine and not doc.get("error"):
            by_engine.setdefault(engine, doc)
    for engine, rel in PREFERRED.items():
        with open(os.path.join(FIX, rel), encoding="utf-8") as f:
            by_engine[engine] = json.load(f)
    for path in sorted(glob.glob(os.path.join(FIX, "ai", "*", "*.json"))):
        with open(path, encoding="utf-8") as f:
            for a in json.load(f)["answers"]:
                by_engine.setdefault(a["engine"], a["raw"])
    with open(os.path.join(FIX, "rest", "searchapi-onboarding", "me.json"), encoding="utf-8") as f:
        me = json.load(f)
    places = []                                   # saved /api/v1/locations answers, in file order
    for path in sorted(glob.glob(os.path.join(FIX, "rest", "*", "locations*.json"))):
        with open(path, encoding="utf-8") as f:
            places += json.load(f)
    return by_engine, me, places


class State:
    def __init__(self):
        self.by_engine, self.me, self.places = load_fixtures()
        self.lock = threading.Lock()
        self.requests = []      # (method, path, query dict, headers dict)
        self.attempts = {}      # query string -> count
        self.inflight = 0
        self.max_inflight = 0


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def send(self, code, body, extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            query = json.loads(self.rfile.read(n) or b"{}")
            self.handle_any("POST", urllib.parse.urlparse(self.path).path, query)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            self.handle_any("GET", u.path, dict(urllib.parse.parse_qsl(u.query)))

        def handle_any(self, method, path, query):
            with state.lock:
                state.requests.append((method, self.path, query, {k.lower(): v for k, v in self.headers.items()}))
                state.inflight += 1
                state.max_inflight = max(state.max_inflight, state.inflight)
                key = json.dumps(sorted(query.items()))
                state.attempts[key] = attempt = state.attempts.get(key, 0) + 1
            try:
                self.route(path, query, attempt)
            finally:
                with state.lock:
                    state.inflight -= 1

        def route(self, path, query, attempt):
            if path == "/_mock/requests":            # what clients sent, for tests that drive real agents
                return self.send(200, [{"path": r[1].split("?")[0], "query": r[2]} for r in state.requests[:-1]])
            if path.startswith("/openapi/") and path.endswith(".yaml"):
                spec = os.path.join(FIX, "openapi", os.path.basename(path))
                if not os.path.isfile(spec):
                    return self.send(404, {"error": "Not found"})
                with open(spec, "rb") as f:
                    return self.send(200, f.read())
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer ") or auth[7:].startswith("bad"):
                return self.send(401, {"error": "Invalid API key."})
            if path == "/api/v1/me":
                key = auth[7:]
                if key.startswith("down"):
                    return self.send(503, {"error": "Service unavailable."}, {"Retry-After": "0"})
                left = 2 if key.startswith("low") else 0 if key.startswith("zero") else None
                return self.send(200, state.me if left is None else dict(state.me, account=dict(state.me["account"], remaining_credits=left)))
            if path not in ("/api/v1/search", "/api/v1/locations"):
                return self.send(404, {"error": "Not found"})
            tokens = next((v[5:] for v in query.values() if str(v).startswith("mock:")), "").split(",")
            for t in tokens:
                if t.startswith("slow="):
                    time.sleep(float(t[5:]))
            ra = next((t[3:] for t in tokens if t.startswith("ra=")), "0")
            msg = next((t[4:] for t in tokens if t.startswith("msg=")), None)
            fault = next((t for t in tokens if t and not t.isdigit() and not t.startswith(("slow=", "ra=", "msg="))), "")
            fails = [int(x) for x in tokens if x.isdigit()]
            if len(fails) == 1 and fails[0] in PERMANENT:
                code = fails[0]                       # always fails
            else:
                code = fails[attempt - 1] if attempt <= len(fails) else None   # fails N times, then answers
            if code:
                return self.send(code, {"error": msg or "Mock fault %d" % code}, {"Retry-After": ra} if code in (429, 503) else None)
            if fault == "echokey":
                return self.send(400, {"error": "Bad request for key %s" % auth[7:]})
            if path == "/api/v1/locations":           # free on the live API; same key check
                q = str(query.get("q") or "").strip().lower()
                if not q:
                    return self.send(400, {"error": "Missing required parameter: q"})
                try:
                    limit = min(max(int(query.get("limit", 10)), 1), 100)
                except ValueError:
                    return self.send(400, {"error": "limit must be a number"})
                return self.send(200, [p for p in state.places
                                       if q in p["name"].lower() or q in p["canonical_name"].lower()][:limit])
            if fault == "redirect":
                return self.send(302, b"", {"Location": "http://localhost:%d/_mock/stolen" % self.server.server_port})
            if fault == "plan200":
                return self.send(200, {"error": "Search Analytics API requires a Scale plan or above."})
            if fault == "error200":
                return self.send(200, {"error": "Mock error inside a 200 response."})
            if fault == "notjson":
                return self.send(200, b"<html>not json</html>")
            engine = query.get("engine")
            if engine not in state.by_engine:
                return self.send(400, {"error": "Unsupported engine: `%s`." % engine})
            doc = json.loads(json.dumps(state.by_engine[engine]))
            known = known_params(engine)
            echo = {k: v for k, v in query.items()
                    if (known is None or k in known) and k not in DROPPED.get(engine, ())}
            echo.update({k: v for k, v in OVERRIDDEN.get(engine, {}).items() if k in query})
            if "location" in echo and engine.startswith("google"):   # seen live on google; other engines unchecked
                used = resolve(echo["location"], state.places)
                if used is None:
                    return self.send(400, {"error": NOT_FOUND})
                echo["location_used"] = used
            doc["search_parameters"] = echo
            if fault == "empty":
                doc = {k: doc[k] for k in ("search_metadata", "search_parameters")}
            if fault == "noid":
                doc["search_metadata"].pop("id", None)
            if fault == "nan":
                return self.send(200, json.dumps(dict(doc, odd=float("nan"))).encode("utf-8"))
            if fault == "truncated":
                data = json.dumps(doc).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data) + 500))
                self.end_headers()
                self.wfile.write(data[:200])
                self.close_connection = True
                return
            if fault == "trickle":                    # every piece comes before a read timeout, the whole takes 5 s
                data = json.dumps(doc).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                step = len(data) // 20 + 1
                try:
                    for i in range(0, len(data), step):
                        self.wfile.write(data[i:i + step])
                        time.sleep(0.25)
                except OSError:                           # the client gave up: expected
                    pass
                self.close_connection = True
                return
            return self.send(200, doc)
    return Handler


def start(port=0):
    state = State()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
    srv, _ = start(port)
    print("Mock SearchApi on http://127.0.0.1:%d (Ctrl+C to stop)" % srv.server_port)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
