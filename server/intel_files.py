"""Intelligence portability (#50 slice 8): stored images, export and import.

Pure module (no app/db imports), so it unit-tests without booting the app:

  * `reencode(data)` — the one way an image gets onto the data volume. Sniffed
    first (PNG, JPEG, WebP, GIF; never SVG), a pixel cap set BEFORE any decode
    (Pillow's decompression-bomb guard raised to an error), then re-encoded to
    WebP with the longest edge ≤ 2560 px, which also drops EXIF. A small
    animated GIF is kept as it is; a big one keeps only its first frame.
  * `scan_zip(path, …)` — opens an uploaded import defensively: no absolute or
    `..` paths, no symlinks, no nested zips, a file-count cap and a total
    uncompressed-size cap. Only Markdown and images are read.
  * `parse_front_matter` / `dump_front_matter` — the fields Export writes and
    Import reads (values are JSON, which is valid YAML, so a round trip is
    exact and a hand-written `kind: tactic` still parses).
  * `image_targets(md)` / `rewrite_images(md, mapping)` — the images a file
    points at (relative paths, and the base64 `data:` URIs Google Docs'
    Markdown export uses) and their replacement with stored-file URLs.

docs/intelligence.md "Images" + "Export".
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import posixpath
import re
import stat
import warnings
import zipfile
from urllib.parse import unquote

MAX_PIXELS = 40_000_000          # decoded size cap (40 MP), checked before decode
MAX_EDGE = 2560                  # longest edge after re-encode
GIF_KEEP_MAX = 4 * 1024 * 1024   # an animated GIF up to this size is kept as is
WEBP_QUALITY = 82
MAX_ZIP_FILES = 2000
MAX_IMAGE_IN = 40 * 1024 * 1024  # one source image (zip member or data: URI)
MAX_MD_BYTES = 2 * 1024 * 1024   # one Markdown file
IMAGE_EXTS = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "webp", "gif": "gif"}
MD_EXTS = {"md", "markdown"}


class ImageError(ValueError):
    """An image we won't store: the message says why, for the import report."""


def sniff(data: bytes) -> str | None:
    """The real format from the magic bytes: png | jpg | webp | gif | None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


_PIL_FORMATS = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP", "gif": "GIF"}


def reencode(data: bytes) -> tuple[bytes, str, int, int]:
    """(bytes, mime, width, height) to store, or ImageError."""
    from PIL import Image          # imported here: only an import ever needs it

    if len(data) > MAX_IMAGE_IN:
        raise ImageError("larger than 40 MB")
    kind = sniff(data)
    if not kind:
        raise ImageError("not a PNG, JPEG, WebP or GIF")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data), formats=[_PIL_FORMATS[kind]])
            w, h = img.size
            if w * h > MAX_PIXELS:
                raise ImageError("more than 40 megapixels")
            animated = kind == "gif" and getattr(img, "n_frames", 1) > 1
            if animated and len(data) <= GIF_KEEP_MAX:
                img.close()
                return data, "image/gif", w, h
            img.seek(0)
            img.load()
            has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
            img = img.convert("RGBA" if has_alpha else "RGB")
            img.thumbnail((MAX_EDGE, MAX_EDGE))
            out = io.BytesIO()
            img.save(out, "WEBP", quality=WEBP_QUALITY, method=4)
            return out.getvalue(), "image/webp", img.size[0], img.size[1]
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError("more than 40 megapixels")
    except Exception as exc:       # noqa: BLE001 — any decoder failure is "can't read it"
        raise ImageError(f"couldn't be read ({type(exc).__name__})")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- the zip ----
def _norm(name: str) -> str | None:
    """A member path made safe and comparable, or None when it's unsafe."""
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name.rsplit("/", 1)[-1] else ""


def scan_zip(path, max_files: int = MAX_ZIP_FILES, max_bytes: int | None = None) -> dict:
    """Open an import zip. Returns {"md": {name: text}, "images": {name: ZipInfo},
    "skipped": [(name, reason)], "zip": ZipFile}. Raises ValueError for a zip
    we won't read at all (unsafe paths, too many files, too big)."""
    zf = zipfile.ZipFile(path)
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > max_files:
        zf.close()
        raise ValueError(f"The zip holds {len(infos)} files; the limit is {max_files}.")
    total = sum(i.file_size for i in infos)
    if max_bytes is not None and total > max_bytes:
        zf.close()
        raise ValueError(f"Unpacked, the zip is {total // (1024 * 1024)} MB, more than this server can take now.")
    md, images, skipped = {}, {}, []
    for i in infos:
        name = _norm(i.filename)
        if name is None:
            zf.close()
            raise ValueError(f"Unsafe path in the zip: {i.filename[:120]}")
        mode = (i.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            skipped.append((name, "a symbolic link"))
            continue
        ext = _ext(name)
        if name.startswith("__MACOSX/") or name.rsplit("/", 1)[-1].startswith("._"):
            continue                      # macOS resource forks: noise, not content
        if ext == "zip":
            skipped.append((name, "a zip inside the zip"))
        elif ext in MD_EXTS:
            if i.file_size > MAX_MD_BYTES:
                skipped.append((name, "Markdown file larger than 2 MB"))
                continue
            raw = zf.read(i)
            md[name] = raw.decode("utf-8-sig", errors="replace")
        elif ext in IMAGE_EXTS:
            images[name] = i
        else:
            skipped.append((name, "not Markdown or an image"))
    return {"md": md, "images": images, "skipped": skipped, "zip": zf}


# ---- front matter ----
FRONT_KEYS = ("title", "kind", "summary", "activity", "system", "tags", "visibility", "patch",
              "grade", "valid_until", "header", "id", "published", "revised", "rev", "author")


def dump_front_matter(meta: dict) -> str:
    lines = ["---"]
    for k in FRONT_KEYS:
        v = meta.get(k)
        if v is None or v == "" or v == []:
            continue
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def parse_front_matter(text: str) -> tuple[dict, str]:
    """({key: value}, body). Values are JSON when they parse, else the raw
    string; `tags: a, b` (a bare list) splits on commas."""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if not k:
            continue
        try:
            meta[k] = json.loads(v)
        except ValueError:
            meta[k] = v.strip("'\"")
    if isinstance(meta.get("tags"), str):
        meta["tags"] = [t.strip() for t in meta["tags"].strip("[]").split(",") if t.strip()]
    return meta, text[m.end():]


def title_from(body: str, filename: str) -> tuple[str, str]:
    """A file without front matter: its first `# Heading` is the title (and is
    dropped from the body); else the file name, minus Notion's id suffix."""
    m = re.match(r"^\s*#[ \t]+(.+?)[ \t#]*\r?\n", body)
    if m:
        return m.group(1).strip()[:140], body[m.end():]
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = re.sub(r"\s+[0-9a-f]{32}$", "", stem)          # "Escort SOP 1a2b…" (Notion)
    return (stem.replace("_", " ").strip() or "Untitled")[:140], body


# ---- images in the Markdown ----
_INLINE_IMG = re.compile(r"!\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^)\s]+)(\s+\"[^\"\n]*\")?\s*\)")
_REF_DEF = re.compile(r"^\[([^\]\n]+)\]:[ \t]*(<[^>\n]+>|\S+)[^\n]*$", re.M)
_DATA_URI = re.compile(r"^data:image/(png|jpe?g|webp|gif);base64,([A-Za-z0-9+/=\s]+)$", re.I)


def _clean_target(t: str) -> str:
    t = t.strip()
    return t[1:-1].strip() if t.startswith("<") and t.endswith(">") else t


def image_targets(md: str) -> list[str]:
    """Every image destination the file uses: inline `![…](x)` targets plus
    reference definitions that hold a data: URI (Google Docs' export)."""
    out = [_clean_target(m.group(2)) for m in _INLINE_IMG.finditer(md)]
    out += [_clean_target(m.group(2)) for m in _REF_DEF.finditer(md) if _clean_target(m.group(2)).startswith("data:")]
    return list(dict.fromkeys(out))


def data_uri_bytes(target: str) -> bytes | None:
    m = _DATA_URI.match(target)
    if not m:
        return None
    raw = re.sub(r"\s+", "", m.group(2))
    if len(raw) * 3 // 4 > MAX_IMAGE_IN:
        raise ImageError("larger than 40 MB")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ImageError("a broken data: image")


def resolve(md_name: str, target: str) -> str | None:
    """A relative image path in `md_name` → the zip member name it means."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("/"):
        return None
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    base = posixpath.dirname(md_name)
    return _norm(posixpath.normpath(posixpath.join(base, target)))


def rewrite_images(md: str, mapping: dict[str, str]) -> str:
    """Replace image destinations found in `mapping` ({old target: new url});
    inline images and reference definitions alike. Others stay as written."""
    def inline(m):
        t = _clean_target(m.group(2))
        if t not in mapping:
            return m.group(0)
        return f"![{m.group(1)}]({mapping[t]})"

    def refdef(m):
        t = _clean_target(m.group(2))
        if t not in mapping:
            return m.group(0)
        return f"[{m.group(1)}]: {mapping[t]}"
    return _REF_DEF.sub(refdef, _INLINE_IMG.sub(inline, md))


def drop_data_uris(md: str) -> str:
    """A data: image we didn't store becomes a short placeholder, not 2 MB of
    base64 in the body (it would blow the body limit and never render)."""
    md = _INLINE_IMG.sub(lambda m: f"*[image: {m.group(1) or 'not imported'}]*"
                         if _clean_target(m.group(2)).startswith("data:") else m.group(0), md)
    return _REF_DEF.sub(lambda m: "" if _clean_target(m.group(2)).startswith("data:") else m.group(0), md)
