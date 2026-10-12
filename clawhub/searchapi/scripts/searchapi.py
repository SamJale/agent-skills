#!/usr/bin/env python3
"""SearchApi.io for agents: call, check, trim. Python 3.8+, standard library only.

  searchapi.py ENGINE key=value ... [--fields SPEC] [--full]   one search
  searchapi.py pick ID|RAW ... --fields SPEC     re-read saved responses (no call, no credit)
  searchapi.py params ENGINE [PARAM]             params from the engine's live OpenAPI file
  searchapi.py locations QUERY [limit=N]         exact values for location= (free)
  searchapi.py batch FILE [--yes]                one "ENGINE key=value ..." per line, 5 at a time
  searchapi.py credits                           credits left and searches this hour
  searchapi.py setup [--key-stdin]               save your key (hidden prompt) and check it

Key: SEARCHAPI_API_KEY, else ~/.config/searchapi/key. It is sent only in the Authorization
header. Line 1 of every output is a JSON status line: ignored = params the API dropped,
changed = values it used instead of yours, location_used = the place it searched from when
that is not your location. Raw responses are saved untouched in $SEARCHAPI_DIR (default: a
private searchapi/ folder in the temp folder) as ENGINE-ID.json.
SPEC is block:field,sub.field;block2 (example: organic_results:position,title,link;pagination);
* stands for every key of a map (by_date:*.value).
One call takes at most about 2 minutes, retries included.
Exit codes: 0 ok (empty too), 2 bad input or 400, 3 key, credits or plan, 4 API error,
5 network or timeout.
"""
import argparse
import concurrent.futures
import getpass
import glob
import http.client
import ipaddress
import json
import os
import random
import re
import shlex
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
BASE = "https://www.searchapi.io"
DOCS = BASE + "/docs/google"   # the docs home; the bare /docs path is a 404
TIMEOUT = 100        # seconds per attempt; the API answers with its own free, retryable 503 at about 90 s
DEADLINE = 115       # seconds per call, retries included, so one call fits a 2-minute agent shell timeout
RETRIES = 2          # extra tries after a 429 or 5xx (failed calls are not billed)
BACKOFF = (2, 6)     # seconds before retry 1 and 2, times a 0.5-1.5 jitter, unless Retry-After says
MAX_WAIT = 60        # cap on any Retry-After wait, in seconds
CONCURRENCY = 5      # batch calls in flight; the per-plan limit is not published yet
ASK_ABOVE = 25       # batch asks the user before running more calls than this
OVERVIEW_AT = 25000  # chars; bigger output prints an overview (Claude Code shows 30,000 inline)
MAP_KEYS = 40        # a dict with more keys than this is data (dates, ids), shown as one path: *
META = ("id", "status", "json_url", "html_url")
KEY_FILE = os.path.join("~", ".config", "searchapi", "key")
SIGN_UP = "https://www.searchapi.io/users/sign_up?utm_source=agent-skill&utm_medium=agent-skill&utm_campaign=searchapi-skill&utm_content=script"
EGRESS = ("If this shell blocks network access, use the SearchApi MCP server instead, or allow "
          "www.searchapi.io (Codex: codex -c sandbox_workspace_write.network_access=true).")
CERTS = ("Python cannot verify SSL certificates. On macOS run 'Install Certificates.command' in "
         "your Python folder, or use the SearchApi MCP server.")
HARNESSES = (("CLAUDECODE", "claude-code"), ("CODEX_THREAD_ID", "codex"), ("CODEX_SANDBOX", "codex"),
             ("CURSOR_AGENT", "cursor"), ("CURSOR_SANDBOX", "cursor"), ("GEMINI_CLI", "gemini-cli"),
             ("COPILOT_CLI", "copilot"), ("COPILOT_AGENT_SESSION_ID", "copilot"), ("KIRO_AGENT_PATH", "kiro"),
             ("GROK_AGENT", "grok"), ("AMP_CURRENT_THREAD_ID", "amp"), ("OPENCODE", "opencode"))
DATA_URI = re.compile(r"data:(?:image/[\w.+-]+[;,]|[^,\s]*;base64,)", re.I)   # inline images and base64 payloads only
REF_LINE = re.compile(r"^\[\[(\d{1,9})\] - (.*)\]\((\S*)\)[ \t]*$")
MARK = re.compile(r"\[(\d{1,9})\]\(")                            # an inline citation: [3](link)
HINT = ("position", "title", "name", "link", "website", "price", "extracted_price", "total_price", "rating",
        "reviews", "date", "iso_date", "source", "snippet")      # fields a pick hint offers first, best first
HINT_IN_DICT, HINT_IN_LIST = ("title", "name", "total_price"), ("airline", "flight_number")   # channel.title, flights.airline
NOT_HINT = re.compile(r"logo|thumbnail|favicon|image|^(prds|ludocid|kgmid|data_c?id|displayed_link)$")
GOOGLE_URL = re.compile(r"https?://(www\.)?google\.")
LOCATION_FIELDS = ("canonical_name", "target_type", "country_code", "reach")
LINK = re.compile(r"\]\([^)\s]*\)")
WORD = re.compile(r"[^\W_]+")
NAME = re.compile(r"[a-z0-9_]+")
KEY_OK = re.compile(r"[\x21-\x7e]{8,256}")
PLAN = re.compile(r"\b(plan|subscription|credits?|quota)\b", re.I)
SECRETS = []         # keys loaded in this run: scrubbed from every error message
ASCII = False        # True when stdout cannot take UTF-8 (Windows pipes): JSON is then ASCII-escaped


class ApiError(Exception):
    def __init__(self, code, msg, **extra):
        Exception.__init__(self, msg)
        self.code, self.msg, self.extra = code, str(msg)[:2000], extra


def redact(s):
    s = str(s)
    for k in SECRETS:
        s = s.replace(k, "[key]")
    return s


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=ASCII)


def emit(head, body=""):
    print(dumps(head) + ("\n" + body if body else ""))
    sys.stdout.flush()


def fail(code, msg, **extra):
    emit(dict(ok=False, error=redact(msg)[:2000], **{k: redact(v) if isinstance(v, str) else v for k, v in extra.items()}))
    sys.exit(code)


def command(*args):
    """A command to paste in any folder: python3, this script's absolute path (shell-quoted), then args."""
    return " ".join(("python3", shlex.quote(os.path.abspath(__file__))) + args)


def plural(n, word):
    return "%s %s%s" % (fmt(n), word, "" if n == 1 else "s")


# ------------------------------------------------------------------ files

def private(d):
    """A real folder (not a link) of ours that no other user can write to. Always true off POSIX."""
    if not hasattr(os, "getuid"):
        return True
    st = os.lstat(d)
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022


def data_dir():
    """$SEARCHAPI_DIR, else a private searchapi/ in the temp folder (searchapi-UID if someone else has that one)."""
    d = os.environ.get("SEARCHAPI_DIR")
    if d:
        d = os.path.expanduser(d)
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d
    d = os.path.join(tempfile.gettempdir(), "searchapi")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        if os.access(d, os.W_OK) and private(d):
            return d
    except OSError:
        pass
    d += "-%s" % (os.getuid() if hasattr(os, "getuid") else re.sub(r"\W", "", getpass.getuser()))
    os.makedirs(d, mode=0o700, exist_ok=True)
    if not private(d):                                       # planted by another user: never read or write it
        raise OSError("%s is not a private folder" % d)
    return d


def save_raw(engine, sid, raw):
    """Write the response bytes exactly as received. Returns the path; raises OSError."""
    name = re.sub(r"[^\w.-]", "_", str(sid)) if sid else "%d-%s" % (time.time() * 1000, os.urandom(3).hex())
    path = os.path.join(data_dir(), "%s-%s.json" % (engine, name))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return path


# ------------------------------------------------------------------ key, client tag, HTTP

def base_url():
    """SEARCHAPI_BASE_URL is honoured only on this machine (tests and mocks), so the key never leaves for another host."""
    url = os.environ.get("SEARCHAPI_BASE_URL")
    if not url:
        return BASE
    try:
        u = urllib.parse.urlsplit(url)
        host, _ = u.hostname or "", u.port                   # .port raises ValueError on a bad port
        local = u.scheme in ("http", "https") and (host == "localhost" or ipaddress.ip_address(host).is_loopback)
    except ValueError:
        local = False
    if not local or re.search(r"[\x00-\x20\x7f]", url):
        fail(2, "SEARCHAPI_BASE_URL must be an http(s) URL on localhost. Unset it to use www.searchapi.io.")
    return url.rstrip("/")


def load_key():
    key, src = (os.environ.get("SEARCHAPI_API_KEY") or "").strip(), "SEARCHAPI_API_KEY"
    if not key:
        src = KEY_FILE
        try:
            with open(os.path.expanduser(KEY_FILE), "rb") as f:
                key = f.read().decode("utf-8").strip()
        except FileNotFoundError:
            key = ""
        except PermissionError:
            raise ApiError(3, "Cannot read %s (file permissions). Run: %s" % (KEY_FILE, command("setup")))
        except (OSError, UnicodeDecodeError):
            raise ApiError(3, "%s is not a readable key file. Run: %s" % (KEY_FILE, command("setup")))
    if not key:
        raise ApiError(3, "No SearchApi key. Run: %s (or use the SearchApi MCP server). "
                          "No account yet? Sign up: %s" % (command("setup"), SIGN_UP))
    SECRETS.extend(p for p in [key] + key.split() if len(p) >= 8)   # a short one would scramble every message
    if not KEY_OK.fullmatch(key):
        raise ApiError(3, "The key in %s has spaces, extra lines or odd characters. Run: %s" % (src, command("setup")))
    return key


def client(skill):
    harness = next((name for var, name in HARNESSES if os.environ.get(var)), None)
    agent = re.sub(r"[^a-z0-9_.-]", "", (os.environ.get("AI_AGENT") or os.environ.get("AGENT") or "").lower())[:30]
    harness = harness or (agent if agent not in ("", "1", "true") else "unknown")
    return "searchapi-skills/%s (%s; %s)" % (VERSION, harness, skill)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None      # never follow one: the key must not travel to another URL


OPENER = urllib.request.build_opener(NoRedirect)
LOCAL_OPENER = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))   # http_proxy would see the key


def backoff(retry_after, attempt):
    try:
        wait = float(retry_after)
        if wait != wait or wait < 0:                  # NaN or negative
            raise ValueError
        return min(wait, MAX_WAIT)
    except (TypeError, ValueError):
        return BACKOFF[attempt] * random.uniform(0.5, 1.5)


def request(path, query, key, skill):
    """GET base+path. Returns (parsed JSON, raw bytes). Retries 429 and 5xx twice and a broken
    connection once, within DEADLINE seconds in total."""
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json",
               "User-Agent": client(skill), "X-SearchApi-Client": client(skill)}
    base = base_url()
    opener, host = (OPENER, "www.searchapi.io") if base == BASE else (LOCAL_OPENER, urllib.parse.urlsplit(base).netloc)
    method, data, flaky, attempt, start = "GET", None, False, 0, time.time()
    while True:
        url = base + path + ("?" + urllib.parse.urlencode(query) if query and method == "GET" else "")
        left = DEADLINE - (time.time() - start)
        sent_at = time.time()
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with opener.open(req, timeout=min(TIMEOUT, max(left, 5))) as r:
                chunks = []
                while True:                                  # in pieces: a body that trickles in must not outlast DEADLINE
                    chunk = r.read1(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if time.time() - start > DEADLINE:
                        raise ApiError(5, "No complete answer in %d s. Retry once at most; do not loop." % DEADLINE)
                raw = b"".join(chunks)
                if r.length:                                 # closed before Content-Length bytes came
                    raise http.client.IncompleteRead(raw, r.length)
            try:
                return json.loads(raw.decode("utf-8", "replace"), parse_constant=lambda c: None), raw
            except ValueError:
                raise ApiError(4, "The API sent a response that is not JSON: %s" % raw[:200].decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            try:
                msg = json.loads(body).get("error") or body
            except (ValueError, AttributeError):
                msg = body or e.reason
            if e.code in (413, 414) and method == "GET":   # very long token: resend as a JSON body
                method, data = "POST", json.dumps(query).encode("utf-8")
                headers["Content-Type"] = "application/json"
                continue
            if 300 <= e.code < 400:
                raise ApiError(4, "The API answered with a redirect (HTTP %d). Not followed, so the key stays put."
                               % e.code, http=e.code)
            if (e.code == 429 or e.code >= 500) and attempt < RETRIES:
                wait = backoff(e.headers.get("Retry-After") if e.headers else None, attempt)
                if time.time() - start + wait + max(10, time.time() - sent_at) < DEADLINE:   # room for a whole attempt
                    attempt += 1
                    time.sleep(wait)
                    continue
            code = 2 if e.code == 400 else 3 if e.code in (401, 402, 403) or PLAN.search(str(msg)) else 4
            raise ApiError(code, msg, http=e.code)
        except (http.client.HTTPException, socket.timeout, TimeoutError, urllib.error.URLError, OSError) as e:
            if isinstance(e, http.client.InvalidURL):        # nothing was sent: a retry cannot help
                raise ApiError(2, "Bad request URL (%s). Check SEARCHAPI_BASE_URL and the params." % e)
            reason = getattr(e, "reason", e)
            slow = isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason)
            broken = isinstance(e, http.client.HTTPException) or isinstance(reason, (ConnectionResetError, ConnectionAbortedError))
            if slow or broken:
                if not flaky and time.time() - start + 30 < DEADLINE:   # a broken call may still bill: once only
                    flaky = True
                    continue
                raise ApiError(5, "No complete answer in %d s. Retry once at most; do not loop."
                               % (time.time() - start))
            if "CERTIFICATE_VERIFY_FAILED" in str(reason):
                raise ApiError(5, CERTS)
            raise ApiError(5, "Could not reach %s (%s). %s" % (host, reason, EGRESS))


# ------------------------------------------------------------------ lossless trim

def seq(s):
    return " %s " % " ".join(WORD.findall(s.lower()))


def answer_parts(node, texts, cited):
    """Text leaves (numbers too) and cited reference indexes inside text_blocks."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "reference_indexes":
                cited.update(x for x in (v if isinstance(v, list) else [v]) if isinstance(x, int))
            elif k != "type":
                answer_parts(v, texts, cited)
    elif isinstance(node, list):
        for v in node:
            answer_parts(v, texts, cited)
    elif node is not None:
        texts.append(node if isinstance(node, str) else json.dumps(node))


def dedupe_answer(d):
    """AI answers come twice: markdown and text_blocks. Drop a copy only when nothing is lost."""
    rl = d.get("reference_links")
    refs = [r for r in rl if isinstance(r, dict) and isinstance(r.get("link"), str) and r["link"]] if isinstance(rl, list) else []
    titles = {}
    for r in refs:
        titles.setdefault((r["link"], str(r.get("index"))), set()).add(" ".join(str(r.get("title") or "").split()).lower())
    if refs:   # the "[[n] - title](link)" list appended to markdown repeats reference_links
        keep = []
        for ln in d["markdown"].split("\n"):
            m = REF_LINE.match(ln)
            if not (m and " ".join(m.group(2).split()).lower() in titles.get((m.group(3), m.group(1)), ())):
                keep.append(ln)
        d["markdown"] = "\n".join(keep).rstrip()
    md, blocks = d["markdown"], d.get("text_blocks")
    if not (md and blocks):
        return
    texts, cited = [], set()
    answer_parts(blocks, texts, cited)
    if not cited <= {int(n) for n in MARK.findall(md)}:
        return      # which paragraph cites which source lives only in text_blocks
    links, flat = {r["link"] for r in refs}, seq(LINK.sub("]", md))
    for s in texts:
        if s.startswith("http"):
            if s not in md and s not in links:
                return
        elif WORD.search(s):
            if seq(s) not in flat:
                return
        elif s.strip() and s.strip() not in md:              # symbols only (check marks, stars): kept as they are
            return
    del d["text_blocks"]


def trim(node, top=False):
    """Drop base64 and inline-image data: URIs, the request echo, nulls and empty arrays/objects, and
    duplicate AI answer copies; cut search_metadata to id, status and URLs. Every result row stays."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if top and k == "search_parameters":
                continue
            if top and k == "search_metadata" and isinstance(v, dict):
                v = {m: v[m] for m in META if m in v}
            v = trim(v)
            if v is not None and v != [] and v != {}:
                out[k] = v
        if isinstance(out.get("markdown"), str):
            dedupe_answer(out)
        return out
    if isinstance(node, list):   # only data: URIs leave a list; every other item keeps its place
        return [trim(v) for v in node if not (isinstance(v, str) and DATA_URI.match(v))]
    if isinstance(node, str) and DATA_URI.match(node):
        return None
    return node


def count(doc, body):
    """Rows in the main result list: organic_results, else the first *_results list, else the first list of
    objects; None when there is no such list (google_trends)."""
    named = [k for k, v in doc.items() if k.endswith("_results") and isinstance(v, list)]
    if named:
        return len(doc["organic_results" if "organic_results" in named else named[0]])
    lists = [v for k, v in body.items() if k != "text_blocks" and isinstance(v, list) and v and isinstance(v[0], dict)]
    return len(lists[0]) if lists else None


def overview(body, limit=OVERVIEW_AT - 1000):
    """One line per top-level block: field paths you can pass to --fields, with array sizes and types."""
    def walk(n, path, seen):
        if isinstance(n, dict):
            many = len(n) > MAP_KEYS
            for k, v in n.items():
                walk(v, (path + "." if path else "") + ("*" if many else str(k)), seen)
            return
        e = seen.setdefault(path, [set(), None, None])
        if isinstance(n, list):
            e[1] = len(n) if e[1] is None else min(e[1], len(n))
            e[2] = max(e[2] or 0, len(n))
            for v in n:
                walk(v, path, seen)
        else:
            e[0].add("url" if str(n).startswith("http") else
                     {"str": "str", "bool": "bool", "int": "int", "float": "num"}.get(type(n).__name__, "?"))

    def label(e):
        n = "" if e is None or e[2] is None else "[%d]" % e[2] if e[1] == e[2] else "[%d-%d]" % (e[1], e[2])
        return n + (" " + "|".join(sorted(e[0])) if e and e[0] else "")
    out = []
    for k, v in body.items():
        seen = {}
        walk(v, "", seen)
        top = seen.pop("", None)
        line = str(k) + label(top) + (": " + ", ".join(p + label(e) for p, e in seen.items()) if seen else "")
        out.append(line if len(line) <= 2000 else line[:1950] + " ... (%d paths)" % len(seen))
    text = "\n".join(out)
    if len(text) <= limit:
        return text
    return text[:limit - 100] + "\n... (cut: pick blocks by name)" if limit >= 200 else ""


def hint(body, sid):
    """A pick command for the main result list: up to 7 useful fields, the follow-up id and token when they
    fit, and never more than prints in full (100 rows may leave room for fewer)."""
    rows = next((k for k in ["organic_results"] + list(body) if k.endswith("_results") and isinstance(body.get(k), list)),
                None) or next((k for k, v in body.items() if k != "text_blocks" and isinstance(v, list)), None)
    first = body[rows][0] if rows and body[rows] else None
    scalar, paths = (str, int, float, bool), {}              # path -> 0 top level, 1 one level down
    for k, v in first.items() if isinstance(first, dict) and re.fullmatch(r"[\w-]+", rows) else ():
        if NOT_HINT.search(k) or not re.fullmatch(r"[\w-]+", k):
            continue
        if isinstance(v, scalar):
            if not (k.endswith("link") and GOOGLE_URL.match(str(v))):   # Google's own pages, not the result
                paths[k] = 0
        elif isinstance(v, dict):
            paths.update((k + "." + s, 1) for s, x in v.items() if s in HINT_IN_DICT and isinstance(x, scalar))
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            paths.update((k + "." + s, 1) for s, x in v[0].items() if s in HINT_IN_LIST and isinstance(x, scalar))
    order = HINT + HINT_IN_LIST
    keep = [p for p in ("asin", "place_id", "id") if p in paths][:1] + [p for p in paths if p.endswith("_token")][:1]
    best = sorted((p for p in paths if p not in keep and p.rsplit(".", 1)[-1] in order),
                  key=lambda p: (paths[p], order.index(p.rsplit(".", 1)[-1])))
    fill = [p for p in paths if p not in keep + best and not paths[p]]
    fields = []
    for p in best[:7 - len(keep)] + keep + fill:            # greedy: a big token goes before the useful fields do
        if len(fields) < (5 if p in fill else 7) and \
                len(dumps(pick(body, "%s:%s" % (rows, ",".join(fields + [p])))[0])) <= OVERVIEW_AT - 1000:
            fields.append(p)
    fields = [p for p in fields if p not in keep] + [p for p in fields if p in keep] or (best + keep + fill)[:1]
    spec = "%s:%s" % (rows, ",".join(fields)) if fields else "block:field,field;block2"
    return command("pick", shlex.quote(str(sid)), "--fields", "'%s'" % spec)


def merge(a, b):
    """Two field tries in one: True (the whole value) wins."""
    if a is True or b is True:
        return True
    return dict(a, **{k: merge(a[k], v) if k in a else v for k, v in b.items()})


def project(n, t):
    if t is True:
        return n
    if isinstance(n, list):
        return [project(v, t) for v in n]
    if isinstance(n, dict):
        star = t.get("*")                                    # * = every key of a map (dates, ids)
        if star is None:
            return {k: project(n[k], sub) for k, sub in t.items() if k in n}
        return {k: project(v, merge(t[k], star) if k in t else star) for k, v in n.items()}
    return None


def prune(n):
    if isinstance(n, dict):
        n = {k: prune(v) for k, v in n.items()}
        return {k: v for k, v in n.items() if v not in (None, "", [], {})}
    if isinstance(n, list):
        return [prune(x) for x in n]
    return n


def pick(body, spec):
    trie, missing = {}, []
    for part in filter(None, (p.strip() for p in spec.split(";"))):
        block, _, fields = part.partition(":")
        for path in [block.strip() + "." + f.strip() for f in fields.split(",") if f.strip()] or [block.strip()]:
            one = True
            for b in reversed(path.split(".")):
                one = {b: one}
            if not any_value(prune(project(body, one))):
                missing.append(path)
            cur, bits = trie, path.split(".")
            for b in bits[:-1]:
                if cur.get(b) is True:
                    break
                cur = cur.setdefault(b, {})
            else:
                cur[bits[-1]] = True
    return prune(project(body, trie)), missing


def any_value(n):
    if isinstance(n, dict):
        return any(any_value(v) for v in n.values())
    if isinstance(n, list):
        return any(any_value(v) for v in n)
    return n not in (None, "", [], {})


# ------------------------------------------------------------------ one search

def parse_pairs(items):
    sent = {}
    for kv in items:
        k, sep, v = kv.partition("=")
        if not sep or not k:
            raise ApiError(2, "Bad argument %r: use key=value" % kv)
        if k.lower() == "api_key":
            raise ApiError(2, "Never pass the key as a param. Use SEARCHAPI_API_KEY or the setup command.")
        if k == "engine":
            raise ApiError(2, "The engine is the first argument, not a param.")
        if k in sent:
            raise ApiError(2, "%s is given twice" % k)
        sent[k] = v
    return sent


def same(a, b):
    a, b = str(a).strip().lower(), str(b).strip().lower()
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def check_params(sent, echo):
    """SearchApi drops unknown params and overrides some values (num) without an error."""
    renamed = [v for k, v in echo.items() if k not in sent]          # a few engines echo a param under another name
    ignored = [k for k, v in sent.items() if k not in echo and not any(same(v, x) for x in renamed)]
    changed = {k: echo[k] for k, v in sent.items() if k in echo and not same(v, echo[k])}
    return ignored, changed


def located(sent, echo):
    """The location the API searched from, when it is not the one sent (live: London became a TV region)."""
    used = echo.get("location_used") if isinstance(echo, dict) else None
    norm = lambda s: re.sub(r"\s*,\s*", ",", str(s).strip().lower())
    return used if used not in (None, "") and norm(used) != norm("" if sent is None else sent) else None


def search(engine, sent, key, skill):
    """One call. Returns (head, trimmed body, doc); raises ApiError on any failure."""
    if not NAME.fullmatch(engine):
        raise ApiError(2, "Unknown engine %r. Engines are lowercase names such as google or google_maps." % engine)
    try:
        doc, raw = request("/api/v1/search", dict(sent, engine=engine), key, skill)
    except ApiError as e:
        if e.extra.get("http") == 400 and "engine" in e.msg.lower():
            e.msg += " (engine list: %s)" % DOCS
        raise
    meta = doc.get("search_metadata") if isinstance(doc, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    head = {"ok": True, "id": meta.get("id")}
    try:
        path = save_raw(engine, meta.get("id"), raw)
    except OSError as e:
        path = None
        head["warning"] = "Raw response not saved (%s). Set SEARCHAPI_DIR to a writable folder." % e
    if isinstance(doc, dict) and doc.get("error"):          # check error first: error bodies have no metadata
        raise ApiError(3 if PLAN.search(str(doc["error"])) else 4, doc["error"], raw=path)
    if not isinstance(doc, dict) or meta.get("status", "Success") != "Success":
        raise ApiError(4, "search_metadata.status is %r, not Success" % meta.get("status"), raw=path)
    echo = doc.get("search_parameters")
    if isinstance(echo, dict) and echo:
        ignored, changed = check_params(sent, echo)
        if ignored:
            head["ignored"] = ignored
        if changed:
            head["changed"] = changed
        if located(sent.get("location"), echo):
            head["location_used"] = echo["location_used"]
    body = trim(doc, top=True)
    if not {k for k in body if k not in ("search_metadata", "search_information", "pagination", "related_searches")}:
        head["empty"] = True
    head["count"] = 0 if head.get("empty") else count(doc, body)
    head["raw"] = path
    return head, body, doc


def show(head, body, fields, left=OVERVIEW_AT, saved=True):
    """Print the status line and the JSON, or an overview when the JSON passes `left` characters and a raw
    file is there to pick from (without one, an overview is a dead end). Returns the characters printed."""
    if fields:
        out, missing = pick(body, fields)
        if missing:
            head["missing"] = missing
        if body.get("pagination") and "pagination" not in out:
            out["pagination"] = body["pagination"]
        body = out
    text = dumps(body)
    if len(text) > left and saved:
        head["overview"] = True
        head["next"] = hint(body, head["id"] or os.path.basename(head.get("raw") or "")) if not fields else \
            "pick fewer blocks or fields%s: these came to %d characters" % (
                ", or fewer IDs per run" if left < OVERVIEW_AT else "", len(text))
        text = overview(body, left - 1000)
    emit(head, text)
    return len(dumps(head)) + len(text) + 2


# ------------------------------------------------------------------ batch

def batch(path, yes, skill):
    try:
        if path == "-":
            text = sys.stdin.buffer.read().decode("utf-8-sig")   # UTF-8 whatever the console code page
        else:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
    except (OSError, UnicodeDecodeError, AttributeError) as e:
        fail(2, "Cannot read %s: %s" % (path, e))
    jobs = []
    for n, line in enumerate(text.lstrip(chr(0xFEFF)).splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):                 # whole-line comments only: C# and #tags are queries
            continue
        try:
            parts = shlex.split(line)
            if not NAME.fullmatch(parts[0]):
                raise ApiError(2, "unknown engine %r" % parts[0])
            jobs.append((n, parts[0], parse_pairs(parts[1:])))
        except ValueError as e:
            fail(2, "Line %d: %s" % (n, e))
        except ApiError as e:
            fail(2, "Line %d: %s" % (n, e.msg))
    if not jobs:
        fail(2, "No queries in %s. Write one per line: ENGINE key=value ..." % path)
    try:
        key = load_key()
        credits = me(key, skill)["credits"]                  # free: one balance check per batch, never per call
    except ApiError as e:                                    # no balance, no batch: the calls would fail the same way
        fail(e.code, e.msg, **e.extra)
    if len(jobs) > ASK_ABOVE and not yes:
        fail(2, "%d calls planned, %s credits left. If the user listed these searches, tell them this count and "
                "balance, then rerun with --yes: %s. Otherwise ask the user first."
             % (len(jobs), fmt(credits), command("batch", shlex.quote(path), "--yes")), planned=len(jobs), credits=credits)
    if isinstance(credits, int) and credits < len(jobs):
        fail(3, "%s planned but only %s left. Top up at %s or run fewer."
             % (plural(len(jobs), "call"), plural(credits, "credit"), BASE), planned=len(jobs), credits=credits)
    try:
        folder = data_dir()
    except OSError:
        folder = None
    emit({"ok": True, "planned": len(jobs), "credits": credits, "concurrency": CONCURRENCY, "dir": folder})
    stop = threading.Event()

    def run(job):
        n, engine, sent = job
        if stop.is_set():
            return n, engine, sent, None, "skipped", None
        try:
            head, body, _ = search(engine, sent, key, skill)
            return n, engine, sent, head, "ok", None
        except ApiError as e:
            if e.extra.get("http") in (401, 402):
                stop.set()                                   # bad key or no credits: do not start the rest
            return n, engine, sent, None, redact(e.msg), e.code   # a plan error on one engine fails that line only
        except Exception as e:                               # never lose the table over one row
            return n, engine, sent, None, redact("%s: %s" % (type(e).__name__, e)), 4
    with concurrent.futures.ThreadPoolExecutor(CONCURRENCY) as pool:
        results = list(pool.map(run, jobs))
    rows, codes, empty = ["line\tengine\tquery\tstatus\tcount\traw"], [], 0
    for n, engine, sent, head, status, code in results:
        query = " ".join((sent.get("q") or " ".join("%s=%s" % kv for kv in sent.items())).split())
        query = query if len(query) <= 50 else query[:47] + "..."
        if head:
            status = "empty" if head.get("empty") else "ok"
            empty += status == "empty"
            for k in ("ignored", "changed"):
                if head.get(k):
                    status += " %s=%s" % (k, ",".join(head[k]))
            if head.get("location_used"):
                status += " location_used=%s" % head["location_used"]
            if not head.get("raw"):
                status += " (raw not saved)"
        else:
            codes.append(code or 3)
            status = "error: " + " ".join(str(status).split())[:80]
        cells = (n, engine, query, status, "" if not head or head["count"] is None else head["count"],
                 os.path.basename(head["raw"] or "") if head else "")
        rows.append("\t".join(" ".join(str(c).split()) for c in cells))   # one line, no tab inside a cell
    emit({"ok": not codes, "calls": len(jobs), "failed": len(codes), "empty": empty,
          "next": command("pick", "RAW", "[RAW ...]", "--fields", "SPEC")},
         "\n".join(rows))
    if codes:
        sys.exit(codes[0])


# ------------------------------------------------------------------ account: setup and credits

def me(key, skill):
    doc, _ = request("/api/v1/me", None, key, skill)
    doc = doc if isinstance(doc, dict) else {}
    acc, use = doc.get("account") or {}, doc.get("api_usage") or {}
    return {"credits": acc.get("remaining_credits"), "searches_this_hour": use.get("searches_this_hour"),
            "hourly_limit": use.get("hourly_rate_limit")}


def fmt(n):
    return "{:,}".format(n) if isinstance(n, int) else "unknown" if n is None else str(n)


def credits(skill):
    try:
        info = me(load_key(), skill)
    except ApiError as e:
        fail(e.code, e.msg, **e.extra)
    emit(dict(ok=True, **info), "Credits left: %s. Searches this hour: %s of %s." % (
        fmt(info["credits"]), fmt(info["searches_this_hour"]), fmt(info["hourly_limit"])))


def save_key(key):
    """Write the key file 0600 through a new file, so an old 0644 file or a symlink is replaced, never written into."""
    path = os.path.expanduser(KEY_FILE)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
        os.replace(tmp, path)
    except BaseException:                                    # never leave the key behind in a temp file
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def setup(from_stdin, skill):
    tty = sys.stdin is not None and sys.stdin.isatty()
    if from_stdin and not tty:
        key = sys.stdin.read().strip()
    elif not tty:
        fail(2, "setup needs a terminal for the hidden prompt. Run it in your own terminal, "
                "or pipe the key in: ... | %s" % command("setup", "--key-stdin"))
    else:                                                    # --key-stdin typed at a terminal is hidden too
        key = getpass.getpass("SearchApi API key (input hidden): ").strip()
    SECRETS.extend(p for p in [key] + key.split() if len(p) >= 8)
    if not KEY_OK.fullmatch(key):
        fail(2, "That does not look like a SearchApi key. Copy it from your SearchApi dashboard. "
                "No account yet? Sign up: %s" % SIGN_UP)
    head = {"ok": True, "saved": KEY_FILE.replace(os.sep, "/")}
    if (os.environ.get("SEARCHAPI_API_KEY") or "").strip() not in ("", key):
        head["note"] = "SEARCHAPI_API_KEY is set in this shell and is used before the saved key."
    try:
        info = me(key, skill)
    except ApiError as e:
        if e.extra.get("http") in (401, 402, 403):
            fail(3, "SearchApi did not accept this key, so it was not saved: %s" % e.msg, **e.extra)
        info, why = None, e                                  # offline or the API is down: keep the key, say it is unchecked
    try:
        save_key(key)
    except OSError as e:
        fail(2, "Could not save the key to %s (%s). Set SEARCHAPI_API_KEY instead." % (head["saved"], e))
    if info is None:
        fail(why.code, "Key saved to %s but not checked: %s" % (head["saved"], why.msg), saved=head["saved"])
    head["credits"] = info["credits"]
    emit(head, "Connected: %s credits left. Key saved to %s (only you can read it)." % (
        fmt(info["credits"]), head["saved"]))


# ------------------------------------------------------------------ params from the live OpenAPI file

def _ind(line):
    return len(line) - len(line.lstrip(" "))


def _scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return {"true": True, "false": False, "null": None, "~": None, "": None}.get(s, s)


def _split(s, sep=","):
    """Split on sep outside quotes; with sep '#', cut a trailing comment instead."""
    items, cur, q = [], "", None
    for i, ch in enumerate(s):
        if q or ch not in sep + "'\"":
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        elif sep == "#" and (i == 0 or s[i - 1] in " \t"):
            return [cur.rstrip()]
        elif sep == ",":
            items.append(cur)
            cur = ""
            continue
        cur += ch
    return items + [cur]


def _value(rest, lines, i, ind):
    rest = _split(rest, "#")[0].strip()
    if rest[:1] in ("|", ">"):
        body = []
        while i < len(lines) and (not lines[i].strip() or _ind(lines[i]) > ind):
            body.append(lines[i].strip())
            i += 1
        return (" " if rest[0] == ">" else "\n").join(body).strip(), i
    if rest == "":
        j = i
        while j < len(lines) and not lines[j].strip():
            j += 1
        nested = j < len(lines) and (_ind(lines[j]) > ind or _ind(lines[j]) == ind and lines[j].lstrip().startswith("- "))
        return _block(lines, j, _ind(lines[j])) if nested else (None, i)
    while rest.startswith("[") and not rest.endswith("]") and i < len(lines):
        rest, i = rest + " " + _split(lines[i], "#")[0].strip(), i + 1
    if rest.startswith("[") and rest.endswith("]"):
        return [_scalar(x) for x in _split(rest[1:-1]) if x.strip()], i
    while i < len(lines) and lines[i].strip() and _ind(lines[i]) > ind:
        rest, i = rest + " " + lines[i].strip(), i + 1
    return _scalar(rest), i


def _block(lines, i, ind):
    """A block mapping or sequence whose entries sit at indent ind (the YAML subset these specs use)."""
    seq_ = lines[i].lstrip().startswith("- ")
    out = [] if seq_ else {}
    while i < len(lines):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("#") or _ind(ln) > ind:
            i += 1
        elif _ind(ln) < ind or ln.lstrip().startswith("- ") != seq_:
            break
        elif seq_ and re.match(r"^[^:'\"\[]+?:(\s|$)", ln[ind + 2:]):
            lines[i] = " " * (ind + 2) + ln[ind + 2:]          # "- key: v" starts a mapping item
            val, i = _block(lines, i, ind + 2)
            out.append(val)
        elif seq_:
            val, i = _value(ln[ind + 2:], lines, i + 1, ind)
            out.append(val)
        else:
            m = re.match(r"^\s*([^:#]+?):(\s+.*|)$", ln)
            if m:
                out[_scalar(m.group(1))], i = _value(m.group(2), lines, i + 1, ind)
            else:
                i += 1
    return out, i


def oas_params(text):
    lines = text.split("\n")
    find = lambda rx, start: next((i for i in range(start or 0, len(lines)) if re.match(rx, lines[i])), None)
    p = find(r"^      parameters:\s*$", find(r"^    get:\s*$", 0))
    items = _block(lines, p + 1, _ind(lines[p + 1]))[0] if p is not None else []
    c = find(r"^  parameters:\s*$", find(r"^components:\s*$", 0))
    comps = _block(lines, c + 1, 4)[0] if c is not None else {}
    return [comps.get(x["$ref"].rsplit("/", 1)[-1], {}) if isinstance(x, dict) and "$ref" in x else x for x in items]


def params(engine, name=None):
    try:
        if engine.endswith((".yaml", ".yml")) and os.path.isfile(engine):   # a saved spec, for offline tests
            with open(engine, "rb") as f:
                text = f.read().decode("utf-8", "replace")
        elif not NAME.fullmatch(engine):
            fail(2, "Unknown engine %r" % engine)
        else:
            with urllib.request.urlopen("%s/openapi/%s.yaml" % (base_url(), engine), timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            fail(2, "No OpenAPI file for %s: check the engine name, the MCP tool list or %s" % (engine, DOCS))
        fail(4, "Could not fetch the OpenAPI file for %s (HTTP %d). Use the MCP tool schema or %s" % (engine, e.code, DOCS))
    except (http.client.HTTPException, urllib.error.URLError, OSError) as e:
        fail(5, CERTS if "CERTIFICATE_VERIFY_FAILED" in str(e) else "%s. %s" % (e, EGRESS))
    try:
        specs = oas_params(text)
    except (IndexError, KeyError, ValueError, AttributeError, TypeError, RecursionError):
        specs = None
    if not specs:                                            # not a spec (an HTML page, an empty file): never "0 params"
        fail(4, "Could not read the OpenAPI file for %s. Use the MCP tool schema or %s" % (engine, DOCS))
    rows = []
    for p in specs:
        if not isinstance(p, dict) or p.get("name") in ("engine", "api_key") or name not in (None, p.get("name")):
            continue
        if name:
            return emit({"ok": True, "engine": engine}, dumps(p))
        sch = p.get("schema") if isinstance(p.get("schema"), dict) else {}
        en = sch.get("enum") if isinstance(sch.get("enum"), list) else []
        bits = [str(p.get("name")), str(sch.get("type", "?"))] + (["req"] if p.get("required") else [])
        bits += (["def=%s" % sch["default"]] if "default" in sch else []) + (
            ["enum(%d)" % len(en)] if len(en) > 12 else ["enum=" + "|".join(map(str, en))] if en else [])
        desc = re.sub(r"\s+", " ", str(p.get("description") or "")).strip()
        m = re.match(r"^(.+?[.!?])(\s|$)", desc)
        desc = m.group(1) if m else desc
        rows.append(" ".join(bits) + " - " + (desc if len(desc) <= 160 else desc[:157] + "..."))
    if name:
        fail(2, "%s has no param %s" % (engine, name))
    emit({"ok": True, "engine": engine, "params": len(rows)},
         "\n".join(rows) + "\n(req = required. Full text and enum: %s)" % command("params", shlex.quote(engine), "PARAM"))


# ------------------------------------------------------------------ locations

def locations(args, full, skill):
    """Exact location= values from GET /api/v1/locations (free): one line per place, biggest first."""
    words = [a for a in args if "=" not in a]
    try:
        query = parse_pairs([a for a in args if "=" in a])
    except ApiError as e:
        fail(e.code, e.msg)
    if words and "q" in query:
        fail(2, "q is given twice")
    query.setdefault("q", " ".join(words))
    if not query["q"].strip():
        fail(2, "Usage: %s" % command("locations", "QUERY", "[limit=N]"))
    try:
        doc, _ = request("/api/v1/locations", query, load_key(), skill)
    except ApiError as e:
        fail(e.code, e.msg, **e.extra)
    if isinstance(doc, dict) and doc.get("error"):
        fail(3 if PLAN.search(str(doc["error"])) else 4, doc["error"])
    if not isinstance(doc, list):
        fail(4, "The locations API sent no list: %s" % dumps(doc)[:200])
    rows = [r for r in doc if isinstance(r, dict)]
    if full:
        return emit({"ok": True, "count": len(rows)}, dumps(rows))
    emit({"ok": True, "count": len(rows)}, "\n".join(
        "\t".join(" ".join(str("" if r.get(k) is None else r.get(k)).split()) for k in LOCATION_FIELDS) for r in rows))


# ------------------------------------------------------------------ main

def load_raw(ref):
    try:
        folder = data_dir()
    except OSError:
        folder = None
    found = [p for p in [ref] + ([os.path.join(folder, os.path.basename(ref))] if folder else []) if os.path.isfile(p)]
    found = found or (glob.glob(os.path.join(folder, "*-%s.json" % glob.escape(ref))) if folder else [])
    if not found:
        fail(2, "No saved response %r. Use the id or raw file from a status line or batch table." % ref)
    try:
        with open(found[0], "rb") as f:
            doc = json.loads(f.read().decode("utf-8"), parse_constant=lambda c: None)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        fail(2, "%s is not a saved SearchApi response (%s)" % (found[0], type(e).__name__))
    if not isinstance(doc, dict):
        fail(2, "%s is not a search response (no JSON object)" % found[0])
    return doc, found[0]


class Parser(argparse.ArgumentParser):
    def error(self, message):
        fail(2, "%s. Usage: %s" % (message, re.sub(r"^usage:\s*", "", self.format_usage().strip())))


def main():
    global ASCII
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "").replace("_", "")
    if enc not in ("utf8", "utf8sig"):                       # Windows pipes: keep JSON valid, never crash
        ASCII = True
        try:
            sys.stdout.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    ap = Parser(prog=command(), description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                allow_abbrev=False, usage="%(prog)s ENGINE key=value ... | pick | params | locations | batch | credits | setup")
    ap.add_argument("engine", help=argparse.SUPPRESS)
    ap.add_argument("args", nargs="*", help=argparse.SUPPRESS)
    ap.add_argument("--fields", action="append", help="block:field,field;block2 (print only these; repeats add up)")
    ap.add_argument("--full", action="store_true", help="print the untrimmed response")
    ap.add_argument("--yes", action="store_true", help="batch: run more than %d calls" % ASK_ABOVE)
    ap.add_argument("--key-stdin", action="store_true", help="setup: read the key from stdin")
    ap.add_argument("--skill", default="searchapi", help=argparse.SUPPRESS)
    a = ap.parse_intermixed_args()
    skill = re.sub(r"[^a-z0-9_.-]", "", a.skill.lower())[:40] or "searchapi"
    what, fields = a.engine, ";".join(a.fields or ())
    if a.full and fields:
        fail(2, "Use --full or --fields, not both.")
    if what == "params":
        return params(*a.args[:2]) if a.args else fail(2, "Usage: %s" % command("params", "ENGINE", "[PARAM]"))
    if what == "locations":
        if fields:
            fail(2, "locations prints one line per place. Use --full for every field.")
        return locations(a.args, a.full, skill)
    if what == "pick":
        if not a.args or not fields:
            fail(2, "Usage: %s" % command("pick", "ID", "[ID ...]", "--fields", "SPEC"))
        left = OVERVIEW_AT                                   # one budget for every response picked in this run
        for ref in a.args:
            doc, path = load_raw(ref)
            meta = doc.get("search_metadata") if isinstance(doc.get("search_metadata"), dict) else {}
            head = {"ok": True, "id": meta.get("id") or ref}
            if doc.get("error"):
                fail(4, doc["error"], id=head["id"], raw=path)
            echo = doc.get("search_parameters")
            if located(echo.get("location") if isinstance(echo, dict) else None, echo):
                head["location_used"] = echo["location_used"]
            left -= show(head, trim(doc, top=True), fields, max(left, 0))
        return
    if what == "batch":
        if fields or a.full:
            fail(2, "batch prints a summary table. Use pick on its raw files for fields.")
        return batch(a.args[0], a.yes, skill) if a.args else fail(2, "Usage: %s" % command("batch", "FILE", "[--yes]"))
    if what == "credits":
        return credits(skill)
    if what == "setup":
        return setup(a.key_stdin, skill)
    try:
        sent = parse_pairs(a.args)
        head, body, doc = search(what, sent, load_key(), skill)
    except ApiError as e:
        fail(e.code, e.msg, **e.extra)
    if a.full:
        return emit(head, dumps(doc))
    show(head, body, fields, saved=bool(head.get("raw")))


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:                                  # the reader stopped early (| head): not an error
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:                                   # never a traceback: it could carry the key
        fail(4, "Unexpected error (%s): %s. Please report it at https://github.com/searchapi/agent-skills/issues"
             % (type(e).__name__, e))
