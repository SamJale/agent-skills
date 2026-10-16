"""Offline tests for scripts/searchapi.py: saved real responses and a local mock. No network, no credits.

    python3 -m unittest discover -s tests -v
"""
import glob
import http.client
import importlib.util
import io
import json
import os
import re
import select
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from contextlib import contextmanager, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "skills", "searchapi", "scripts", "searchapi.py")
FIX = os.path.join(HERE, "fixtures")
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)
import mock_server  # noqa: E402

spec = importlib.util.spec_from_file_location("searchapi", SCRIPT)
sa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sa)

TEST_KEY = "test_key_0123456789abcdef"


def rest_fixtures():
    """(name, parsed doc or None, raw text) for all 69 saved REST responses."""
    out = []
    for path in sorted(glob.glob(os.path.join(FIX, "rest", "*", "*.json"))):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        out.append((os.path.relpath(path, FIX), doc, text))
    return out


def ai_fixtures():
    """198 AI-engine answers from one tracker run, one file per term and engine (each under 256 KiB)."""
    out = []
    for path in sorted(glob.glob(os.path.join(FIX, "ai", "*", "*.json"))):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        out += [("ai/%s/%s/%d" % (d["term"], d["engine"], i), a["raw"]) for i, a in enumerate(d["answers"])]
    return out


REST = rest_fixtures()
VALID = [(n, d) for n, d, _ in REST if isinstance(d, dict) and (d.get("search_metadata") or {}).get("status") == "Success"]
ERRORS = [(n, d) for n, d, _ in REST if isinstance(d, dict) and d.get("error")]
SUPPORT = [(n, d) for n, d, _ in REST if isinstance(d, list) or isinstance(d, dict) and "account" in d]
INVALID = [(n, t) for n, d, t in REST if d is None]
AI = ai_fixtures()


def walk(node, path=()):
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, path + (i,))


def get(node, path):
    for p in path:
        node = node[p]
    return node


def plain_trim(node):
    """Independent reference for docs without AI answers: what lossless trimming must leave."""
    if isinstance(node, dict):
        out = {k: plain_trim(v) for k, v in node.items()}
        return {k: v for k, v in out.items() if v is not None and v != [] and v != {}}
    if isinstance(node, list):
        return [plain_trim(v) for v in node if not (isinstance(v, str) and v.startswith("data:"))]
    return None if isinstance(node, str) and node.startswith("data:") else node


def has_answer(doc):
    return any(isinstance(n, dict) and "markdown" in n for _, n in walk(doc))


MISSING = object()


@contextmanager
def patched(obj, **attrs):
    """Set attributes on a module or object for a while; ones that did not exist are removed again."""
    old = {k: getattr(obj, k, MISSING) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is MISSING:
                delattr(obj, k)
            else:
                setattr(obj, k, v)


def set_env(values):
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@contextmanager
def environ(**kw):
    """Set (or with None, unset) environment variables for a while."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        set_env(kw)
        yield
    finally:
        set_env(old)


def free_port():
    """A local port nothing listens on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FixtureSets(unittest.TestCase):
    def test_fixture_counts(self):
        self.assertEqual(len(REST), 69)
        self.assertEqual((len(VALID), len(ERRORS), len(SUPPORT), len(INVALID)), (57, 7, 4, 1))
        self.assertEqual(len(AI), 198)


class Trim(unittest.TestCase):
    docs = VALID + AI

    def test_no_data_uri_left(self):
        for name, doc in self.docs:
            for _, v in walk(sa.trim(doc, top=True)):
                self.assertFalse(isinstance(v, str) and v.startswith("data:"), name)

    def test_no_row_dropped(self):
        for name, doc in self.docs:
            t = sa.trim(doc, top=True)
            for path, v in walk(doc):
                if not isinstance(v, list) or not v or path[0] in ("search_parameters", "search_metadata"):
                    continue
                if all(isinstance(x, str) and x.startswith("data:") for x in v):
                    continue
                try:
                    tv = get(t, path)
                except (KeyError, IndexError, TypeError):
                    self.assertIn("text_blocks", path, "%s lost %s" % (name, path))
                    continue
                kept = [x for x in v if not (isinstance(x, str) and x.startswith("data:"))]
                self.assertEqual(len(tv), len(kept), "%s %s" % (name, path))

    def test_lossless_outside_ai_answers(self):
        for name, doc in self.docs:
            if has_answer(doc):
                continue
            expect = plain_trim({k: v for k, v in doc.items() if k not in ("search_parameters", "search_metadata")})
            got = sa.trim(doc, top=True)
            got.pop("search_metadata", None)
            self.assertEqual(got, expect, name)

    def test_every_url_kept(self):
        """Image URLs, links and pagination survive; only base64 and the request echo go."""
        for name, doc in self.docs:
            text = json.dumps(sa.trim(doc, top=True), ensure_ascii=False)
            for path, v in walk(doc):
                if path and path[0] in ("search_parameters", "search_metadata"):
                    continue
                if isinstance(v, str) and v.startswith("http"):
                    self.assertIn(json.dumps(v, ensure_ascii=False)[1:-1], text, "%s %s" % (name, path))
            if doc.get("pagination"):
                self.assertEqual(sa.trim(doc, top=True)["pagination"], plain_trim(doc["pagination"]), name)

    def test_image_urls_kept(self):
        kept = 0
        for name, doc in self.docs:
            t = json.dumps(sa.trim(doc, top=True), ensure_ascii=False)
            for path, v in walk(doc):
                if path and str(path[-1]) in ("thumbnail", "image", "favicon") and isinstance(v, str) and v.startswith("http"):
                    self.assertIn(json.dumps(v, ensure_ascii=False)[1:-1], t, name)
                    kept += 1
        self.assertGreater(kept, 100)

    def test_metadata_cut_to_four_fields(self):
        for name, doc in VALID:
            meta = sa.trim(doc, top=True)["search_metadata"]
            self.assertEqual(set(meta), {"id", "status", "json_url", "html_url"}, name)

    def test_ai_answer_dedupe(self):
        dropped = kept = 0
        for name, doc in self.docs:
            for path, n in walk(doc):
                if not (isinstance(n, dict) and isinstance(n.get("markdown"), str) and n.get("text_blocks")):
                    continue
                t = get(sa.trim(doc, top=True), path)
                if n.get("reference_links"):
                    self.assertEqual(t["reference_links"], plain_trim(n["reference_links"]), name)
                self.assertNotRegex(t["markdown"], r"(?m)^\[\[\d+\] - .*\]\(http", name)
                if "text_blocks" in t:
                    kept += 1          # markdown misses some of the text: both copies stay
                    continue
                dropped += 1
                texts, cited = [], set()
                sa.answer_parts(n["text_blocks"], texts, cited)
                self.assertLessEqual(cited, {int(x) for x in sa.MARK.findall(t["markdown"])}, name)  # citations kept
                flat = sa.seq(sa.LINK.sub("]", t["markdown"]))
                for s in texts:
                    if not s.startswith("http") and sa.WORD.search(s):
                        self.assertIn(sa.seq(s), flat, "%s lost text %r" % (name, s[:60]))
        self.assertGreater(dropped, 40)
        self.assertGreater(kept, 50)

    def test_savings(self):
        def size(d):
            return len(json.dumps(d, separators=(",", ":"), ensure_ascii=False))
        raw = sum(size(d) for _, d in VALID)
        slim = sum(size(sa.trim(d, top=True)) for _, d in VALID)
        g = [(n, d) for n, d in VALID if d["search_parameters"]["engine"] in ("google", "google_light", "bing")]
        g_raw, g_slim = sum(size(d) for _, d in g), sum(size(sa.trim(d, top=True)) for _, d in g)
        print("\n  trim saves %.0f%% of characters on 57 responses, %.0f%% on Google and Bing web SERPs"
              % (100 - 100.0 * slim / raw, 100 - 100.0 * g_slim / g_raw))
        self.assertLess(slim, raw * 0.85)    # characters; o200k tokens drop more, base64 is token-dense
        self.assertLess(g_slim, g_raw * 0.7)


class Pick(unittest.TestCase):
    def test_pick_fields(self):
        doc = dict(VALID)["rest/brand-monitoring/google.json"]
        out, missing = sa.pick(sa.trim(doc, top=True), "organic_results:position,link;nope")
        self.assertEqual(missing, ["nope"])
        self.assertEqual(len(out["organic_results"]), len(doc["organic_results"]))
        self.assertEqual(set().union(*map(set, out["organic_results"])), {"position", "link"})

    def test_overview_lists_paths(self):
        doc = dict(VALID)["rest/rank-tracking/google_rank_tracking.json"]
        text = sa.overview(sa.trim(doc, top=True))
        self.assertIn("organic_results[%d]" % len(doc["organic_results"]), text)
        self.assertIn("link url", text)


class Params(unittest.TestCase):
    def test_all_samples_parse(self):
        specs = sorted(glob.glob(os.path.join(FIX, "openapi", "*.yaml")))
        self.assertGreaterEqual(len(specs), 10)
        for path in specs:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            got = [p for p in sa.oas_params(text) if isinstance(p, dict) and p.get("name")]
            # independent count: entries in the GET parameter list (name or $ref)
            block = re.search(r"(?ms)^    get:.*?^      parameters:\n(.*?)(?=^      \S|^    \S|^  \S)", text).group(1)
            expect = len(re.findall(r"(?m)^        - ", block))
            self.assertEqual(len(got), expect, path)
            self.assertTrue(all(p.get("schema") is not None for p in got), path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                sa.params(path)
            rows = buf.getvalue().strip().split("\n")
            n = len([p for p in got if p["name"] not in ("engine", "api_key")])
            self.assertEqual(json.loads(rows[0])["params"], n, path)
            self.assertEqual(len(rows) - 2, n, path)
            self.assertNotIn("api_key", buf.getvalue().split("\n(req")[0], path)


# ------------------------------------------------------------------ CLI against the mock

class MockCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.state = mock_server.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="searchapi_test_")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": self.home, "USERPROFILE": self.home,
                    "SEARCHAPI_DIR": os.path.join(self.tmp, "data"),
                    "SEARCHAPI_BASE_URL": "http://127.0.0.1:%d" % self.port, "SEARCHAPI_API_KEY": TEST_KEY}
        self.state.requests.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, env=None, stdin=None, timeout=60, cwd=None, isolated=True):
        """isolated=False drops -I, which would also ignore PYTHONIOENCODING."""
        e = dict(self.env, **(env or {}))
        e = {k: v for k, v in e.items() if v is not None}
        r = subprocess.run([sys.executable] + (["-I"] if isolated else ["-s", "-B"]) + [SCRIPT] + list(args), input=stdin,
                           capture_output=True, cwd=cwd, text=True, env=e, timeout=timeout,
                           stdin=None if stdin is not None else subprocess.DEVNULL)
        head = json.loads(r.stdout.split("\n", 1)[0]) if r.stdout.strip() else {}
        return r.returncode, head, r.stdout, r.stderr

class Cli(MockCase):
    def test_call_status_line_and_trimmed_json(self):
        code, head, out, _ = self.run_cli("google", "q=coffee", "gl=us", "hl=en")
        self.assertEqual(code, 0, out)
        self.assertTrue(head["ok"])
        self.assertTrue(head["id"].startswith("search_"))
        self.assertEqual(head["count"], 10)
        self.assertTrue(os.path.isfile(head["raw"]))
        body = json.loads(out.split("\n", 1)[1])
        self.assertNotIn("search_parameters", body)
        self.assertIn("organic_results", body)
        with open(head["raw"], encoding="utf-8") as f:
            self.assertIn("search_parameters", json.load(f))     # the raw file is untouched

    def test_key_only_in_header_and_client_tag(self):
        self.run_cli("google", "q=coffee", env={"CLAUDECODE": "1"})
        method, url, query, headers = self.state.requests[-1]
        self.assertNotIn(TEST_KEY, url)
        self.assertNotIn(TEST_KEY, json.dumps(query))
        self.assertEqual(headers["authorization"], "Bearer " + TEST_KEY)
        self.assertEqual(headers["x-searchapi-client"], "searchapi-skills/1.0.0 (claude-code; searchapi)")

    def test_client_tag_unknown_harness(self):
        self.run_cli("google", "q=coffee", "--skill", "searchapi-migrate")
        self.assertEqual(self.state.requests[-1][3]["x-searchapi-client"],
                         "searchapi-skills/1.0.0 (unknown; searchapi-migrate)")

    def test_ignored_params(self):
        code, head, _, _ = self.run_cli("google", "q=coffee", "num=100", "gl=us", "foo=bar")
        self.assertEqual(code, 0)
        self.assertEqual(head["ignored"], ["foo"])
        self.assertEqual(head["changed"], {"num": 10})         # live: google echoes num as 10
        code, head, _, _ = self.run_cli("bing", "q=coffee", "page=100", "num=100")
        self.assertEqual(head["ignored"], ["num"])              # the same value on another param does not hide it
        self.assertNotIn("changed", head)

    def test_big_response_gives_overview(self):
        code, head, out, _ = self.run_cli("google_rank_tracking", "q=best serp api")
        self.assertEqual(code, 0)
        self.assertTrue(head["overview"])
        self.assertIn("pick %s" % head["id"], head["next"])
        self.assertLess(len(out), 25000)
        self.assertIn("organic_results[", out)
        with open(head["raw"], encoding="utf-8") as f:
            self.assertGreater(len(json.dumps(sa.trim(json.load(f), top=True))), sa.OVERVIEW_AT)
        n = len(self.state.requests)
        code, head2, out2, _ = self.run_cli("pick", head["id"], "--fields", "organic_results:position,link")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.state.requests), n)              # pick makes no call
        rows = json.loads(out2.split("\n", 1)[1])["organic_results"]
        self.assertEqual(len(rows), head["count"])
        self.assertEqual(set(rows[0]), {"position", "link"})

    def test_small_response_prints_json(self):
        code, head, out, _ = self.run_cli("google_autocomplete", "q=serp")
        self.assertEqual(code, 0)
        self.assertNotIn("overview", head)
        json.loads(out.split("\n", 1)[1])

    def test_full_prints_untrimmed(self):
        code, head, out, _ = self.run_cli("google", "q=coffee", "--full")
        self.assertEqual(code, 0)
        self.assertIn("search_parameters", json.loads(out.split("\n", 1)[1]))

    def test_fields_on_a_call(self):
        code, head, out, _ = self.run_cli("google_maps", "q=plumbers in Norwich", "--fields", "local_results:title,rating")
        self.assertEqual(code, 0)
        rows = json.loads(out.split("\n", 1)[1])["local_results"]
        self.assertTrue(rows and set().union(*map(set, rows)) <= {"title", "rating"})

    def test_empty_is_ok(self):
        code, head, _, _ = self.run_cli("google", "q=mock:empty")
        self.assertEqual(code, 0)
        self.assertTrue(head["empty"])
        self.assertEqual(head["count"], 0)

    def test_retries_then_succeeds(self):
        code, head, _, _ = self.run_cli("google", "q=mock:503,429")
        self.assertEqual(code, 0)
        self.assertEqual(sum(1 for r in self.state.requests if r[2].get("q") == "mock:503,429"), 3)

    def test_exit_codes(self):
        cases = [
            (("google", "q=mock:400"), 2), (("google", "nokeyvalue"), 2), (("Bad-Engine", "q=x"), 2),
            (("google", "api_" "key=abc"), 2), (("google", "q=mock:401"), 3), (("google", "q=mock:402"), 3),
            (("google", "q=mock:403"), 3), (("google", "q=mock:error200"), 4), (("google", "q=mock:500"), 4),
            (("google", "q=mock:503,503,503"), 4), (("google", "q=mock:notjson"), 4), (("no_such_engine", "q=x"), 2),
        ]
        for args, want in cases:
            code, head, out, _ = self.run_cli(*args)
            self.assertEqual(code, want, "%s -> %s" % (args, out))
            self.assertFalse(head["ok"], args)
            self.assertLessEqual(len(head["error"]), 2000)
        n = len(self.state.requests)
        self.run_cli("google", "q=mock:401")
        self.assertEqual(len(self.state.requests), n + 1)      # 401 is never retried

    def test_no_key(self):
        code, head, _, _ = self.run_cli("google", "q=x", env={"SEARCHAPI_API_KEY": None})
        self.assertEqual(code, 3)
        self.assertIn("setup", head["error"])
        self.assertIn("utm_source=agent-skill", head["error"])

    def test_network_error_prints_egress_fix(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        code, head, _, _ = self.run_cli("google", "q=x", env={"SEARCHAPI_BASE_URL": "http://127.0.0.1:%d" % port})
        self.assertEqual(code, 5)
        self.assertIn("www.searchapi.io", head["error"])
        self.assertIn("MCP", head["error"])

    def test_base_url_must_be_local(self):
        code, head, _, _ = self.run_cli("google", "q=x", env={"SEARCHAPI_BASE_URL": "https://evil.example.com"})
        self.assertEqual(code, 2)
        self.assertEqual(self.state.requests, [])

    def test_gate_fires_on_every_error_fixture(self):
        for name, doc in ERRORS:
            sa_request = sa.request
            sa.request = lambda *a, _d=doc: (_d, json.dumps(_d).encode("utf-8"))
            try:
                with self.assertRaises(sa.ApiError) as cm:
                    with redirect_stdout(io.StringIO()):
                        os.environ["SEARCHAPI_DIR"] = os.path.join(self.tmp, "data")
                        sa.search("google", {"q": "x"}, TEST_KEY, "searchapi")
                plan = "plan" in doc["error"] or "subscription" in doc["error"]
                self.assertEqual(cm.exception.code, 3 if plan else 4, name)
                self.assertEqual(cm.exception.msg, doc["error"], name)
            finally:
                sa.request = sa_request
                os.environ.pop("SEARCHAPI_DIR", None)
        self.assertEqual(len(ERRORS), 7)

    def test_status_not_success_fails(self):
        doc = json.loads(json.dumps(VALID[0][1]))
        doc["search_metadata"]["status"] = "Error"
        sa_request, sa.request = sa.request, lambda *a: (doc, json.dumps(doc).encode("utf-8"))
        try:
            os.environ["SEARCHAPI_DIR"] = os.path.join(self.tmp, "data")
            with self.assertRaises(sa.ApiError) as cm:
                sa.search("google", {"q": "x"}, TEST_KEY, "searchapi")
            self.assertEqual(cm.exception.code, 4)
        finally:
            sa.request = sa_request
            os.environ.pop("SEARCHAPI_DIR", None)

    def test_timeout_retries_once(self):
        old = sa.TIMEOUT
        sa.TIMEOUT = 0.3
        os.environ["SEARCHAPI_BASE_URL"] = self.env["SEARCHAPI_BASE_URL"]
        try:
            with self.assertRaises(sa.ApiError) as cm:
                sa.request("/api/v1/search", {"engine": "google", "q": "mock:slow=1.5"}, TEST_KEY, "searchapi")
            self.assertEqual(cm.exception.code, 5)
        finally:
            sa.TIMEOUT = old
            os.environ.pop("SEARCHAPI_BASE_URL", None)
        self.assertEqual(sum(1 for r in self.state.requests if r[2].get("q") == "mock:slow=1.5"), 2)

    def test_params_from_mock(self):
        code, _, out, _ = self.run_cli("params", "google_maps")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"(?m)^q string")
        code, _, out, _ = self.run_cli("params", "google_maps", "ll")
        self.assertEqual(json.loads(out.split("\n")[1])["name"], "ll")
        code, head, _, _ = self.run_cli("params", "not_an_engine")
        self.assertEqual(code, 2)

    def test_credits(self):
        code, head, out, _ = self.run_cli("credits")
        self.assertEqual(code, 0)
        self.assertEqual((head["credits"], head["searches_this_hour"], head["hourly_limit"]), (999517, 1, 200000))
        self.assertIn("Credits left: 999,517. Searches this hour: 1 of 200,000.", out)
        self.assertEqual(self.state.requests[-1][1], "/api/v1/me")

    def test_setup_writes_0600_and_never_echoes(self):
        key = "fresh_key_9876543210zyxw"
        code, head, out, err = self.run_cli("setup", "--key-stdin", stdin=key + "\n", env={"SEARCHAPI_API_KEY": None})
        self.assertEqual(code, 0, out)
        path = os.path.join(self.home, ".config", "searchapi", "key")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertEqual(f.read().strip(), key)
        self.assertNotIn(key, out + err)
        self.assertIn("Connected: 999,517 credits left", out)
        self.assertEqual(self.state.requests[-1][3]["authorization"], "Bearer " + key)
        # the saved key is used when SEARCHAPI_API_KEY is not set
        code, head, _, _ = self.run_cli("google", "q=coffee", env={"SEARCHAPI_API_KEY": None})
        self.assertEqual(code, 0)
        self.assertEqual(self.state.requests[-1][3]["authorization"], "Bearer " + key)

    def test_setup_rejects_bad_key_without_saving(self):
        code, head, out, err = self.run_cli("setup", "--key-stdin", stdin="bad_key_0123456789")
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "searchapi", "key")))
        self.assertNotIn("bad_key_0123456789", out + err)

    def test_setup_without_terminal_does_not_hang(self):
        code, head, _, _ = self.run_cli("setup", timeout=10)
        self.assertEqual(code, 2)
        self.assertIn("--key-stdin", head["error"])

    def test_batch_runs_five_at_a_time_with_retries(self):
        lines = ["# twenty queries", ""]
        for i in range(20):
            q = "mock:slow=0.3" if i % 4 else ("mock:503,slow=0.3" if i % 8 == 0 else "mock:503,503,slow=0.3")
            lines.append('google q="%s" gl=us page=%d' % (q, i))
        path = os.path.join(self.tmp, "queries.txt")
        with open(path, "w") as f:
            f.write("\n".join(lines))
        self.state.max_inflight = 0
        t = time.time()
        code, head, out, _ = self.run_cli("batch", path)
        self.assertEqual(code, 0, out)
        self.assertEqual(head, {"ok": True, "planned": 20, "credits": 999517, "concurrency": 5,
                                "dir": self.env["SEARCHAPI_DIR"]})   # N5: the plan line has ok too
        summary = json.loads(out.split("\n")[1])
        self.assertEqual((summary["ok"], summary["calls"], summary["failed"]), (True, 20, 0))
        table = out.strip().split("\n")[2:]
        self.assertEqual(table[0], "line\tengine\tquery\tstatus\tcount\traw")
        self.assertEqual(len(table), 21)
        self.assertTrue(all(row.split("\t")[3] == "ok" for row in table[1:]))
        self.assertEqual(self.state.max_inflight, 5)
        self.assertEqual(len(self.state.requests), 1 + 20 + 3 + 2 * 2)   # one free balance check; 3 retried once, 2 twice
        self.assertLess(time.time() - t, 20)

    def test_batch_asks_before_more_than_25(self):
        path = os.path.join(self.tmp, "big.txt")
        with open(path, "w") as f:
            f.write("\n".join("google q=coffee page=%d" % i for i in range(30)))
        code, head, _, _ = self.run_cli("batch", path)
        self.assertEqual(code, 2)
        self.assertEqual(head["planned"], 30)
        self.assertIn("999,517 credits left", head["error"])
        self.assertEqual(head["credits"], 999517)
        self.assertEqual([r for r in self.state.requests if r[1].startswith("/api/v1/search")], [])
        code, head, out, _ = self.run_cli("batch", path, "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().split("\n")), 2 + 31)

    def test_batch_reports_failures_and_stops_on_bad_key(self):
        path = os.path.join(self.tmp, "mixed.txt")
        with open(path, "w") as f:
            f.write("google q=coffee\ngoogle q=mock:400\ngoogle num=5 q=tea\n")
        code, head, out, _ = self.run_cli("batch", path)
        self.assertEqual(code, 2)
        rows = out.strip().split("\n")[3:]
        self.assertTrue(rows[1].split("\t")[3].startswith("error: "))
        self.assertEqual(rows[2].split("\t")[3], "ok changed=num")
        self.state.requests.clear()
        code, head, out, _ = self.run_cli("batch", path, env={"SEARCHAPI_API_KEY": "bad_key_123456"})
        self.assertEqual(code, 3)
        self.assertEqual([r[1] for r in self.state.requests], ["/api/v1/me"])   # stops before any search

    def test_batch_bad_line(self):
        path = os.path.join(self.tmp, "bad.txt")
        with open(path, "w") as f:
            f.write("google q=coffee\ngoogle nokeyvalue\n")
        code, head, _, _ = self.run_cli("batch", path)
        self.assertEqual(code, 2)
        self.assertIn("Line 2", head["error"])
        self.assertEqual(self.state.requests, [])

    def test_help(self):
        r = subprocess.run([sys.executable, "-I", SCRIPT, "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertLess(len(r.stdout), 4000)
        self.assertIn("setup", r.stdout)


class SkillCommands(MockCase):
    """Every command written in a SKILL.md runs, from the skill folder, against the mock."""
    skill = os.path.join(ROOT, "skills", "searchapi")

    def commands(self):
        with open(os.path.join(self.skill, "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        spans = re.findall(r"`([^`\n]+)`", text)
        engines = set(re.findall(r"`([a-z0-9_]+)`", text))
        return [c for c in spans if c.startswith(("python3 scripts/", "printf ", "jq "))
                or (c.split(" ")[0] in engines and re.search(r" \w+=", c))]

    def sh(self, cmd, env=None):
        e = dict(self.env, **(env or {}))
        e = {k: v for k, v in e.items() if v is not None}
        r = subprocess.run(["/bin/sh", "-c", cmd], cwd=self.skill, capture_output=True, text=True, env=e,
                           timeout=60, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr

    def test_every_command_runs(self):
        cmds = self.commands()
        self.assertGreaterEqual(len(cmds), 12, cmds)
        code, out, _ = self.sh("python3 scripts/searchapi.py google_rank_tracking q=seed")
        last = json.loads(out.split("\n")[0])
        batch = os.path.join(self.tmp, "q.txt")
        with open(batch, "w") as f:
            f.write("google_light q=coffee gl=us\ngoogle_news q=tea time_period=last_day\n")
        ran = []
        for c in cmds:
            run = c
            if c.startswith("jq "):
                if not shutil.which("jq"):
                    continue
                run = c.replace("FILE", shlex.quote(last["raw"]))
            elif c.startswith("printf "):
                run = c.replace("PASTED_KEY", "fresh_key_0123456789")
                env = {"SEARCHAPI_API_KEY": None}
            elif not c.startswith("python3 "):
                run = "python3 scripts/searchapi.py " + c
            run = (run.replace("ENGINE key=value ...", "google q=coffee").replace("params ENGINE", "params google_maps")
                   .replace("pick ID", "pick " + last["id"]).replace("batch FILE", "batch " + batch))
            if run.endswith("searchapi.py setup"):
                run += " < /dev/null"                       # the hidden prompt needs a terminal: expect the clear exit 2
            code, out, err = self.sh(run, env if c.startswith("printf ") else None)
            want = 2 if run.endswith("< /dev/null") else 0
            self.assertEqual(code, want, "%s\n%s%s" % (run, out, err))
            if run.startswith("python3"):
                self.assertTrue(json.loads(out.split("\n")[0]).get("ok", True) or want, run)
            ran.append(run)
        print("\n  ran %d SKILL.md commands against the mock:\n    " % len(ran) + "\n    ".join(ran))


class Mapping(unittest.TestCase):
    """Every SearchApi param in references/mapping.md exists in that engine's OpenAPI file."""
    path = os.path.join(ROOT, "skills", "searchapi-migrate", "references", "mapping.md")

    def spec_params(self, engine):
        with open(os.path.join(FIX, "openapi", engine + ".yaml"), encoding="utf-8") as f:
            return {p.get("name") for p in sa.oas_params(f.read()) if isinstance(p, dict)}

    def test_params_exist(self):
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        checked, vendors = 0, re.findall(r"(?m)^## (SerpApi|Serper|DataForSEO|Bright Data)$", text)
        self.assertEqual(vendors, ["SerpApi", "Serper", "DataForSEO", "Bright Data"])
        for section in re.split(r"(?m)^## ", text):
            m = re.search(r"(?ms)^Params\n\n\|[^\n]*\| SearchApi engines \|[^\n]*\n\|[-|]+\|\n(.*?)(?:\n\n|\Z)", section)
            if not m:
                continue
            for row in m.group(1).split("\n"):
                cells = [c.strip() for c in row.strip("|").split("|")]
                ours = re.findall(r"`([a-z_]+)`", cells[1])
                engines = [e.strip() for e in cells[2].split(",") if e.strip()]
                for engine in engines:
                    have = self.spec_params(engine)
                    for p in ours:
                        self.assertIn(p, have, "%s: %s has no param %s (%s)" % (section.split(chr(10))[0], engine, p, row))
                        checked += 1
        print("\n  mapping: %d SearchApi params checked against OpenAPI" % checked)
        self.assertGreater(checked, 150)

    def test_engines_exist(self):
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        named = set()
        for table in re.findall(r"(?ms)^Engines\n\n\|[^\n]*\n\|[-|]+\|\n(.*?)(?:\n\n|\Z)", text):
            for row in table.split("\n"):
                named |= set(re.findall(r"`([a-z_]+)`", row.strip("|").split("|")[1]))
        named.discard("same")
        self.assertGreater(len(named), 8)
        for engine in named:
            self.assertTrue(os.path.isfile(os.path.join(FIX, "openapi", engine + ".yaml")), engine)


class MigrateSample(MockCase):
    """A SerpApi snippet, converted with the mapping, runs correctly against the mock."""

    def load(self, name):
        path = os.path.join(FIX, "migrate", name)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        mod_spec = importlib.util.spec_from_file_location(name[:-3], path)
        mod = importlib.util.module_from_spec(mod_spec)
        return text, mod, mod_spec

    def test_serpapi_snippet_converts(self):
        before, _, _ = self.load("serpapi_before.py")
        after, mod, mod_spec = self.load("searchapi_after.py")
        self.assertIn("serpapi", before)
        code = after.split('"""', 2)[2]
        for gone in ("serpapi", "SERPAPI", '"start"', '"api_key"', '"num"', "X-API-KEY"):
            self.assertNotIn(gone, code)
        os.environ.update(SEARCHAPI_BASE_URL=self.env["SEARCHAPI_BASE_URL"], SEARCHAPI_API_KEY=TEST_KEY)
        try:
            mod_spec.loader.exec_module(mod)
            rows = mod.top_links("coffee", page=2)
        finally:
            os.environ.pop("SEARCHAPI_BASE_URL", None)
            os.environ.pop("SEARCHAPI_API_KEY", None)
        method, url, query, headers = self.state.requests[-1]
        self.assertEqual(headers["authorization"], "Bearer " + TEST_KEY)
        self.assertNotIn(TEST_KEY, url)
        self.assertEqual(query, {"engine": "google", "q": "coffee", "location": "Austin,Texas,United States",
                                 "gl": "us", "hl": "en", "page": "2"})
        with open(os.path.join(FIX, "openapi", "google.yaml"), encoding="utf-8") as f:
            allowed = {p.get("name") for p in sa.oas_params(f.read()) if isinstance(p, dict)}
        self.assertLessEqual(set(query), allowed)
        fixture = mock_server.State().by_engine["google"]["organic_results"]
        self.assertEqual(rows, [(10 + r["position"], r["title"], r["link"]) for r in fixture])
        self.assertEqual(rows[0][0], 11)


class TrimEdges(unittest.TestCase):
    """Regression tests from the adversarial review (2026-10-09)."""

    def test_only_inline_images_and_base64_leave(self):
        doc = {"title": "data: the new oil", "thumb": "data:image/png;base64,iVBORw0KGgo=",
               "svg": "data:image/svg+xml,%3Csvg%3E", "blob": "data:application/octet-stream;base64,AAAA",
               "plain": "data:text/plain,hello", "words": ["data:", "url", "data:image/gif;base64,R0lG"]}
        t = sa.trim(doc)
        self.assertEqual(t, {"title": "data: the new oil", "plain": "data:text/plain,hello", "words": ["data:", "url"]})

    def test_citation_map_kept_when_markdown_has_no_markers(self):
        refs = [{"index": 0, "link": "https://a.example", "title": "A"}]
        d = {"markdown": "Answer text.", "reference_links": refs,
             "text_blocks": [{"type": "paragraph", "answer": "Answer text.", "reference_indexes": [0]}]}
        self.assertIn("text_blocks", sa.trim(d))                  # which paragraph cites what lives only there
        d["markdown"] = "Answer text [0](https://a.example)."
        self.assertNotIn("text_blocks", sa.trim(d))

    def test_reordered_words_are_not_a_duplicate(self):
        d = {"markdown": "It is not cheap.", "text_blocks": [{"type": "paragraph", "answer": "It is cheap."}]}
        self.assertIn("text_blocks", sa.trim(d))

    def test_reference_line_with_another_title_stays(self):
        refs = [{"index": 1, "link": "https://a.example", "title": "Real title"}]
        md = "Text.\n[[1] - Real title](https://a.example)\n[[2] - Other title](https://a.example)"
        t = sa.trim({"markdown": md, "reference_links": refs})
        self.assertEqual(t["markdown"], "Text.\n[[2] - Other title](https://a.example)")

    def test_count_prefers_organic_results(self):
        doc = {"local_results": [{"title": "x"}], "organic_results": [{"position": i} for i in range(10)]}
        self.assertEqual(sa.count(doc, sa.trim(doc)), 10)
        doc = {"organic_results": [], "related_searches": [{"query": "y"}]}
        self.assertEqual(sa.count(doc, sa.trim(doc)), 0)

    def test_overview_stays_small(self):
        big = {"by_date": {"2026-01-%04d" % i: {"v": i} for i in range(3000)},
               "rows": [{"f%d" % i: i} for i in range(3000)]}
        text = sa.overview(big)
        self.assertLess(len(text), sa.OVERVIEW_AT)
        self.assertIn("by_date: *.v int", text)

    def test_hint_names_the_main_results(self):
        doc = dict(VALID)["rest/rank-tracking/google_rank_tracking.json"]
        self.assertIn("organic_results:position", sa.hint(sa.trim(doc, top=True), "search_x"))

    def test_raw_file_is_the_bytes_received(self):
        raw = b'{"search_metadata": {"id": "search_raw1", "status": "Success"}, "price": 1.50, "n": 1e2}'
        old, sa.request = sa.request, lambda *a: (json.loads(raw), raw)
        tmp = tempfile.mkdtemp()
        os.environ["SEARCHAPI_DIR"] = tmp
        try:
            head, _, _ = sa.search("google", {"q": "x"}, TEST_KEY, "searchapi")
            with open(head["raw"], "rb") as f:
                self.assertEqual(f.read(), raw)
            self.assertEqual(stat.S_IMODE(os.stat(head["raw"]).st_mode), 0o600)
        finally:
            sa.request = old
            os.environ.pop("SEARCHAPI_DIR", None)
            shutil.rmtree(tmp, ignore_errors=True)


class Hardening(MockCase):
    """Regression tests from the adversarial review (2026-10-09): every one of these crashed or leaked before."""

    def assertNoTraceback(self, out, err):
        self.assertNotIn("Traceback", out + err)
        json.loads(out.split("\n", 1)[0])

    def test_key_with_a_newline_never_leaks(self):
        for env in ({"SEARCHAPI_API_KEY": "SECRETKEY_aaaa\nSECRETKEY_bbbb"},):
            code, head, out, err = self.run_cli("google", "q=coffee", env=env)
            self.assertEqual(code, 3)
            self.assertNotIn("SECRETKEY", out + err)
            self.assertNoTraceback(out, err)
        os.makedirs(os.path.join(self.home, ".config", "searchapi"))
        with open(os.path.join(self.home, ".config", "searchapi", "key"), "w") as f:
            f.write("SECRETKEY_old_1\nSECRETKEY_new_2\n")
        for args in (("google", "q=coffee"), ("credits",)):
            code, head, out, err = self.run_cli(*args, env={"SEARCHAPI_API_KEY": None})
            self.assertEqual(code, 3, out)
            self.assertNotIn("SECRETKEY", out + err)
        self.assertEqual(self.state.requests, [])

    def test_redirect_is_not_followed(self):
        code, head, out, err = self.run_cli("google", "q=mock:redirect")
        self.assertEqual(code, 4)
        self.assertIn("redirect", head["error"])
        self.assertFalse([r for r in self.state.requests if r[1].startswith("/_mock/stolen")])

    def test_hash_inside_a_batch_query_is_kept(self):
        path = os.path.join(self.tmp, "q.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# a comment line\ngoogle q=C# gl=us\ngoogle q=#fyp\ngoogle \"q=coffee\tshop\"\n")
        code, head, out, _ = self.run_cli("batch", path)
        self.assertEqual(code, 0, out)
        sent = [r[2].get("q") for r in self.state.requests if r[1].startswith("/api/v1/search")]
        self.assertEqual(sorted(sent), sorted(["C#", "#fyp", "coffee\tshop"]))
        self.assertTrue(all(len(row.split("\t")) == 6 for row in out.strip().split("\n")[2:]))   # no tab in a cell

    def test_batch_file_with_a_bom(self):
        path = os.path.join(self.tmp, "bom.txt")
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("google q=coffee\n")
        code, head, out, _ = self.run_cli("batch", path)
        self.assertEqual(code, 0, out)

    def test_unwritable_or_odd_data_dir_keeps_the_result(self):
        locked = os.path.join(self.tmp, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0o500)
        afile = os.path.join(self.tmp, "afile")
        open(afile, "w").close()
        try:
            for d in ([locked] if os.geteuid() != 0 else []) + [afile]:
                code, head, out, err = self.run_cli("google", "q=coffee", env={"SEARCHAPI_DIR": d})
                self.assertEqual(code, 0, out + err)
                self.assertIn("warning", head)
                self.assertIsNone(head["raw"])
                self.assertIn("organic_results", out)
        finally:
            os.chmod(locked, 0o700)

    def test_broken_response_retried_once_then_exit_5(self):
        code, head, out, err = self.run_cli("google", "q=mock:truncated")
        self.assertEqual(code, 5, out + err)
        self.assertNoTraceback(out, err)
        self.assertEqual(len(self.state.requests), 2)

    def test_non_utf8_stdout(self):
        code, head, out, err = self.run_cli("chatgpt", "q=best serp api", "--fields", "markdown",
                                            env={"PYTHONIOENCODING": "cp1252"}, isolated=False)
        self.assertEqual(code, 0, err)
        self.assertNoTraceback(out, err)
        md = json.loads(out.split("\n", 1)[1])["markdown"]
        self.assertTrue(any(ord(c) > 255 for c in md))          # the star ratings survive as JSON escapes

    def test_odd_retry_after_values(self):
        for ra in ("-1", "nan", "junk"):
            code, head, out, err = self.run_cli("google", "q=mock:503,ra=%s" % ra)
            self.assertEqual(code, 0, out + err)

    def test_cloudflare_5xx_is_retried(self):
        code, head, out, _ = self.run_cli("google", "q=mock:520")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.state.requests), 2)

    def test_deadline_stops_slow_retries(self):
        old = sa.DEADLINE
        sa.DEADLINE = 3
        os.environ["SEARCHAPI_BASE_URL"] = self.env["SEARCHAPI_BASE_URL"]
        try:
            t = time.time()
            with self.assertRaises(sa.ApiError) as cm:
                sa.request("/api/v1/search", {"engine": "google", "q": "mock:503,503,ra=2"}, TEST_KEY, "searchapi")
            self.assertEqual(cm.exception.code, 4)
            self.assertLess(time.time() - t, 2)                   # no 2 s wait it could not afford
            self.assertEqual(len(self.state.requests), 1)
        finally:
            sa.DEADLINE = old
            os.environ.pop("SEARCHAPI_BASE_URL", None)

    def test_plan_error_in_a_200_is_exit_3(self):
        code, head, _, _ = self.run_cli("google", "q=mock:plan200")
        self.assertEqual(code, 3)

    def test_ids_missing_still_give_distinct_raw_files(self):
        raws = {self.run_cli("google", "q=mock:noid,n%d" % i)[1]["raw"] for i in range(3)}
        self.assertEqual(len(raws), 3)

    def test_nan_in_a_response_prints_valid_json(self):
        code, head, out, _ = self.run_cli("google", "q=mock:nan", "--full")
        self.assertEqual(code, 0)
        body = json.loads(out.split("\n", 1)[1], parse_constant=lambda c: self.fail("bare " + c))
        self.assertIsNone(body["odd"])

    def test_pick_on_bad_files(self):
        bad = os.path.join(self.tmp, "bad.json")
        for content, want in (("{not json", 2), ("[1, 2]", 2), ('{"search_metadata": "oops", "x": [1]}', 0),
                              ('{"error": "Invalid API key."}', 4)):
            with open(bad, "w") as f:
                f.write(content)
            code, head, out, err = self.run_cli("pick", bad, "--fields", "x")
            self.assertEqual(code, want, content)
            self.assertNoTraceback(out, err)

    def test_pick_too_big_gives_an_overview(self):
        code, head, _, _ = self.run_cli("google_rank_tracking", "q=best serp api")
        self.assertIn("organic_results:position", head["next"])
        code, head2, out, _ = self.run_cli("pick", head["id"], "--fields", "organic_results")
        self.assertEqual(code, 0)
        self.assertTrue(head2["overview"])
        self.assertLess(len(out), sa.OVERVIEW_AT)

    def test_engine_names_are_exact(self):
        for args in (("google\n", "q=x"), ("params", "google\n")):
            code, head, out, err = self.run_cli(*args)
            self.assertEqual(code, 2, args)
            self.assertNoTraceback(out, err)
        self.assertEqual(self.state.requests, [])

    def test_params_ignores_a_same_named_file(self):
        with open(os.path.join(self.tmp, "google"), "w") as f:
            f.write("not a spec")
        code, head, _, _ = self.run_cli("params", "google", cwd=self.tmp)
        self.assertEqual(code, 0)
        self.assertGreater(head["params"], 10)

    def test_setup_replaces_a_symlink_and_a_loose_file(self):
        folder = os.path.join(self.home, ".config", "searchapi")
        os.makedirs(folder)
        target = os.path.join(self.tmp, "elsewhere")
        with open(target, "w") as f:
            f.write("keep me")
        os.symlink(target, os.path.join(folder, "key"))
        key = "fresh_key_9876543210zyxw"
        code, _, out, _ = self.run_cli("setup", "--key-stdin", stdin=key, env={"SEARCHAPI_API_KEY": None})
        self.assertEqual(code, 0, out)
        with open(target) as f:
            self.assertEqual(f.read(), "keep me")
        path = os.path.join(folder, "key")
        self.assertFalse(os.path.islink(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_unreadable_key_file(self):
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        folder = os.path.join(self.home, ".config", "searchapi")
        os.makedirs(folder)
        path = os.path.join(folder, "key")
        with open(path, "w") as f:
            f.write("some_key_123456789")
        os.chmod(path, 0)
        code, head, out, err = self.run_cli("google", "q=x", env={"SEARCHAPI_API_KEY": None})
        self.assertEqual(code, 3)
        self.assertIn("permissions", head["error"])

    def test_bad_flags_give_a_json_status(self):
        for args in ((), ("google", "--fields"), ("google", "q=x", "--f", "a"), ("google", "q=x", "--full", "--fields", "a"),
                     ("google", "q=a", "q=b"), ("google", "API_KEY=abc"), ("batch", "x.txt", "--full")):
            code, head, out, err = self.run_cli(*args)
            self.assertEqual(code, 2, args)
            self.assertFalse(head["ok"], args)
        self.assertEqual(self.state.requests, [])

    def test_batch_refuses_when_credits_are_short(self):
        path = os.path.join(self.tmp, "q.txt")
        with open(path, "w") as f:
            f.write("google q=a\ngoogle q=b\ngoogle q=c\n")
        code, head, _, _ = self.run_cli("batch", path, env={"SEARCHAPI_API_KEY": "low_key_123456"})
        self.assertEqual(code, 3)
        self.assertEqual((head["planned"], head["credits"]), (3, 2))
        self.assertEqual([r[1] for r in self.state.requests], ["/api/v1/me"])

    def test_broken_pipe_is_quiet(self):
        e = {k: v for k, v in self.env.items() if v is not None}
        r = subprocess.run("%s -I %s google_rank_tracking q=x --full | head -c 50" % (shlex.quote(sys.executable), shlex.quote(SCRIPT)),
                           shell=True, capture_output=True, text=True, env=e, timeout=60)
        self.assertNotIn("Traceback", r.stderr)


class ReleaseInProcess(unittest.TestCase):
    """Regression tests from the release test (2026-10-12), in process. Each one failed on the old script."""

    def test_trim_keeps_symbols_numbers_and_renumbered_reference_lines(self):          # L13
        marks = {"markdown": "Fast: yes", "text_blocks": [{"type": "paragraph", "answer": "Fast: yes"},
                                                          {"type": "paragraph", "answer": "\u2713 \u2713"}]}
        self.assertIn("text_blocks", sa.trim(marks))                  # the check marks live only in text_blocks
        table = {"markdown": "Plan prices", "text_blocks": [{"type": "table", "headers": ["Plan prices"], "rows": [[19.99]]}]}
        self.assertIn("text_blocks", sa.trim(table))                  # so does the number
        table["markdown"] = "Plan prices: 19.99"
        self.assertNotIn("text_blocks", sa.trim(table))               # deduped once markdown has it all
        refs = [{"index": 1, "link": "https://a.example", "title": "A"}]
        md = "Text [1](https://a.example).\n[[1] - A](https://a.example)\n[[5] - A](https://a.example)"
        self.assertEqual(sa.trim({"markdown": md, "reference_links": refs})["markdown"],
                         "Text [1](https://a.example).\n[[5] - A](https://a.example)")

    def test_trim_survives_malformed_reference_links(self):                             # N1
        md = "Answer.\n[[1] - A](https://a.example)"
        for refs in (5, True, [{"link": ["a"]}, {"link": {"u": 1}}, 7, "x"]):
            self.assertEqual(sa.trim({"markdown": md, "reference_links": refs})["markdown"], md, refs)

    def test_text_starting_data_image_is_kept(self):                                     # N7
        doc = {"a": "data:image/ files are big", "b": "data:image/png;base64,iVBORw0KGgo=",
               "c": "data:image/svg+xml,%3Csvg%3E", "d": ["data:image/ is a prefix", "data:image/webp;base64,UklG"]}
        self.assertEqual(sa.trim(doc), {"a": "data:image/ files are big", "d": ["data:image/ is a prefix"]})

    def test_fields_take_the_star_paths_the_overview_prints(self):                      # L12
        body = {"by_date": {"2026-01-%02d" % i: {"v": i, "w": -i} for i in range(1, 51)}}
        self.assertIn("by_date: *.v int", sa.overview(body))
        out, missing = sa.pick(body, "by_date:*.v")
        self.assertEqual(missing, [])
        self.assertEqual(out, {"by_date": {k: {"v": v["v"]} for k, v in body["by_date"].items()}})
        out, _ = sa.pick(body, "by_date:*.v;by_date:2026-01-01.w")
        self.assertEqual(out["by_date"]["2026-01-01"], {"v": 1, "w": -1})

    def test_next_hint_offers_the_fields_agents_need(self):                             # item 3, L10
        cases = [  # fixture, main list, fields the hint must offer, fields it must not
            ("flight-price-monitor/google_flights_round_trip.json", "other_flights",
             {"price", "flights.airline", "flights.flight_number", "departure_token"}, {"airline_logo"}),
            ("ecom-price-monitoring/google_shopping.json", "shopping_results",
             {"title", "price", "rating"}, {"prds", "product_link", "offers_link", "thumbnail"}),
            ("ecom-price-monitoring/amazon_search.json", "organic_results", {"title", "price", "asin"}, {"thumbnail"}),
            ("local-leads-scraper/google_maps_page1.json", "local_results",
             {"title", "rating", "website", "place_id"}, {"ludocid", "kgmid", "data_id"}),
            ("deep-research/youtube.json", "videos", {"title", "link", "channel.title", "id"}, {"key_moments.title"}),
            ("rank-tracking/google_rank_tracking.json", "organic_results", {"position", "title", "link"}, {"displayed_link"}),
        ]
        for name, rows, want, never in cases:
            body = sa.trim(dict(VALID)["rest/" + name], top=True)
            words = shlex.split(sa.hint(body, "search_x"))
            self.assertEqual(words[:5], ["python3", SCRIPT, "pick", "search_x", "--fields"], name)
            block, _, fields = words[5].partition(":")
            fields = fields.split(",")
            self.assertEqual(block, rows, name)
            self.assertTrue(want <= set(fields) and not never & set(fields) and len(fields) <= 7, "%s: %s" % (name, fields))
            self.assertLessEqual(len(sa.dumps(sa.pick(body, words[5])[0])), sa.OVERVIEW_AT, name)   # prints in full

    def test_next_hint_takes_the_main_list_in_response_order(self):                     # live google_shopping, 2026-10-12
        body = sa.trim(dict(VALID)["rest/ecom-price-monitoring/google_shopping.json"], top=True)
        body["categorized_shopping_results"] = [{"title": "Silver headphones", "shopping_results": [{"position": 1, "title": "x"}]}]
        words = shlex.split(sa.hint(body, "search_x"))   # live order: shopping_results, then categorized_shopping_results
        block, _, fields = words[5].partition(":")
        self.assertEqual(block, "shopping_results")
        self.assertIn("price", fields.split(","))

    def test_retry_needs_time_for_a_whole_attempt(self):                                 # L1
        clock, calls = [1000.0], []

        class Clock:
            time = staticmethod(lambda: clock[0])
            sleep = staticmethod(lambda s: clock.__setitem__(0, clock[0] + s))

        class Slow503:
            def open(self, req, timeout=None):
                calls.append(req.full_url)
                clock[0] += 95                          # the API's own 503 comes after about 90 s
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {"Retry-After": "0"}, io.BytesIO(b'{"error": "busy"}'))
        with environ(SEARCHAPI_BASE_URL=None), patched(sa, time=Clock, OPENER=Slow503(), LOCAL_OPENER=Slow503()):
            with self.assertRaises(sa.ApiError) as cm:
                sa.request("/api/v1/search", {"engine": "google", "q": "x"}, TEST_KEY, "searchapi")
        self.assertEqual((cm.exception.code, len(calls)), (4, 1))   # a second 95 s try would end past the deadline

    def test_a_bad_url_is_not_retried(self):                                             # N9
        calls = []

        class BadUrl:
            def open(self, req, timeout=None):
                calls.append(req.full_url)
                raise http.client.InvalidURL("nonnumeric port: 'abc'")
        with environ(SEARCHAPI_BASE_URL=None), patched(sa, OPENER=BadUrl(), LOCAL_OPENER=BadUrl()):
            with self.assertRaises(sa.ApiError) as cm:
                sa.request("/api/v1/search", {"engine": "google", "q": "x"}, TEST_KEY, "searchapi")
        self.assertEqual((cm.exception.code, len(calls)), (2, 1))

    def test_raw_files_are_opened_binary(self):                                         # Windows O_BINARY
        bit, real, flags, tmp = 1 << 29, os.open, [], tempfile.mkdtemp()

        def spy(path, f, *a):
            flags.append(f)
            return real(path, f & ~bit, *a)
        try:   # os.O_BINARY exists on Windows only: a stand-in bit shows the script asks for it
            with environ(SEARCHAPI_DIR=tmp), patched(os, O_BINARY=bit, open=spy):
                sa.save_raw("google", "search_bin", b'{"a": 1}\n')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertTrue(flags and all(f & bit for f in flags), flags)


class Release(MockCase):
    """Regression tests from the release test (2026-10-12), through the CLI. Each one failed on the old script."""

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_locations(self):                                                            # item 1
        code, head, out, _ = self.run_cli("locations", "brooklyn", "limit=3")
        self.assertEqual(code, 0, out)
        self.assertEqual(head, {"ok": True, "count": 3})
        lines = out.strip().split("\n")[1:]
        self.assertEqual(lines[0], "Brooklyn,New York,United States\tBorough\tUS\t4130000")
        self.assertTrue(len(lines) == 3 and all(len(ln.split("\t")) == 4 for ln in lines), lines)
        method, url, query, headers = self.state.requests[-1]
        self.assertEqual((url.split("?")[0], query), ("/api/v1/locations", {"q": "brooklyn", "limit": "3"}))
        self.assertEqual(headers["authorization"], "Bearer " + TEST_KEY)
        self.assertTrue(headers["x-searchapi-client"].startswith("searchapi-skills/"))
        code, head, out, _ = self.run_cli("locations", "leeds")                   # the SKILL.md example
        self.assertEqual(out.split("\n")[1].split("\t")[0], "Leeds,England,United Kingdom")
        self.run_cli("locations", "new", "york")
        self.assertEqual(self.state.requests[-1][2], {"q": "new york"})
        code, head, out, _ = self.run_cli("locations", "brooklyn", "--full")
        self.assertEqual(json.loads(out.split("\n", 1)[1])[0]["lat"], 40.6781784)
        code, head, out, _ = self.run_cli("locations", "zzzz")
        self.assertEqual((code, out.strip()), (0, '{"ok":true,"count":0}'))
        for args in (("locations",), ("locations", "x", "--fields", "a"), ("locations", "x", "q=y")):
            self.assertEqual(self.run_cli(*args)[0], 2, args)
        self.assertEqual(self.run_cli("locations", "leeds", env={"SEARCHAPI_API_KEY": "bad_key_123456"})[0], 3)

    def test_location_used_in_the_status_line(self):                                    # item 2
        code, head, out, _ = self.run_cli("google", "q=coffee", "gl=uk", "location=London")
        self.assertEqual(code, 0, out)
        self.assertEqual(head["location_used"], "London TV Region,England,United Kingdom")
        for loc in ("Leeds,England,United Kingdom", "brooklyn, new york, united states"):
            code, head, out, _ = self.run_cli("google", "q=coffee", "location=" + loc)
            self.assertEqual(code, 0, out)
            self.assertNotIn("location_used", head, loc)              # the same place, written another way
        code, head, _, _ = self.run_cli("google", "q=coffee", "location=leeds, uk")
        self.assertEqual((code, head["error"]), (2, "Location was not found. Double check location parameter."))
        sid = self.run_cli("google", "q=tea", "location=London")[1]["id"]
        code, head, _, _ = self.run_cli("pick", sid, "--fields", "organic_results:link")
        self.assertEqual(head["location_used"], "London TV Region,England,United Kingdom")   # read from the raw file
        code, head, out, _ = self.run_cli("batch", self.write("loc.txt", "google q=coffee location=London\n"))
        self.assertIn("ok location_used=London TV Region,England,United Kingdom", out.split("\n")[3])

    def test_batch_over_25_says_when_to_ask(self):                                      # item 4, L10
        path = self.write("big.txt", "\n".join("google q=coffee page=%d" % i for i in range(30)))
        code, head, _, _ = self.run_cli("batch", path)
        self.assertEqual(code, 2)
        self.assertTrue(head["error"].startswith("30 calls planned, 999,517 credits left. If the user listed these "
                                                 "searches, tell them this count and balance, then rerun with --yes: "))
        self.assertTrue(head["error"].endswith(". Otherwise ask the user first."))
        rerun = re.search(r"rerun with --yes: (.+?)\. Otherwise", head["error"]).group(1)
        self.assertEqual(shlex.split(rerun), ["python3", SCRIPT, "batch", path, "--yes"])

    def test_mock_has_a_spec_for_every_engine_the_skill_names(self):                    # item 5
        with open(os.path.join(ROOT, "skills", "searchapi", "SKILL.md"), encoding="utf-8") as f:
            text = re.sub(r"(?ms)^## Connect\n.*?(?=^## )", "", f.read())   # MCP tool names, not engines
        family = (r"(google|bing|youtube|amazon|airbnb|meta|tiktok|chatgpt|gemini|perplexity|duckduckgo|yahoo|yandex|"
                  r"baidu|naver|apple|walmart|ebay|tripadvisor|booking|zillow|instagram|facebook|linkedin|reddit)(_|$)")
        engines = {t for t in re.findall(r"`([a-z0-9_]+)`", text) if re.match(family, t) and not t.endswith("_token")}
        self.assertGreater(len(engines), 20)
        self.assertEqual(sorted(e for e in engines if not os.path.isfile(os.path.join(FIX, "openapi", e + ".yaml"))), [])

    def test_setup_keeps_the_key_when_the_api_is_down(self):                            # M1
        key = "down_key_0123456789abcdef"
        code, head, out, err = self.run_cli("setup", "--key-stdin", stdin=key, env={"SEARCHAPI_API_KEY": None})
        self.assertEqual(code, 4, out)
        self.assertIn("not checked", head["error"])
        self.assertNotIn("did not accept", head["error"])
        with open(os.path.join(self.home, ".config", "searchapi", "key")) as f:
            self.assertEqual(f.read().strip(), key)
        self.assertNotIn(key, out + err)

    def test_an_empty_or_short_key_keeps_messages_readable(self):                       # M2
        code, head, _, _ = self.run_cli("setup", "--key-stdin", stdin="", env={"SEARCHAPI_API_KEY": None})
        self.assertEqual(code, 2)
        self.assertNotIn("[key]", head["error"])
        self.assertIn("Sign up: " + sa.SIGN_UP, head["error"])
        code, head, _, _ = self.run_cli("google", "q=coffee", env={"SEARCHAPI_API_KEY": "x"})
        self.assertEqual(code, 3)
        self.assertIn("has spaces, extra lines or odd characters", head["error"])

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX folder ownership")
    def test_default_raw_folder_must_be_private(self):                                  # M3
        os.makedirs(os.path.join(self.tmp, "planted"))
        for kind in ("world-writable", "symlink"):
            tmpdir = os.path.join(self.tmp, kind)
            shared = os.path.join(tmpdir, "searchapi")
            os.makedirs(tmpdir)
            if kind == "symlink":
                os.symlink(os.path.join(self.tmp, "planted"), shared)
            else:
                os.makedirs(shared)
                os.chmod(shared, 0o777)
            code, head, out, _ = self.run_cli("google", "q=coffee", env={"SEARCHAPI_DIR": None, "TMPDIR": tmpdir})
            self.assertEqual(code, 0, out)
            own = os.path.join(tmpdir, "searchapi-%d" % os.getuid())
            self.assertEqual(os.path.dirname(head["raw"]), own, kind)
            self.assertEqual(stat.S_IMODE(os.stat(own).st_mode), 0o700)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "planted")), [])

    def test_batch_reads_stdin_as_utf8(self):                                           # M4
        e = dict(self.env, PYTHONIOENCODING="cp1252")                     # a Windows console code page
        r = subprocess.run([sys.executable, "-s", "-B", SCRIPT, "batch", "-"], input="google_light q=caf\u00e9\n".encode("utf-8"),
                           capture_output=True, env=e, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([x[2].get("q") for x in self.state.requests if x[1].startswith("/api/v1/search")], ["caf\u00e9"])

    def test_batch_plan_error_fails_one_line_only(self):                                # M5
        slow = "".join("google q=mock:slow=0.3,n%d\n" % i for i in range(10))
        code, head, out, _ = self.run_cli("batch", self.write("plan.txt", "google q=mock:plan200\n" + slow))
        status = [r.split("\t")[3] for r in out.strip().split("\n")[3:]]
        self.assertEqual(code, 3)
        self.assertTrue(status[0].startswith("error: ") and status[0] != "error: skipped", status[0])
        self.assertEqual(status[1:], ["ok"] * 10)                         # the other lines still ran
        code, head, out, _ = self.run_cli("batch", self.write("key.txt", "google q=mock:401\n" + slow))
        self.assertEqual(code, 3)
        self.assertIn("error: skipped", [r.split("\t")[3] for r in out.strip().split("\n")[3:]])   # a 401 still stops

    def test_batch_stops_when_the_balance_check_fails(self):                            # L2
        path = self.write("q.txt", "google q=coffee\ngoogle q=tea\n")
        code, head, out, _ = self.run_cli("batch", path, env={"SEARCHAPI_BASE_URL": "http://127.0.0.1:%d" % free_port()})
        self.assertEqual(code, 5)
        self.assertFalse(head["ok"])
        self.assertEqual(out.strip().count("\n"), 0)                     # one status line, no plan, no table

    def test_a_400_is_bad_input_whatever_it_says(self):                                 # L3
        code, head, _, _ = self.run_cli("google", "q=mock:400,msg=Your plan allows 10 pages")
        self.assertEqual((code, head["http"]), (2, 400))

    def test_batch_table_is_redacted(self):                                             # L4
        code, head, out, err = self.run_cli("batch", self.write("q.txt", "google q=mock:echokey\n"))
        self.assertEqual(code, 2)
        self.assertNotIn(TEST_KEY, out + err)
        self.assertIn("[key]", out)

    def test_local_base_url_never_goes_through_a_proxy(self):                          # L5
        proxy, seen = mock_server.start()
        try:
            code, head, out, _ = self.run_cli("google", "q=coffee", env={"http_proxy": "http://127.0.0.1:%d" % proxy.server_port})
        finally:
            proxy.shutdown()
            proxy.server_close()
        self.assertEqual(seen.requests, [])                               # the key never went to the proxy
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state.requests[-1][3]["authorization"], "Bearer " + TEST_KEY)

    def test_params_on_an_unreadable_spec(self):                                        # L6
        for text in ("<html>Sign in to the Wi-Fi</html>", ""):
            code, head, _, _ = self.run_cli("params", self.write("bad.yaml", text))
            self.assertEqual(code, 4, text)
            self.assertIn("Could not read the OpenAPI file", head["error"])

    def test_big_response_without_a_raw_file_prints_in_full(self):                     # L7
        code, head, out, _ = self.run_cli("google_rank_tracking", "q=x", env={"SEARCHAPI_DIR": self.write("afile", "")})
        self.assertEqual(code, 0, out)
        self.assertIsNone(head["raw"])
        self.assertNotIn("overview", head)                                # an overview would point at a missing file
        self.assertEqual(len(json.loads(out.split("\n", 1)[1])["organic_results"]), head["count"])

    def test_picking_many_ids_shares_one_output_budget(self):                          # L8
        sid = self.run_cli("bing", "q=coffee")[1]["id"]
        code, _, out, _ = self.run_cli("pick", *[sid] * 6 + ["--fields", "organic_results"])
        self.assertEqual(code, 0)
        heads = [json.loads(ln) for ln in out.split("\n") if ln.startswith('{"ok"')]
        self.assertEqual(len(heads), 6)
        self.assertLessEqual(len(out), sa.OVERVIEW_AT + 1000)
        self.assertNotIn("overview", heads[0])
        self.assertTrue(heads[-1]["overview"])
        self.assertIn("fewer IDs per run", heads[-1]["next"])

    def test_trickling_body_meets_the_deadline(self):                                  # L9
        t = time.time()
        with environ(SEARCHAPI_BASE_URL=self.env["SEARCHAPI_BASE_URL"]), patched(sa, DEADLINE=1.5):
            with self.assertRaises(sa.ApiError) as cm:
                sa.request("/api/v1/search", {"engine": "google", "q": "mock:trickle"}, TEST_KEY, "searchapi")
        self.assertEqual(cm.exception.code, 5)
        self.assertLess(time.time() - t, 4)                                # the whole body takes 5 s

    def test_printed_commands_use_the_quoted_absolute_path(self):                      # L10
        folder = os.path.join(self.tmp, "my skills")
        os.makedirs(os.path.join(folder, "scripts"))
        copy = os.path.join(folder, "scripts", "searchapi.py")
        shutil.copy(SCRIPT, copy)
        e = {k: v for k, v in dict(self.env, SEARCHAPI_API_KEY=None).items() if v is not None}
        r = subprocess.run([sys.executable, "-I", "scripts/searchapi.py", "google", "q=x"], cwd=folder,
                           capture_output=True, text=True, env=e, timeout=60)
        printed = re.search(r"Run: (.+? setup) ", json.loads(r.stdout.split("\n")[0])["error"]).group(1)
        words = shlex.split(printed)
        self.assertEqual((words[0], os.path.realpath(words[1]), words[2:]), ("python3", os.path.realpath(copy), ["setup"]))
        r = subprocess.run(printed.replace("python3", shlex.quote(sys.executable), 1) + " < /dev/null", shell=True,
                           cwd=self.home, capture_output=True, text=True, env=e, timeout=60)
        self.assertIn("--key-stdin", json.loads(r.stdout.split("\n")[0])["error"])   # it runs from another folder

    def test_count_is_null_without_a_result_list(self):                                # L11
        code, head, _, _ = self.run_cli("google_trends", "q=coffee")
        self.assertEqual(code, 0)
        self.assertIsNone(head["count"])
        code, head, out, _ = self.run_cli("batch", self.write("t.txt", "google_trends q=coffee\n"))
        self.assertEqual(out.strip().split("\n")[3].split("\t")[4], "")

    def test_a_failed_setup_leaves_no_temp_file(self):                                 # L14
        folder = os.path.join(self.home, ".config", "searchapi")
        os.makedirs(os.path.join(folder, "key"))                         # a folder where the key file goes
        code, head, out, _ = self.run_cli("setup", "--key-stdin", stdin="fresh_key_9876543210zyxw",
                                          env={"SEARCHAPI_API_KEY": None})
        self.assertEqual(code, 2, out)
        self.assertEqual(os.listdir(folder), ["key"])                    # no key.PID.tmp holding the key

    def test_searchapi_dir_expands_home(self):                                         # N2
        code, head, out, _ = self.run_cli("google", "q=coffee", env={"SEARCHAPI_DIR": "~/raw"}, cwd=self.tmp)
        self.assertEqual(code, 0, out)
        self.assertEqual(os.path.dirname(head["raw"]), os.path.join(self.home, "raw"))

    def test_repeated_fields_add_up(self):                                             # N3
        code, head, out, _ = self.run_cli("google", "q=coffee", "--fields", "organic_results:position",
                                          "--fields", "search_information")
        self.assertEqual(code, 0, out)
        self.assertLessEqual({"organic_results", "search_information"}, set(json.loads(out.split("\n", 1)[1])))

    def test_base_url_must_be_http_on_localhost(self):                                 # N4
        for url in ("ftp://localhost:21", "file://localhost/etc/hosts", "http://[::1", "http://localhost:abc",
                    "http://127.0.0.1:%d/a b" % self.port):
            code, head, out, _ = self.run_cli("google", "q=x", env={"SEARCHAPI_BASE_URL": url})
            self.assertEqual(code, 2, url + out)
            self.assertNotIn("Unexpected", head["error"], url)
        self.assertEqual(self.state.requests, [])
        port = free_port()
        code, head, _, _ = self.run_cli("google", "q=x", env={"SEARCHAPI_BASE_URL": "http://127.0.0.1:%d" % port})
        self.assertEqual(code, 5)
        self.assertIn("Could not reach 127.0.0.1:%d" % port, head["error"])   # not www.searchapi.io

    @unittest.skipUnless(hasattr(os, "openpty") and sys.platform != "win32", "needs a pseudo-terminal")
    def test_key_stdin_typed_at_a_terminal_is_hidden(self):                            # N6
        import pty
        key = "typed_key_0123456789abcdef"
        env = {k: v for k, v in dict(self.env, SEARCHAPI_API_KEY=None).items() if v is not None}
        pid, fd = pty.fork()
        if pid == 0:                                                      # child: a user typing into setup --key-stdin
            os.execve(sys.executable, [sys.executable, "-I", SCRIPT, "setup", "--key-stdin"], env)
        out, sent, start = b"", False, time.time()
        while time.time() - start < 20:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    chunk = os.read(fd, 1024)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if not sent and (b"input hidden" in out or time.time() - start > 3):
                os.write(fd, key.encode() + b"\n\x04")                   # Ctrl-D ends a plain stdin read too
                sent = True
        os.waitpid(pid, 0)
        text = out.decode("utf-8", "replace")
        self.assertNotIn(key, text)                                       # not echoed
        self.assertIn("Connected", text)

    def test_usage_and_plural_wording(self):                                           # N8
        code, head, _, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertNotIn("usage: usage", head["error"].lower())
        code, head, _, _ = self.run_cli("batch", self.write("one.txt", "google q=coffee\n"),
                                        env={"SEARCHAPI_API_KEY": "zero_key_123456"})
        self.assertEqual(code, 3)
        self.assertIn("1 call planned but only 0 credits left", head["error"])


@unittest.skipUnless(hasattr(os, "openpty") and sys.platform != "win32", "needs a pseudo-terminal")
class Onboarding(MockCase):
    """No key anywhere: the error points to setup, the hidden prompt saves and checks the key, the search resumes."""

    def test_no_key_then_hidden_prompt_then_search(self):
        import pty
        code, head, _, _ = self.run_cli("google", "q=coffee", env={"SEARCHAPI_API_KEY": None})
        self.assertEqual(code, 3)
        self.assertIn("setup", head["error"])
        key = "typed_key_0123456789abcdef"
        env = {k: v for k, v in dict(self.env, SEARCHAPI_API_KEY=None).items() if v is not None}
        pid, fd = pty.fork()
        if pid == 0:                                  # child: the user's terminal
            os.execve(sys.executable, [sys.executable, "-I", SCRIPT, "setup"], env)
        out, sent = b"", False
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if not sent and b"input hidden" in out:
                os.write(fd, key.encode() + b"\n")
                sent = True
        _, status = os.waitpid(pid, 0)
        text = out.decode("utf-8", "replace")
        self.assertEqual(os.WEXITSTATUS(status), 0, text)
        self.assertNotIn(key, text)                   # the typed key never shows
        self.assertIn("Connected: 999,517 credits left", text)
        path = os.path.join(self.home, ".config", "searchapi", "key")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        code, head, _, _ = self.run_cli("google", "q=coffee", env={"SEARCHAPI_API_KEY": None})
        self.assertEqual(code, 0)
        self.assertEqual(self.state.requests[-1][3]["authorization"], "Bearer " + key)


class HermesCopy(MockCase):
    """hermes/ gets a script that takes the key only from the setup file: Hermes blocks skills that read a secret env var."""
    HERMES = os.path.join(ROOT, "hermes", "searchapi", "scripts", "searchapi.py")

    def run_hermes(self, *args):
        r = subprocess.run([sys.executable, "-I", self.HERMES] + list(args), capture_output=True, text=True,
                           env=self.env, timeout=60, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout

    def test_env_key_ignored_and_setup_file_used(self):
        code, out = self.run_hermes("credits")                 # SEARCHAPI_API_KEY is set in self.env
        self.assertEqual(code, 3, out)
        self.assertIn("No SearchApi key. Run: ", out)
        self.assertEqual(self.state.requests, [])
        folder = os.path.join(self.home, ".config", "searchapi")
        os.makedirs(folder)
        with open(os.path.join(folder, "key"), "w") as f:
            f.write(TEST_KEY)
        os.chmod(os.path.join(folder, "key"), 0o600)
        code, out = self.run_hermes("credits")
        self.assertEqual(code, 0, out)
        headers = self.state.requests[-1][3]
        self.assertEqual(headers["authorization"], "Bearer " + TEST_KEY)
        self.assertEqual(headers["x-searchapi-client"], "searchapi-skills/1.0.0 (hermes; searchapi)")

    def test_env_key_saved_once_with_the_skill_md_pipe(self):
        with open(os.path.join(ROOT, "hermes", "searchapi", "SKILL.md"), encoding="utf-8") as f:
            pipe = re.search(r"`(printf '%s' \"\$SEARCHAPI_API_KEY\" \| python3 scripts/searchapi\.py setup --key-stdin)`",
                             f.read()).group(1)
        cmd = pipe.replace("python3", shlex.quote(sys.executable) + " -I", 1)
        r = subprocess.run(["/bin/sh", "-c", cmd], cwd=os.path.dirname(os.path.dirname(self.HERMES)), env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(TEST_KEY, r.stdout + r.stderr)
        path = os.path.join(self.home, ".config", "searchapi", "key")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        code, out = self.run_hermes("credits")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state.requests[-1][3]["authorization"], "Bearer " + TEST_KEY)


if __name__ == "__main__":
    unittest.main()
