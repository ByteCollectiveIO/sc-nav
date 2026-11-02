"""Markdown → HTML for Intelligence entries (docs/intelligence.md, backlog #50).

Rendered on the SERVER, never in the SPA: the single-file shell has no build
step, and one pipeline means Preview and Publish can't disagree
(`POST /api/intel/render` calls exactly this).

Two layers, belt and braces:
  1. markdown-it-py with raw HTML OFF and a small set of rules for our dialect.
     Nothing a member types can produce a tag the rules below don't emit.
  2. nh3 (ammonia) with an explicit allow-list, run over the result anyway. If
     a rule here ever emits something unexpected, the sanitizer drops it.

The dialect (the doc's "Formatting" table):
  * CommonMark + GFM tables + ~~strike~~.
  * `__x__` is UNDERLINE (Discord semantics), not CommonMark bold — members
    paste from Discord and the text has to look the same here.
  * Headings: `#` is the entry title's level, so it's demoted to h2; h4–h6
    fold to h3. Each gets a `sec-…` id (prefixed so a heading can never take
    an id the app itself uses, e.g. a heading "intel-view").
  * `- [ ]` / `- [x]` task items: display-only boxes with a stable id derived
    from the item text, so reordering doesn't scramble a later op's ticks.
  * Links: http(s) only, with the site's host shown after the text; bare URLs
    are linked too. Every <a> gets rel="noopener noreferrer nofollow".
  * Images: only from the caller's host list, and NEVER an <img>. They render
    as a click-to-load placeholder (a span carrying the URL); any other host
    renders as a plain link. With `own_files`, `/api/intel/files/<id>` (an
    image an admin import stored, slice 8) is a placeholder too, class
    `intel-file`: the SPA loads those inline (same origin, no consent).
  * `[[kind:id]]` suite references → a placeholder the SPA hydrates into a live
    chip per viewer (names, state and visibility stay current).
  * `<t:unix:fmt>` Discord timestamps → <time>, shown in the reader's zone.
  * `{red}text{/red}` text color, from a FIXED palette (COLORS) that maps to
    theme tokens in the SPA, so colored text stays readable in both themes.
    Never a free CSS color: no style attribute leaves this module. An
    unpaired or crossed marker stays as the literal text.

Pure module: no app/db imports, so it unit-tests without booting the app.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit

import nh3
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.token import Token

# Input cap for one body (an SOP with tables is a few KB; this is far above it).
MAX_BODY_CHARS = 100_000

# What a `[[kind:id]]` reference may point at. The id is digits only: every one
# of these is an integer row id, or a Discord snowflake for members.
REF_KINDS = ("poi", "zone", "warning", "event", "op", "entry", "member")
_REF_RE = re.compile(r"\[\[(" + "|".join(REF_KINDS) + r"):(\d{1,20})\]\]")

# Discord timestamp markup. The style letter is optional (Discord defaults to f).
_TS_RE = re.compile(r"<t:(-?\d{1,12})(?::([tTdDfFR]))?>")
# Discord accepts any int64; anything outside datetime's range would raise, and
# a NaN-style poison value must never reach a stored body (cf. _COORD_MAX).
_TS_MIN, _TS_MAX = -62_135_596_800, 253_402_300_799

# Bare URL in running text. Trailing punctuation stays outside the link.
_URL_RE = re.compile(r"https?://[^\s<>\[\]()\"']*[^\s<>\[\]()\"'.,;:!?*_~]")

_TASK_RE = re.compile(r"^\[([ xX])\][ \t]+")

# Text colors: name → nothing else. The SPA's `.intel-c-<name>` maps each to a
# theme token (red = --bad, orange = --warn, …).
COLORS = ("red", "orange", "yellow", "green", "blue", "purple")
_COLOR_RE = re.compile(r"\{(/?)(" + "|".join(COLORS) + r")\}")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _http_url(url: str) -> bool:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def section_slug(text: str) -> str:
    """ASCII slug for a heading anchor or an entry URL (`#/intel/e/<id>-<slug>`)."""
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")
    return slug[:60].rstrip("-") or "section"


# ---- core rules (run after inline parsing; they rewrite the token stream) ----

def _rule_headings(state) -> None:
    seen: dict[str, int] = {}
    toks = state.tokens
    for i, t in enumerate(toks):
        if t.type not in ("heading_open", "heading_close"):
            continue
        level = int(t.tag[1])
        t.tag = "h2" if level <= 2 else "h3"
        if t.type == "heading_open" and i + 1 < len(toks):
            # Color markers are formatting, not words: "## {red}Loadout{/red}" → sec-loadout.
            base = "sec-" + section_slug(_COLOR_RE.sub("", toks[i + 1].content))
            n = seen.get(base, 0) + 1
            seen[base] = n
            t.attrSet("id", base if n == 1 else f"{base}-{n}")


def _rule_underline(state) -> None:
    for t in state.tokens:
        if t.type != "inline" or not t.children:
            continue
        for c in t.children:
            if c.type in ("strong_open", "strong_close") and c.markup == "__":
                c.tag = "u"


def _rule_tasks(state) -> None:
    toks = state.tokens
    seen: dict[str, int] = {}
    for i, t in enumerate(toks):
        # list_item_open → paragraph_open → inline (tight lists keep the hidden paragraph)
        if t.type != "list_item_open" or i + 2 >= len(toks):
            continue
        para, inline = toks[i + 1], toks[i + 2]
        if para.type != "paragraph_open" or inline.type != "inline" or not inline.children:
            continue
        first = inline.children[0]
        m = _TASK_RE.match(first.content) if first.type == "text" else None
        if not m:
            continue
        first.content = first.content[m.end():]
        inline.content = inline.content[m.end():]
        text = " ".join(inline.content.split()).lower()
        base = "t-" + hashlib.sha1(text.encode()).hexdigest()[:8]
        n = seen.get(base, 0) + 1
        seen[base] = n
        t.attrSet("class", "task-item")
        t.attrSet("data-task-id", base if n == 1 else f"{base}-{n}")
        box = Token("intel_task", "", 0)
        box.meta = {"done": m.group(1) != " "}
        inline.children.insert(0, box)


def _rule_bare_urls(state) -> None:
    for t in state.tokens:
        if t.type != "inline" or not t.children:
            continue
        out: list[Token] = []
        depth = 0
        for c in t.children:
            if c.type == "link_open":
                depth += 1
            elif c.type == "link_close":
                depth -= 1
            if c.type != "text" or depth > 0 or "http" not in c.content:
                out.append(c)
                continue
            pos = 0
            for m in _URL_RE.finditer(c.content):
                if m.start() > pos:
                    pre = Token("text", "", 0)
                    pre.content = c.content[pos:m.start()]
                    out.append(pre)
                url = m.group(0)
                o = Token("link_open", "a", 1)
                o.attrSet("href", url)
                o.markup = "linkify"
                txt = Token("text", "", 0)
                txt.content = url
                cl = Token("link_close", "a", -1)
                cl.markup = "linkify"
                out += [o, txt, cl]
                pos = m.end()
            if pos == 0:
                out.append(c)
            elif pos < len(c.content):
                tail = Token("text", "", 0)
                tail.content = c.content[pos:]
                out.append(tail)
        t.children = out


# ---- inline rules (claim their syntax before CommonMark sees it) ----

def _inline_ref(state, silent: bool) -> bool:
    if not state.src.startswith("[[", state.pos):
        return False
    m = _REF_RE.match(state.src, state.pos)
    if not m:
        return False
    if not silent:
        tok = state.push("intel_ref", "", 0)
        tok.meta = {"kind": m.group(1), "id": m.group(2)}
        state.env.setdefault("refs", []).append((m.group(1), m.group(2)))
    state.pos = m.end()
    return True


def _inline_ts(state, silent: bool) -> bool:
    if not state.src.startswith("<t:", state.pos):
        return False
    m = _TS_RE.match(state.src, state.pos)
    if not m:
        return False
    ts = int(m.group(1))
    if not (_TS_MIN <= ts <= _TS_MAX):
        return False
    if not silent:
        tok = state.push("intel_ts", "", 0)
        tok.meta = {"ts": ts, "fmt": m.group(2) or "f", "raw": m.group(0)}
    state.pos = m.end()
    return True


def _inline_color(state, silent: bool) -> bool:
    if state.src[state.pos] != "{":
        return False
    m = _COLOR_RE.match(state.src, state.pos)
    if not m:
        return False
    if not silent:
        tok = state.push("intel_color", "", 0)
        tok.meta = {"close": bool(m.group(1)), "color": m.group(2)}
        tok.content = m.group(0)
    state.pos = m.end()
    return True


def _rule_colors(state) -> None:
    """Pair color markers within each inline run (a stack, so they nest and
    can't cross). Anything left unpaired turns back into its literal text."""
    for t in state.tokens:
        if t.type != "inline" or not t.children:
            continue
        stack, paired = [], set()
        for i, c in enumerate(t.children):
            if c.type != "intel_color":
                continue
            if not c.meta["close"]:
                stack.append(i)
            elif stack and t.children[stack[-1]].meta["color"] == c.meta["color"]:
                paired.update((stack.pop(), i))
            else:
                stack.clear()       # crossed or stray close: drop everything open
        for i, c in enumerate(t.children):
            if c.type == "intel_color" and i not in paired:
                c.type = "text"


# ---- renderers for the tokens above, plus links and images ----

def _render_color(self, tokens, idx, options, env) -> str:
    m = tokens[idx].meta
    return "</span>" if m["close"] else f'<span class="intel-c intel-c-{m["color"]}">'


def _render_ref(self, tokens, idx, options, env) -> str:
    k, i = tokens[idx].meta["kind"], tokens[idx].meta["id"]
    return (f'<span class="intel-ref" data-ref-kind="{k}" data-ref-id="{i}">'
            f"[[{k}:{i}]]</span>")


def _render_ts(self, tokens, idx, options, env) -> str:
    m = tokens[idx].meta
    dt = datetime.fromtimestamp(m["ts"], tz=timezone.utc)
    iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f'<time datetime="{iso}" data-fmt="{m["fmt"]}">'
            f'{dt.strftime("%Y-%m-%d %H:%M")} UTC</time>')


def _render_task(self, tokens, idx, options, env) -> str:
    done = tokens[idx].meta["done"]
    label = "done" if done else "not done"
    return (f'<span class="intel-task" data-done="{1 if done else 0}" '
            f'role="img" aria-label="{label}"></span>')


def _render_link_close(self, tokens, idx, options, env) -> str:
    # Find the matching open (links don't nest) to show the site after the text.
    j = idx - 1
    while j >= 0 and tokens[j].type != "link_open":
        j -= 1
    if j < 0 or tokens[j].markup in ("linkify", "autolink"):
        return "</a>"
    host = _host(tokens[j].attrGet("href") or "")
    return f'</a> <span class="ext-host">({escapeHtml(host)})</span>' if host else "</a>"


_OWN_FILE_RE = re.compile(r"^/api/intel/files/(\d{1,12})$")


def _render_image(self, tokens, idx, options, env) -> str:
    tok = tokens[idx]
    src = tok.attrGet("src") or ""
    alt = self.renderInlineAsText(tok.children or [], options, env)
    m = _OWN_FILE_RE.match(src)
    if m and not env.get("own_files"):
        return escapeHtml(alt or "image")      # a comment: stored images only show in entries
    if m:
        env.setdefault("files", []).append(int(m.group(1)))
        return (f'<span class="intel-file" data-src="/api/intel/files/{int(m.group(1))}" '
                f'data-alt="{escapeHtml(alt)}">{escapeHtml(alt or "image")}</span>')
    host = _host(src)
    if src.lower().startswith("https://") and host in env.get("image_hosts", ()):
        env["images"] = env.get("images", 0) + 1
        return (f'<span class="intel-img" data-src="{escapeHtml(src)}" '
                f'data-alt="{escapeHtml(alt)}">{escapeHtml(alt or "image")}'
                f' <span class="ext-host">({escapeHtml(host)})</span></span>')
    text = alt or src
    return (f'<a href="{escapeHtml(src)}">{escapeHtml(text)}</a>'
            f' <span class="ext-host">({escapeHtml(host)})</span>')


def _build() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    # http(s), plus the one same-origin path a stored image lives at (slice 8).
    md.validateLink = lambda u: _http_url(u) or bool(_OWN_FILE_RE.match(u.strip()))
    md.inline.ruler.before("link", "intel_ref", _inline_ref)
    md.inline.ruler.before("autolink", "intel_ts", _inline_ts)
    md.inline.ruler.before("emphasis", "intel_color", _inline_color)
    md.core.ruler.push("intel_headings", _rule_headings)
    md.core.ruler.push("intel_underline", _rule_underline)
    md.core.ruler.push("intel_tasks", _rule_tasks)
    md.core.ruler.push("intel_bare_urls", _rule_bare_urls)
    md.core.ruler.push("intel_colors", _rule_colors)
    md.add_render_rule("intel_ref", _render_ref)
    md.add_render_rule("intel_ts", _render_ts)
    md.add_render_rule("intel_task", _render_task)
    md.add_render_rule("intel_color", _render_color)
    md.add_render_rule("link_close", _render_link_close)
    md.add_render_rule("image", _render_image)
    return md


_MD = _build()

_TAGS = {"p", "br", "h2", "h3", "strong", "em", "u", "s", "ul", "ol", "li",
         "blockquote", "code", "pre", "table", "thead", "tbody", "tr", "th", "td",
         "hr", "a", "span", "time"}
_ATTRS = {
    "a": {"href"},
    "h2": {"id"}, "h3": {"id"},
    "ol": {"start"},
    "li": {"data-task-id"},
    "th": {"style"}, "td": {"style"},
    "span": {"data-ref-kind", "data-ref-id", "data-src", "data-alt", "data-done",
             "role", "aria-label"},
    "time": {"datetime", "data-fmt"},
}
_CLASSES = {"span": {"intel-ref", "intel-img", "intel-file", "intel-task", "ext-host", "intel-c",
                     *(f"intel-c-{c}" for c in COLORS)},
            "li": {"task-item"}}


def sanitize(html: str) -> str:
    """The trust boundary. Everything stored or served as entry HTML passes here."""
    return nh3.clean(
        html, tags=_TAGS, attributes=_ATTRS, allowed_classes=_CLASSES,
        url_schemes={"http", "https"}, link_rel="noopener noreferrer nofollow",
        strip_comments=True, filter_style_properties={"text-align"},
        clean_content_tags={"script", "style"},
    )


def render(src: str, image_hosts=(), own_files: bool = False) -> dict:
    """Render one Markdown body. Returns {html, refs, images, files}:
    html   — sanitized HTML, safe to insert as-is (never an <img>);
    refs   — [(kind, id), …] in order, de-duplicated (feeds intel_links);
    images — how many allowed-host image placeholders it holds;
    files  — stored image ids it shows (only with `own_files`: entry bodies,
             never comments), de-duplicated (feeds intel_file_refs)."""
    env = {"image_hosts": tuple(h.lower() for h in image_hosts), "own_files": own_files}
    html = _MD.render(src[:MAX_BODY_CHARS], env)
    refs = list(dict.fromkeys(env.get("refs", [])))
    return {"html": sanitize(html), "refs": refs, "images": env.get("images", 0),
            "files": list(dict.fromkeys(env.get("files", [])))}
