"""Lint the repo: skill size and frontmatter, links, key handling, scanner trip-wires, variants, manifests.

    python3 build/lint.py                  everything (CI)
    python3 build/lint.py --skill searchapi   one skill only (while the rest is being built)

Exit 1 on any error. Standard library only (Python 3.11+ also checks the Gemini .toml files).
"""
import datetime
import glob
import importlib.util
import json
import os
import re
import urllib.parse
import stat
import sys

sys.dont_write_bytecode = True       # never leave a .pyc in the skill folder
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "skills")
spec = importlib.util.spec_from_file_location("searchapi", os.path.join(SKILLS, "searchapi", "scripts", "searchapi.py"))
sa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sa)
vspec = importlib.util.spec_from_file_location("variants", os.path.join(ROOT, "build", "variants.py"))
variants = importlib.util.module_from_spec(vspec)
vspec.loader.exec_module(variants)

MAX_BYTES = 8000                    # Codex cuts SKILL.md here
MAX_DESC = 500
BRAND_WITHIN = 57                    # Hermes shows 57 chars of a description
KEYS = {"name", "description", "license", "metadata"}
MARKETING = r"\b(powerful|comprehensive|seamless|advanced|cutting-edge|state-of-the-art|revolutionary|robust)\b"
SKIP_DIRS = {".git", "fixtures", "__pycache__", "node_modules"}
TEXT_EXT = {".md", ".py", ".json", ".yaml", ".yml", ".toml", ".txt", ""}
SCAN = [   # mirrors of Hermes skills_guard rules, so a failure comes with our own explanation
    (r"(curl|wget)\s+(?![^\n]*https?://(localhost|127\.0\.0\.1))[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD)", "curl or wget with a secret variable"),
    (r"(requests|httpx)\.(get|post|put|patch)\s*\([^\n]*(key|token|secret|password)", "HTTP library call next to a secret"),
    (r"printenv|\benv\s*\|", "environment dump"),
    (r"^[^#\n]*os\.environ\b(?!\s*\.get\s*\()", "bare os.environ (use os.environ.get)"),
    (r"(\$HOME|~)/\.(ssh|aws|gnupg)", "credential directory"),
    (r"ignore\s+(\w+\s+)*(previous|all|above|prior)\s+instructions|you\s+are\s+(\w+\s+)*now\s|do\s+not\s+(\w+\s+)*tell\s+(\w+\s+)*the\s+user|pretend\s+(\w+\s+)*(you\s+are|to\s+be)\s", "injection-shaped phrase"),
    (r"(send|post|upload|transmit)\s+.*\s+(to|at)\s+https?://", "send-to-URL phrasing"),
    (r"^\s*([-*]\s*)?(write|edit|modify|update|append|prepend|inject|overwrite|replace|alter|add)\b[^,\n]{0,80}(AGENTS\.md|CLAUDE\.md|\.cursorrules|\.claude/settings|\.codex/config)", "edit of an agent config file"),
    (r"os\.system\s*\(|os\.popen\s*\(|shell\s*=\s*True|\beval\s*\(\s*[\"']|\bexec\s*\(\s*[\"']", "shell-out or dynamic code"),
    (r"echo\s+\"?\$\w+\"?\s*\|\s*jq", "echo into jq (use printf '%s')"),
    (r"pip\s+install|npm\s+install|\bsudo\b", "install or privilege command"),
    (r"(api[_-]?key|token|secret)\s*[=:]\s*[\"'](?![A-Z][A-Z0-9_]+[\"'])[A-Za-z0-9+/=_-]{20,}", "hardcoded secret"),
    (r"\$\{CLAUDE_|\$ARGUMENTS|\$\{HERMES_|\{baseDir\}", "harness-only syntax"),
]
HERMES_SCAN = [   # Hermes 0.19 skills_guard env rules, verbatim: critical blocks for good, high needs --force
    (r'os\.environ\b(?!\s*\.get\s*\(\s*["\'](?![^"\']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)))', "os.environ use (high)"),
    (r'os\.environ\s*\.get\s*\(\s*["\'][^"\']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)', "secret read via os.environ.get (critical)"),
    (r'os\.getenv\s*\(\s*[^\)]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)', "secret read via os.getenv (critical)"),
]
errors = []


def err(where, msg):
    errors.append("%s: %s" % (os.path.relpath(where, ROOT), msg))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def frontmatter(path):
    """(raw frontmatter text, parsed dict, body) using the script's YAML-subset reader."""
    text = read(path)
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        err(path, "no frontmatter")
        return "", {}, text
    lines = m.group(1).split("\n")
    data = sa._block(lines, 0, 0)[0] if lines else {}
    return m.group(1), data if isinstance(data, dict) else {}, text[m.end():]


def text_files(top):
    for d, dirs, files in os.walk(top):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            if os.path.splitext(f)[1] in TEXT_EXT and f != ".DS_Store":
                yield os.path.join(d, f)


# ------------------------------------------------------------------ one skill folder

def check_skill(folder, version):
    path = os.path.join(folder, "SKILL.md")
    if not os.path.isfile(path):
        return err(folder, "missing SKILL.md")
    raw = open(path, "rb").read()
    text = raw.decode("utf-8")
    fm, data, body = frontmatter(path)
    name = os.path.basename(folder)
    if len(raw) > MAX_BYTES:
        err(path, "%d bytes, over the %d-byte limit" % (len(raw), MAX_BYTES))
    if text.count("\n") > 500:
        err(path, "over 500 lines")
    if set(data) - KEYS:
        err(path, "frontmatter keys outside %s: %s" % (sorted(KEYS), sorted(set(data) - KEYS)))
    if data.get("name") != name or not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name):
        err(path, "name %r must equal the folder name %r (lowercase, hyphens)" % (data.get("name"), name))
    if re.search("anthropic|claude", name):
        err(path, "reserved word in name")
    desc = data.get("description") or ""
    if not re.search(r'(?m)^description: "[^"\n]*"$', fm):
        err(path, "description must be one double-quoted line")
    if len(desc) > MAX_DESC:
        err(path, "description is %d chars, over %d" % (len(desc), MAX_DESC))
    if "SearchApi" not in desc[:BRAND_WITHIN]:
        err(path, "description must say SearchApi in its first %d chars" % BRAND_WITHIN)
    if "Use when" not in desc or "Not for" not in desc:
        err(path, "description needs a 'Use when' list and one 'Not for'")
    if re.search(r"[<>]", desc) or re.match(r"(I|I'm|I'll|You|We|Let me)\b", desc):
        err(path, "description has < or > or is not third person")
    if re.search(MARKETING, text, re.I):
        err(path, "marketing word (Hermes linter): %s" % re.search(MARKETING, text, re.I).group(0))
    if data.get("license") != "MIT":
        err(path, "license must be MIT")
    meta = data.get("metadata")
    if not isinstance(meta, dict) or not all(isinstance(v, str) for v in meta.values()):
        err(path, "metadata must be a flat map of strings")
        meta = {}
    if meta.get("version") != version:
        err(path, "metadata.version %r does not match %s" % (meta.get("version"), version))
    try:
        if datetime.date.fromisoformat(meta.get("verified_on", "")) > datetime.date.today():
            err(path, "verified_on is in the future")
    except ValueError:
        err(path, "metadata.verified_on must be YYYY-MM-DD")
    if "\n## When to Use\n" not in body:
        err(path, "needs a '## When to Use' heading")
    if any(b > 127 for b in raw):
        err(path, "non-ASCII characters (bytes must equal chars)")
    if body.count("```") % 2 or "[TODO:" in body:
        err(path, "unbalanced code fence or [TODO:")
    named = set(re.findall(r"(?<![\w/.~-])((?:scripts|references|assets)/[\w./-]*\w)", body))
    for rel in sorted(named):
        if not os.path.isfile(os.path.join(folder, rel)):
            err(path, "names %s, which does not exist" % rel)
    for f in glob.glob(os.path.join(folder, "**", "*"), recursive=True):
        rel = os.path.relpath(f, folder).replace(os.sep, "/")
        if os.path.isdir(f):
            continue
        if rel.split("/")[0] in ("scripts", "references") and rel not in named:
            err(f, "not referenced from SKILL.md (installers copy only referenced files)")
        if os.path.basename(f) in ("README.md", "CHANGELOG.md", ".env", ".gitignore", ".DS_Store") or f.endswith((".pyc", ".pyo")):
            err(f, "file not allowed in a skill folder")
        if os.path.getsize(f) > 256 * 1024:
            err(f, "over 256 KB")
        if os.stat(f).st_mode & stat.S_IXUSR and not f.endswith((".py", ".sh")):
            err(f, "exec bit on a non-script")
    for ref in glob.glob(os.path.join(folder, "references", "*.md")):
        lines = read(ref).split("\n")
        heads = [ln[3:] for ln in lines if ln.startswith("## ") and ln != "## Contents"]
        toc = "\n".join(lines[:30])
        if len(lines) > 100 and ("## Contents" not in toc or any("- " + h not in toc for h in heads)):
            err(ref, "over 100 lines: needs a '## Contents' list of every ## heading in its first 30 lines")
    if len(glob.glob(os.path.join(folder, "**", "*"), recursive=True)) > 50:
        err(folder, "over 50 files")
    return data


def scan_text(top, commands=False):
    for f in text_files(top):
        text = read(f)
        for n, line in enumerate(text.split("\n"), 1):
            for rx, why in SCAN:
                if commands and why == "harness-only syntax":
                    continue                       # $ARGUMENTS is the command placeholder
                if re.search(rx, line, re.I if "AGENTS" not in rx and "CLAUDE_" not in rx else 0):
                    err(f, "line %d: %s" % (n, why))
            if os.path.relpath(f, ROOT).startswith("hermes" + os.sep):
                for rx, why in HERMES_SCAN:          # case-sensitive, as in Hermes
                    if re.search(rx, line):
                        err(f, "line %d: Hermes blocks the install: %s (edit HERMES_TEXT in build/variants.py)" % (n, why))


def repo_text_rules(tops):
    for top in tops:
        for f in text_files(top):
            text = read(f)
            rel = os.path.relpath(f, ROOT)
            if re.search("[\u2013\u2014]", text):
                err(f, "em or en dash")
            if "api_" + "key=" in text:
                err(f, "literal api_" "key= (the key goes only in the Authorization header)")
            if "../" in text and not rel.startswith(("tests", "build", ".github")):
                err(f, "../ path (installers fail closed on it)")
            if re.search("[\u200b-\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]", text):
                err(f, "invisible or bidi Unicode")
            if re.search(r"searchapi\.io/docs(?![/\w-])", text):
                err(f, "the docs root link is a 404: link /docs/google or an engine page")
            for url in re.findall(r"searchapi\.io/users/sign_up[^\s\"')`>\]]*", text):
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit("https://www." + url).query))
                if not q.get("utm_source") or q.get("utm_medium") != "agent-skill" or q.get("utm_campaign") != "searchapi-skill":
                    err(f, "sign-up link needs utm_source=<where>, utm_medium=agent-skill, utm_campaign=searchapi-skill")
    for top in tops:   # the Anthropic directory rejects non-image files over 256 KiB
        for f in glob.glob(os.path.join(top, "**", "*"), recursive=True) + glob.glob(os.path.join(top, ".*", "**", "*"), recursive=True):
            if os.path.isfile(f) and "/.git/" not in f and not f.endswith((".png", ".jpg", ".svg", ".gif")) \
                    and os.path.getsize(f) > 256 * 1024:
                err(f, "over 256 KiB: split it (directory limit per non-image file)")


# ------------------------------------------------------------------ variants, commands, manifests

def check_variants(names):
    for name in names:
        canon = os.path.join(SKILLS, name)
        _, _, body = frontmatter(os.path.join(canon, "SKILL.md"))
        for harness in ("hermes", "clawhub"):
            folder = os.path.join(ROOT, harness, name)
            path = os.path.join(folder, "SKILL.md")
            if not os.path.isfile(path):
                err(folder, "missing: run python3 build/variants.py")
                continue
            fm, data, vbody = frontmatter(path)
            want = body.replace("utm_source=agent-skill", "utm_source=" + harness)
            if vbody != (variants.hermes_text(name + "/SKILL.md", want) if harness == "hermes" else want):
                err(path, "body differs from skills/%s/SKILL.md (beyond its sign-up utm_source and HERMES_TEXT): run python3 build/variants.py" % name)
            for f in glob.glob(os.path.join(canon, "**", "*"), recursive=True):
                rel = os.path.relpath(f, canon)
                if os.path.isfile(f) and rel != "SKILL.md" and not rel.startswith("agents"):
                    copy = os.path.join(folder, rel)
                    want = open(f, "rb").read()
                    if harness == "hermes" and os.path.join(name, rel).replace(os.sep, "/") in variants.HERMES_TEXT:
                        want = variants.hermes_text(os.path.join(name, rel), want.decode("utf-8")).encode("utf-8")
                    if not os.path.isfile(copy) or open(copy, "rb").read() != want:
                        err(copy, "support file missing or different: run python3 build/variants.py")
            desc = data.get("description") or ""
            if data.get("name") != name or "SearchApi" not in desc[:BRAND_WITHIN]:
                err(path, "name must match and SearchApi must open the description")
            if harness == "hermes":
                if len(desc) > 60:
                    err(path, "Hermes description over 60 chars")
                if not data.get("version") or not data.get("author") or not ((data.get("metadata") or {}).get("hermes") or {}).get("tags"):
                    err(path, "Hermes needs version, author and metadata.hermes.tags")
                if any(str(e.get("optional")).lower() != "true" for e in data.get("required_environment_variables") or []):
                    err(path, "Hermes env vars must be optional: true (the script reads only the setup key file; MCP users need no key)")
            else:
                oc = (data.get("metadata") or {}).get("openclaw") or {}
                if len(desc) > 160:
                    err(path, "ClawHub description over 160 chars")
                if oc.get("primaryEnv") != "SEARCHAPI_API_KEY" or (oc.get("requires") or {}).get("env"):
                    err(path, "ClawHub needs primaryEnv SEARCHAPI_API_KEY and no requires.env")
                if not {"python3", "python", "py"} <= set((oc.get("requires") or {}).get("anyBins") or []):
                    err(path, "ClawHub requires.anyBins must list python3, python and py (Windows has py and python, rarely python3)")
                if any(v.get("required") is not False for v in oc.get("envVars") or [{}]):
                    err(path, "ClawHub envVars must be required: false")
                declared = {v.get("name") for v in oc.get("envVars") or []}
                missing = sorted(set(script_env()) - declared)
                if missing:
                    err(path, "ClawHub envVars must declare every env var the script reads (scan flags a mismatch): %s"
                        % ", ".join(missing))


def script_env():
    """Env var names the script reads: its os.environ.get calls plus the harness markers."""
    src = read(os.path.join(SKILLS, "searchapi", "scripts", "searchapi.py"))
    return sorted(set(re.findall(r'os\.environ\.get\(\s*"([A-Z][A-Z0-9_]*)"', src)) | {v for v, _ in sa.HARNESSES})


def check_commands():
    cmds = sorted(glob.glob(os.path.join(ROOT, "commands", "*.md")))
    if len(cmds) != 4:
        err(os.path.join(ROOT, "commands"), "expected 4 commands, found %d" % len(cmds))
    try:
        import tomllib
    except ImportError:
        tomllib = None
    for c in cmds:
        fm, data, body = frontmatter(c)
        if not data.get("description"):
            err(c, "command needs a description")
        if data.get("name") != os.path.basename(c)[:-3]:
            err(c, "command needs name: equal to its file name (Cursor's plugin validator)")
        toml = c[:-3] + ".toml"
        if not os.path.isfile(toml):
            err(toml, "missing: run python3 build/variants.py")
        elif tomllib:
            t = tomllib.loads(read(toml))
            if not t.get("prompt") or t.get("description") != data.get("description"):
                err(toml, "stale: run python3 build/variants.py")


README_INSTALL = [   # install commands that were tested and broke (install test 2026-10-16)
    (r"skills add \S+(?: -a \S+)* -a openclaw(?![^`|]*\s-g\b)", "OpenClaw needs -g: a project install lands in ./skills, which OpenClaw does not read"),
    (r"copilot plugin install [\w.-]+/[\w.-]+(?!@)", "Copilot direct repo installs are deprecated: use marketplace add, then install searchapi@searchapi"),
    (r"grok plugin install (?![^`|]*--trust)", "Grok refuses to install without --trust"),
]


def check_readme():
    path = os.path.join(ROOT, "README.md")
    for n, line in enumerate(read(path).split("\n"), 1):
        for rx, why in README_INSTALL:
            if re.search(rx, line):
                err(path, "line %d: %s" % (n, why))


def check_manifests(version):
    def load(rel):
        path = os.path.join(ROOT, rel)
        try:
            return json.loads(read(path))
        except (OSError, ValueError) as e:
            err(path, "bad JSON: %s" % e)
            return {}
    found = {
        ".claude-plugin/plugin.json": load(".claude-plugin/plugin.json").get("version"),
        ".claude-plugin/marketplace.json": [p.get("version") for p in load(".claude-plugin/marketplace.json").get("plugins", [])],
        ".cursor-plugin/plugin.json": load(".cursor-plugin/plugin.json").get("version"),
        "plugin.json": load("plugin.json").get("version"),
        "gemini-extension.json": load("gemini-extension.json").get("version"),
        "scripts/searchapi.py VERSION": sa.VERSION,
    }
    for rel, v in found.items():
        for x in v if isinstance(v, list) else [v]:
            if x != version:
                err(os.path.join(ROOT, rel.split(" ")[0]), "version %r, expected %s" % (x, version))
    for rel in (".claude-plugin/plugin.json", ".cursor-plugin/plugin.json", "plugin.json", "gemini-extension.json"):
        if load(rel).get("name") != "searchapi":
            err(os.path.join(ROOT, rel), "name must be searchapi")
    for rel in (".plugin/plugin.json", ".cursor-plugin/marketplace.json"):   # read by nobody, or shadowing the real one
        if os.path.exists(os.path.join(ROOT, rel)):
            err(os.path.join(ROOT, rel), "remove: Grok reads plugin.json or .claude-plugin/, cursor.directory reads .plugin/ "
                "first, and Cursor wants no marketplace.json for one plugin")
    cursor = load(".cursor-plugin/plugin.json")
    if set(cursor.get("author") or {}) - {"name", "email"} or not cursor.get("displayName"):
        err(os.path.join(ROOT, ".cursor-plugin/plugin.json"), "Cursor schema: author takes only name and email; add displayName")
    for rel in ("plugin.json", ".claude-plugin/plugin.json", ".cursor-plugin/plugin.json", ".claude-plugin/marketplace.json"):
        doc = load(rel)
        for kw in [doc.get("keywords") or []] + [p.get("keywords") or [] for p in doc.get("plugins") or []]:
            if {"mcp", "agent-skills"} & set(kw):
                err(os.path.join(ROOT, rel), "drop the generic keywords mcp and agent-skills (Kiro treats keywords as triggers)")
    short = (((load("plugin.json").get("extensions") or {}).get("com.openai") or {}).get("interface") or {}).get("shortDescription", "")
    if not 0 < len(short) <= 30:
        err(os.path.join(ROOT, "plugin.json"), "com.openai shortDescription must be 1 to 30 chars (OpenAI directory subtitle)")
    mcp = load("mcp.json")
    server = (mcp.get("mcpServers") or {}).get("searchapi") or {}
    if mcp.get("$schema") != "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json" or server != {
            "type": "streamable-http", "url": "https://www.searchapi.io/mcp"}:
        err(os.path.join(ROOT, "mcp.json"), "Agent Plugins mcp.json: $schema plus the MCP URL only (OAuth), no headers")
    if load("plugin.json").get("$schema") != "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json":
        err(os.path.join(ROOT, "plugin.json"), "Agent Plugins plugin.json needs its $schema")
    prompts = ((load("plugin.json").get("extensions") or {}).get("com.openai") or {}).get("interface", {}).get("defaultPrompt")
    if not prompts or len(prompts) > 3:
        err(os.path.join(ROOT, "plugin.json"), "extensions.com.openai.interface.defaultPrompt: 1 to 3 starter prompts")
    for rel in (".cursor-plugin/plugin.json", ".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json", "plugin.json"):
        text = read(os.path.join(ROOT, rel))
        if '"..' in text or '"/' in text:
            err(os.path.join(ROOT, rel), "manifest paths must be relative, with no .. and no absolute paths")
    for cmd in load(".cursor-plugin/plugin.json").get("commands") or []:
        if not os.path.isfile(os.path.join(ROOT, cmd)):
            err(os.path.join(ROOT, ".cursor-plugin/plugin.json"), "command %s does not exist" % cmd)
    if load(".mcp.json") != {"mcpServers": {"searchapi": {"type": "http", "url": "https://www.searchapi.io/mcp"}}}:
        err(os.path.join(ROOT, ".mcp.json"), "Claude Code .mcp.json: the SearchApi MCP URL only (type http, OAuth)")
    ui = ((load("plugin.json").get("extensions") or {}).get("com.openai") or {}).get("interface") or {}
    for k in ("composerIcon", "logo"):
        if not ui.get(k) or not os.path.isfile(os.path.join(ROOT, ui[k])):
            err(os.path.join(ROOT, "plugin.json"), "%s must be a committed relative path" % k)
    logo = load(".cursor-plugin/plugin.json").get("logo")
    if not logo or not os.path.isfile(os.path.join(ROOT, logo)):
        err(os.path.join(ROOT, ".cursor-plugin/plugin.json"), "logo must be a committed relative path")
    oai = os.path.join(SKILLS, "searchapi", "agents", "openai.yaml")
    y = sa._block(read(oai).split("\n"), 0, 0)[0]
    ui, tools = y.get("interface") or {}, (y.get("dependencies") or {}).get("tools") or []
    if "SearchApi" not in str(ui.get("short_description")) or not isinstance(ui.get("default_prompt"), str):
        err(oai, "interface needs short_description with SearchApi and one default_prompt string")
    if not 25 <= len(str(ui.get("short_description"))) <= 64:
        err(oai, "short_description should be 25 to 64 chars (Codex guidance)")
    if (y.get("policy") or {}).get("allow_implicit_invocation") is False:
        err(oai, "the core skill must stay implicitly invocable")
    if not tools or tools[0].get("type") != "mcp" or tools[0].get("transport") != "streamable_http" \
            or tools[0].get("url") != "https://www.searchapi.io/mcp":
        err(oai, "dependencies.tools must declare the SearchApi MCP (streamable_http)")


def main():
    only = sys.argv[sys.argv.index("--skill") + 1] if "--skill" in sys.argv else None
    version = json.loads(read(os.path.join(ROOT, ".claude-plugin", "plugin.json")))["version"]
    names = [only] if only else sorted(n for n in os.listdir(SKILLS) if os.path.isdir(os.path.join(SKILLS, n)))
    for n in names:
        check_skill(os.path.join(SKILLS, n), version)
        scan_text(os.path.join(SKILLS, n))
    if only:
        repo_text_rules([os.path.join(SKILLS, only)])
    else:
        repo_text_rules([ROOT])
        for top in ("hermes", "clawhub", "commands"):
            scan_text(os.path.join(ROOT, top), commands=top == "commands")
        check_variants(names)
        check_commands()
        check_manifests(version)
        check_readme()
    sizes = ", ".join("%s %d bytes" % (n, os.path.getsize(os.path.join(SKILLS, n, "SKILL.md"))) for n in names)
    if errors:
        print("\n".join(errors))
        print("lint: %d error(s) (%s)" % (len(errors), sizes))
        sys.exit(1)
    print("lint: ok (%s)" % sizes)


if __name__ == "__main__":
    main()
