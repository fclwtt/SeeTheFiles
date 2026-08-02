#!/usr/bin/env python3
"""SeeTheFiles — 目录结构可视化工具 (CAP-1..CAP-5).

Self-contained Python CLI. Scans a folder, builds a directory tree JSON, and renders a
self-contained HTML mind-map view (opened in the default browser) for that folder.

Rendering choice (HOW decision Q1): self-contained HTML opened in browser as a popup.
Preview choice (HOW decision Q2): file contents are EMBEDDED into the HTML (no local server),
naturally satisfying the SPEC local-preview safety boundary (no port, no 127.0.0.1, destroyed
on process exit, nothing leaked to disk beyond the temp HTML which is regenerated per run).
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import sys
import tempfile
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.request import url2pathname

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DEFAULT_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash", ".zsh", ".fish", ".bat",
    ".cmd", ".ps1", ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".java", ".go", ".rs",
    ".rb", ".php", ".pl", ".lua", ".sql", ".html", ".htm", ".css", ".scss", ".less",
    ".xml", ".svg", ".csv", ".log", ".rst", ".tex", ".r", ".kt", ".swift", ".dart",
    ".vue", ".dockerfile", ".gitignore", ".env",
}

DEFAULT_IGNORE_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", "dist", "build",
}

# Office Open XML (ZIP-packaged XML) and PDF: binary on disk but still
# previewable. Office text is extracted with stdlib zipfile + ElementTree
# (zero third-party deps); PDF is embedded as a base64 data URI so the
# browser's built-in PDF viewer renders it inline. Using data: URIs (never
# file: paths) for embedded media also avoids the "file: URLs are treated as
# unique security origins" restriction — the embed is same-document content,
# not a separate file:// origin the browser would block.
OFFICE_KIND = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx"}
MAX_PDF_EMBED_BYTES = 8 * 1024 * 1024  # per-file cap for inline PDF embed

# XML namespaces for OOXML text extraction (WordprocessingML / DrawingML / SpreadsheetML).
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


@dataclass
class Config:
    ignore_names: set = field(default_factory=lambda: set(DEFAULT_IGNORE_NAMES))
    max_depth: int = 12
    max_files: int = 100000
    max_preview_bytes: int = 524288
    text_extensions: set = field(default_factory=lambda: set(DEFAULT_TEXT_EXTENSIONS))
    accent: str = "#6366f1"
    # Budget (in bytes) for total embedded preview content. Beyond this budget,
    # text files still render in the tree but preview shows a friendly notice.
    # Bounding by BYTES (not file count) prevents the 5000 x 512KB = 2.5GB OOM path.
    max_embed_bytes: int = 64 * 1024 * 1024
    max_embed_files: int = 20000  # hard secondary cap on embedded file count


def load_config(explicit_path: Optional[str]) -> Config:
    """Load configuration from an optional TOML file. Falls back to defaults."""
    cfg = Config()
    cfg_path = None
    if explicit_path:
        cfg_path = Path(explicit_path)
    else:
        # Look for config.toml next to the running executable.
        # NOTE: when frozen by PyInstaller (--onefile), __file__ points into the
        # temporary _MEI unpack dir, NOT the install folder. Use sys.argv[0] to
        # locate the real .exe folder where config.toml is shipped.
        base = Path(sys.argv[0]).resolve().parent
        candidate = base / "config.toml"
        if candidate.is_file():
            cfg_path = candidate
    if cfg_path and cfg_path.is_file():
        try:
            data = _load_toml(cfg_path)
        except Exception as exc:  # pragma: no cover - defensive
            sys.stderr.write(f"[warn] failed to parse config {cfg_path}: {exc}\n")
            return cfg
        scan = data.get("scan", {})
        if "ignore_names" in scan:
            cfg.ignore_names = set(scan["ignore_names"])
        if "max_depth" in scan:
            cfg.max_depth = int(scan["max_depth"])
        if "max_files" in scan:
            cfg.max_files = int(scan["max_files"])
        prev = data.get("preview", {})
        if "max_preview_bytes" in prev:
            cfg.max_preview_bytes = int(prev["max_preview_bytes"])
        if "text_extensions" in prev:
            cfg.text_extensions = {e.lower() for e in prev["text_extensions"]}
        if "max_embed_files" in prev:
            cfg.max_embed_files = int(prev["max_embed_files"])
        if "max_embed_bytes" in prev:
            cfg.max_embed_bytes = int(prev["max_embed_bytes"])
        ui = data.get("ui", {})
        if "accent" in ui:
            cfg.accent = str(ui["accent"])
    return cfg


def _load_toml(path: Path) -> dict:
    """Minimal TOML parser using stdlib tomllib when available (3.11+), else a tiny fallback."""
    try:
        import tomllib  # type: ignore
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except ModuleNotFoundError:
        # Fallback: only used on <3.11. Use a very small inline parser for our known schema.
        return _mini_toml(parse := path.read_text(encoding="utf-8"))


def _mini_toml(text: str) -> dict:  # pragma: no cover - fallback only
    out: dict = {}
    section = out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            key = line[1:-1].strip()
            section = out.setdefault(key, {})
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                items = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
                section[k] = items
            else:
                section[k] = v.strip('"').strip("'")
    return out


# --------------------------------------------------------------------------- #
# File classification (HOW decision Q3)
# --------------------------------------------------------------------------- #

# Bytes that are strongly indicative of binary content.
_NON_TEXT_BYTES = frozenset(range(0, 9)) | frozenset(range(11, 13)) | frozenset(range(14, 32))


def is_text_extension(name: str, cfg: Config) -> bool:
    ext = os.path.splitext(name)[1].lower()
    if ext in cfg.text_extensions:
        return True
    # .dockerfile / .gitignore style: no ext but known basename
    base = name.lower()
    return base in cfg.text_extensions


def sniff_text(path: Path, chunk_size: int = 8192) -> bool:
    """Content sniffing: read a small head and reject if it contains non-text control bytes."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(chunk_size)
    except OSError:
        return False
    if not head:
        return True  # empty file is text
    # Reject NUL bytes (strong binary signal) or too many control chars.
    if b"\x00" in head:
        return False
    non_text = sum(1 for b in head if b in _NON_TEXT_BYTES)
    return (non_text / len(head)) < 0.05


def classify_file(path: Path, size: int, cfg: Config) -> str:
    """Return file kind.

    - 'text' / 'large': text content (large = exceeds preview byte cap, truncated).
    - 'pdf': PDF document — embedded as base64 data URI for the browser viewer.
    - 'docx' / 'xlsx' / 'pptx': Office OOXML — text extracted via stdlib zipfile.
    - 'binary': not previewable.
    """
    ext = os.path.splitext(path.name)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in OFFICE_KIND:
        return OFFICE_KIND[ext]
    if size > cfg.max_preview_bytes:
        # Could still be text; classify by extension/sniff but flag as large.
        if is_text_extension(path.name, cfg) or sniff_text(path):
            return "large"
        return "binary"
    if is_text_extension(path.name, cfg) or sniff_text(path):
        return "text"
    return "binary"


# --------------------------------------------------------------------------- #
# Scanner (CAP-4: big-directory protection + symlink cycle guard)
# --------------------------------------------------------------------------- #

@dataclass
class ScanResult:
    tree: dict
    total_files: int = 0
    total_dirs: int = 0
    truncated_by_files: bool = False
    truncated_by_depth: bool = False
    skipped_symlink_loops: int = 0


def scan_directory(root: Path, cfg: Config) -> ScanResult:
    """Walk `root` and build a nested tree dict.

    Protection:
      * ignore_names skipped (case-insensitive).
      * max_depth cap (root = depth 0).
      * max_files hard cap (graceful truncation, no crash).
      * symlink cycle guard via a set of visited realpaths (no infinite recursion / no stack overflow).
    """
    root = root.resolve()
    result = ScanResult(tree={})
    # Stack of realpaths of *real* directories currently on the recursion path.
    # Used to detect symlink loops pointing at an ancestor (cycle guard).
    real_dir_stack: list = []

    def make_node(name: str, is_dir: bool) -> dict:
        return {
            "name": name,
            "type": "dir" if is_dir else "file",
            "children": [] if is_dir else None,
            "size": None,
            "kind": None,  # 'text' | 'large' | 'binary' (files only)
            "truncated": False,
        }

    def recurse(current: Path, node: dict, depth: int) -> None:
        try:
            entries = list(os.scandir(current))
        except (PermissionError, OSError):
            return
        # Sort: directories first, then files; alphabetical within each group.
        entries.sort(key=lambda e: (not e.is_dir(), e.name.lower()))
        for entry in entries:
            if result.total_files + result.total_dirs >= cfg.max_files:
                result.truncated_by_files = True
                node["truncated"] = True
                return
            name = entry.name
            if name in cfg.ignore_names or name.lower() in {n.lower() for n in cfg.ignore_names}:
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_link = entry.is_symlink()
            except OSError:
                continue

            # A symlink (even one pointing at a directory) reports is_dir()=False
            # when follow_symlinks=False, so branch on is_link FIRST to catch
            # directory-symlinks and apply the cycle guard.
            if is_link:
                child = make_node(name, True)
                node["children"].append(child)
                result.total_dirs += 1
                # Guard against symlink loops: if the link target's realpath is
                # already on the real-directory recursion path, it's a cycle.
                try:
                    real = Path(os.path.realpath(entry.path))
                except OSError:
                    continue
                if real in set(real_dir_stack):
                    result.skipped_symlink_loops += 1
                    child["kind"] = "symlink-loop"
                    continue
                if depth + 1 > cfg.max_depth:
                    child["truncated"] = True
                    result.truncated_by_depth = True
                else:
                    real_dir_stack.append(real)
                    recurse(entry.path, child, depth + 1)
                    real_dir_stack.pop()
            elif is_dir:
                child = make_node(name, True)
                node["children"].append(child)
                result.total_dirs += 1
                if depth + 1 > cfg.max_depth:
                    child["truncated"] = True
                    result.truncated_by_depth = True
                else:
                    try:
                        real = Path(os.path.realpath(entry.path))
                    except OSError:
                        real = None
                    if real is not None:
                        real_dir_stack.append(real)
                    recurse(entry.path, child, depth + 1)
                    if real is not None:
                        real_dir_stack.pop()
            else:
                # File
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                child = make_node(name, False)
                child["size"] = size
                child["kind"] = classify_file(Path(entry.path), size, cfg)
                node["children"].append(child)
                result.total_files += 1

    root_node = make_node(root.name, True)
    root_node["root_path"] = str(root)
    real_dir_stack.append(root)
    recurse(root, root_node, 0)
    real_dir_stack.pop()
    result.tree = root_node
    return result


# --------------------------------------------------------------------------- #
# Preview content extraction (CAP-3)
# --------------------------------------------------------------------------- #

def _extract_ooxml_text(path: Path, kind: str) -> str:
    """Extract readable text from a .docx/.xlsx/.pptx (OOXML = ZIP + XML).

    Uses only the standard library (zipfile + ElementTree). Output is plain
    text with newlines per paragraph / shared-string / slide. Formatting,
    images and table layout are lost — this is a content preview, not a
    fidelity renderer. Returns "" on any failure (caller treats empty as
    "could not extract" and falls back to a binary notice).
    """
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return ""
    out: list[str] = []
    try:
        if kind == "docx":
            try:
                root = ET.fromstring(zf.read("word/document.xml"))
            except KeyError:
                return ""
            for p in root.iter(f"{{{_W_NS}}}p"):
                line = "".join(t.text for t in p.iter(f"{{{_W_NS}}}t") if t.text)
                out.append(line)
            return "\n".join(out)
        if kind == "pptx":
            slide_names = sorted(
                n for n in zf.namelist()
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            )
            for idx, sn in enumerate(slide_names, 1):
                try:
                    root = ET.fromstring(zf.read(sn))
                except KeyError:
                    continue
                texts = [t.text for t in root.iter(f"{{{_A_NS}}}t") if t.text]
                if texts:
                    out.append(f"── 幻灯片 {idx} ──")
                    out.append("\n".join(texts))
            return "\n".join(out)
        if kind == "xlsx":
            try:
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            except KeyError:
                return ""
            for si in root.iter(f"{{{_S_NS}}}si"):
                line = "".join(t.text for t in si.iter(f"{{{_S_NS}}}t") if t.text)
                out.append(line)
            return "\n".join(out)
    except ET.ParseError:
        return ""
    finally:
        zf.close()
    return ""


def extract_preview(path: Path, cfg: Config) -> dict:
    """Return preview payload for a text/large/pdf/office file.

    Returns dict with keys: ok(bool), kind, content(str|None), reason(str|None),
    size(int), truncated(bool).
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"ok": False, "kind": "error", "content": None, "reason": f"无法读取文件: {exc}", "size": 0, "truncated": False}

    kind = classify_file(path, size, cfg)

    # PDF: embed raw bytes as base64 so the browser's built-in viewer renders
    # it inline via a data: URI. data: is NOT a file: URL, so this sidesteps
    # the "file: URLs are unique security origins" cross-origin block.
    if kind == "pdf":
        try:
            with open(path, "rb") as fh:
                raw = fh.read(MAX_PDF_EMBED_BYTES + 1)
        except OSError as exc:
            return {"ok": False, "kind": "error", "content": None, "reason": f"读取失败: {exc}", "size": size, "truncated": False}
        if len(raw) > MAX_PDF_EMBED_BYTES:
            return {"ok": False, "kind": "pdf-large", "content": None,
                    "reason": f"PDF 超过 {MAX_PDF_EMBED_BYTES // (1024 * 1024)}MB 内嵌上限，未预览，请在外部打开。",
                    "size": size, "truncated": False}
        return {"ok": True, "kind": "pdf", "content": base64.b64encode(raw).decode("ascii"),
                "reason": None, "size": size, "truncated": False}

    # Office OOXML: extract text via stdlib zipfile + ElementTree.
    if kind in ("docx", "xlsx", "pptx"):
        text = _extract_ooxml_text(path, kind)
        if not text:
            return {"ok": False, "kind": "binary", "content": None,
                    "reason": "无法从 Office 文件提取文本（可能已损坏或非标准 OOXML）。",
                    "size": size, "truncated": False}
        enc = text.encode("utf-8")
        truncated = len(enc) > cfg.max_preview_bytes
        if truncated:
            text = enc[:cfg.max_preview_bytes].decode("utf-8", errors="ignore")
        return {"ok": True, "kind": kind, "content": text, "reason": None,
                "size": size, "truncated": truncated}

    if kind == "binary":
        return {"ok": False, "kind": "binary", "content": None,
                "reason": "这是一个二进制文件，SeeTheFiles 仅支持文本 / Office / PDF 预览。",
                "size": size, "truncated": False}
    # text or large -> read (capped)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(cfg.max_preview_bytes + 1)
    except OSError as exc:
        return {"ok": False, "kind": "error", "content": None, "reason": f"读取失败: {exc}", "size": size, "truncated": False}
    truncated = len(raw) > cfg.max_preview_bytes
    if truncated:
        raw = raw[:cfg.max_preview_bytes]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return {"ok": False, "kind": "binary", "content": None,
                    "reason": "文件含无法解码的字节，疑似二进制。", "size": size, "truncated": False}
    return {"ok": True, "kind": kind, "content": text, "reason": None, "size": size, "truncated": truncated}


# --------------------------------------------------------------------------- #
# HTML rendering (CAP-2 mind-map view + CAP-3 preview panel)
# --------------------------------------------------------------------------- #

ICON_COLORS = {
    "py": "#3572A5", "js": "#f1e05a", "ts": "#3178c6", "tsx": "#3178c6", "jsx": "#f1e05a",
    "json": "#cbcb41", "md": "#083fa1", "html": "#e34c26", "htm": "#e34c26", "css": "#563d7c",
    "yaml": "#cb171e", "yml": "#cb171e", "toml": "#9c4221", "txt": "#89a52b", "cfg": "#6d8086",
    "ini": "#6d8086", "sh": "#89e051", "c": "#555555", "h": "#555555", "cpp": "#f34b7d",
    "go": "#00ADD8", "rs": "#dea584", "java": "#b07219", "rb": "#701516", "php": "#4F5D95",
    "pdf": "#ef4444", "docx": "#2563eb", "doc": "#2563eb", "xlsx": "#16a34a", "xls": "#16a34a",
    "pptx": "#ea580c", "ppt": "#ea580c",
    "default": "#94a3b8",
}


def _icon_for(name: str) -> str:
    ext = os.path.splitext(name)[1].lstrip(".").lower() or "default"
    color = ICON_COLORS.get(ext, ICON_COLORS["default"])
    # Single-letter badge
    letter = (ext[:1].upper() if ext != "default" else "•")
    return color, letter


def build_tree_html(node: dict, depth: int = 0) -> str:
    """Recursively build the collapsible mind-map HTML tree (left panel)."""
    is_dir = node["type"] == "dir"
    name = html.escape(node["name"])
    if is_dir:
        children = node.get("children") or []
        child_html = "".join(build_tree_html(c, depth + 1) for c in children)
        truncated = node.get("truncated")
        badge = ""
        if truncated:
            badge = '<span class="trunc" title="已截断（超过扫描上限）">…</span>'
        return f"""
<li class="node dir" data-depth="{depth}">
  <div class="row" onclick="toggle(this)">
    <span class="twisty">▶</span>
    <span class="ic dir-ic">📁</span>
    <span class="label">{name}</span>{badge}
  </div>
  <ul class="children">{child_html}</ul>
</li>"""
    else:
        kind = node.get("kind")
        size = node.get("size") or 0
        color, letter = _icon_for(node["name"])
        kind_cls = {"text": "k-text", "large": "k-large", "binary": "k-binary",
                    "pdf": "k-pdf", "docx": "k-doc", "xlsx": "k-xls", "pptx": "k-ppt"}.get(kind, "k-binary")
        size_str = _human_size(size)
        # Encode the node identity for preview lookup.
        node_id = html.escape(_node_id(node))
        return f"""
<li class="node file {kind_cls}" data-depth="{depth}" data-id="{node_id}">
  <div class="row" onclick="preview(this)">
    <span class="twisty spacer"></span>
    <span class="ic file-ic" style="background:{color}">{letter}</span>
    <span class="label">{name}</span>
    <span class="sz">{size_str}</span>
  </div>
</li>"""


_NODE_COUNTER = {"n": 0}


def _assign_ids(node: dict, parent: str = "") -> str:
    """Assign stable ids to file nodes for preview lookup."""
    _NODE_COUNTER["n"] += 1
    nid = f"{parent}/{node['name']}"
    node["_id"] = nid
    if node["type"] == "dir" and node.get("children"):
        for c in node["children"]:
            _assign_ids(c, nid)
    return nid


def _node_id(node: dict) -> str:
    return node.get("_id", node["name"])


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n/1024**( {'B':0,'KB':1,'MB':2,'GB':3}[unit] ):.1f}{unit}"
        n /= 1024
    return f"{n}B"


_FILE_PAYLOADS: dict = {}


def render_html(scan: ScanResult, cfg: Config, root_path: str) -> str:
    """Create the full self-contained HTML document."""
    _NODE_COUNTER["n"] = 0
    _assign_ids(scan.tree)
    tree_html = build_tree_html(scan.tree, 0)

    # Pre-embed preview payloads for text/large/office/pdf files (HOW Q2:
    # embedded, no server). To keep the HTML bounded for huge directories
    # (CAP-4), embedding is capped by a TOTAL BYTES BUDGET (max_embed_bytes)
    # plus a hard file count ceiling (max_embed_files). Files beyond the
    # budget get a friendly "preview disabled" notice instead of embedded
    # content — this prevents the 5000 x 512KB = 2.5GB OOM. PDF base64 counts
    # ~1.33x its raw size toward the same budget, so a few large PDFs will
    # saturate it faster (by design).
    _FILE_PAYLOADS.clear()
    embedded_count = 0
    embedded_bytes = 0

    def collect(node: dict):
        nonlocal embedded_count, embedded_bytes
        if node["type"] == "file":
            kind = node.get("kind")
            if kind in ("text", "large", "pdf", "docx", "xlsx", "pptx"):
                if embedded_count < cfg.max_embed_files and embedded_bytes < cfg.max_embed_bytes:
                    p = Path(root_path) / _strip_root(node["_id"], scan.tree["name"])
                    payload = extract_preview(p, cfg)
                    _FILE_PAYLOADS[node["_id"]] = payload
                    embedded_count += 1
                    if payload.get("content"):
                        embedded_bytes += len(payload["content"].encode("utf-8", "replace"))
                else:
                    # Friendly notice; content not embedded to bound HTML size.
                    _FILE_PAYLOADS[node["_id"]] = {
                        "ok": False, "kind": "toomany", "content": None,
                        "reason": "该目录文件过多/体积过大，为控制视图体积已禁用内嵌预览；请用编辑器打开文件。",
                        "size": node.get("size") or 0, "truncated": False,
                    }
        elif node.get("children"):
            for c in node["children"]:
                collect(c)

    collect(scan.tree)
    # SECURITY (party-review finding #1): escape '<' and '>' in the embedded JSON so a
    # file literally containing "</script>" cannot prematurely close the <script> block
    # and white-screen the whole view. \u003c / \u003e are valid JSON escapes that decode
    # back to < > in JS but are inert to the HTML parser.
    payloads_json = json.dumps(_FILE_PAYLOADS, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")

    stats = {
        "files": scan.total_files,
        "dirs": scan.total_dirs,
        "truncated_files": scan.truncated_by_files,
        "truncated_depth": scan.truncated_by_depth,
        "symlink_loops": scan.skipped_symlink_loops,
        "max_files": cfg.max_files,
        "max_depth": cfg.max_depth,
        "max_embed": cfg.max_embed_files,
        "embedded": embedded_count,
        "embedded_bytes": embedded_bytes,
        "max_embed_bytes": cfg.max_embed_bytes,
    }

    # The template was originally authored for str.format(), so every CSS/JS
    # brace is doubled ({{ }}) as a format() escape. Rendering now uses
    # __KEY__ placeholders, so those doubled braces must be collapsed back to
    # single braces — otherwise the browser sees ${{...}} / {{STATS.x}} as
    # literal text, the <script> throws "Unexpected token '{'", and every JS
    # function (toggle / preview / ...) ends up undefined.
    #
    # CRITICAL: this brace fix is applied to the RAW template BEFORE the
    # __KEY__ substitutions. Doing it AFTER would run a global {{ → { over the
    # already-inserted PAYLOADS JSON / tree HTML and corrupt any file preview
    # whose content legitimately contains "{{" (Python f-strings, Jinja/
    # Mustache templates, C++ brace init, …). Fixing the template first leaves
    # all dynamic content untouched.
    #
    # Order matters:
    #   1. ${{           → ${            (JS interpolation that kept its $)
    #   2. {{STATS       → ${STATS       (JS interpolation that lost its $)
    #   3. {{formatSize  → ${formatSize  (same — lost its $)
    #   4. {{ → { , }} → }               (all remaining CSS/JS braces)
    # Steps 2-3 must precede 4, otherwise the generic {{ → { would turn them
    # into plain {...} (literal text, not interpolation). "${" is never "{{",
    # so step 4 never damages the ${...} produced by steps 1-3.
    template = (_HTML_TEMPLATE
                .replace("${{", "${")
                .replace("{{STATS", "${STATS")
                .replace("{{formatSize", "${formatSize")
                .replace("{{", "{")
                .replace("}}", "}"))
    # __KEY__ placeholders (not .format()) avoid clashes with the literal
    # braces inside the embedded JSON and CSS.
    return (template
            .replace("__TITLE__", html.escape(scan.tree["name"]))
            .replace("__ROOT_PATH__", html.escape(root_path))
            .replace("__ACCENT__", cfg.accent)
            .replace("__TREE__", tree_html)
            .replace("__PAYLOADS__", payloads_json)
            .replace("__STATS__", json.dumps(stats, ensure_ascii=False))
            .replace("__MAX_PREVIEW__", str(cfg.max_preview_bytes)))


def _strip_root(node_id: str, root_name: str) -> str:
    # node_id looks like /root_name/...strip leading /root_name
    prefix = "/" + root_name
    if node_id.startswith(prefix):
        return node_id[len(prefix):].lstrip("/")
    return node_id.lstrip("/")


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SeeTheFiles — __TITLE__</title>
<style>
:root {{
  --accent: __ACCENT__;
  --bg: #0f1220;
  --panel: #171b2e;
  --panel-2: #1e2440;
  --text: #e6e9f5;
  --muted: #9aa3c4;
  --line: #2c3358;
  --row-hover: #232a4d;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin:0; height:100%; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; background: var(--bg); color: var(--text); }}
.app {{ display:grid; grid-template-columns: var(--tree-w, 38%) 7px 1fr; grid-template-rows: auto 1fr; height:100vh; }}
.splitter {{ grid-column:2; grid-row:2; cursor:col-resize; background:var(--line); position:relative; transition: background .12s ease; }}
.splitter:hover, .splitter.dragging {{ background: var(--accent); }}
.splitter::after {{ content:''; position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); width:2px; height:28px; border-radius:2px; background:#64748b; }}
.splitter:hover::after, .splitter.dragging::after {{ background:#fff; }}
header {{ grid-column: 1 / -1; display:flex; align-items:center; gap:12px; padding:12px 18px; background: linear-gradient(90deg, var(--panel), var(--panel-2)); border-bottom:1px solid var(--line); }}
header .logo {{ width:30px;height:30px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;font-weight:700;color:#fff; }}
header h1 {{ font-size:16px; margin:0; }}
header .path {{ font-size:12px; color:var(--muted); margin-left:auto; max-width:55%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
/* Tree panel */
.tree-panel {{ overflow:auto; padding:14px 8px 40px 18px; border-right:1px solid var(--line); background:var(--panel); min-width:0; }}
.tree-panel ul {{ list-style:none; margin:0; padding-left:18px; position:relative; }}
.tree-panel > ul {{ padding-left:4px; }}
.tree-panel ul ul {{ border-left:1px dashed var(--line); }}
.node {{ margin:2px 0; }}
.row {{ display:flex; align-items:center; gap:6px; padding:4px 8px; border-radius:7px; cursor:pointer; transition: background .12s ease, transform .08s ease; user-select:none; }}
.row:hover {{ background: var(--row-hover); }}
.twisty {{ width:14px; text-align:center; color:var(--muted); transition: transform .15s ease; font-size:10px; }}
.dir.collapsed > .children {{ display:none; }}
.dir.collapsed > .row .twisty {{ transform: rotate(0deg); }}
.dir:not(.collapsed) > .row .twisty {{ transform: rotate(90deg); }}
.twisty.spacer {{ visibility:hidden; }}
.ic {{ width:20px; height:20px; border-radius:5px; display:flex; align-items:center; justify-content:center; font-size:11px; color:#fff; flex:0 0 auto; }}
.dir-ic {{ background:#f5b14c; }}
.file-ic {{ font-weight:700; }}
.label {{ font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.sz {{ margin-left:auto; font-size:11px; color:var(--muted); padding-left:8px; }}
.trunc {{ color:#f59e0b; margin-left:4px; }}
.k-large .ic {{ outline:1px solid #f59e0b; }}
.k-binary .ic {{ outline:1px solid #64748b; }}
.k-pdf .ic {{ outline:1px solid #ef4444; }}
.k-doc .ic {{ outline:1px solid #2563eb; }}
.k-xls .ic {{ outline:1px solid #16a34a; }}
.k-ppt .ic {{ outline:1px solid #ea580c; }}
.pdf-wrap {{ display:flex; flex-direction:column; height:100%; }}
.pdf-wrap embed {{ flex:1; min-height:62vh; border:0; border-radius:8px; background:#525659; }}
.pdf-fallback {{ padding:10px 18px; font-size:12px; color:var(--muted); text-align:center; }}
.pdf-fallback button {{ margin-left:6px; background:var(--accent); color:#fff; border:0; border-radius:6px; padding:3px 10px; cursor:pointer; font-size:12px; }}
.pdf-fallback button:hover {{ filter:brightness(1.1); }}
.node.file.active > .row {{ background: var(--accent); color:#fff; }}
.node.file.active > .row .sz {{ color:#e0e7ff; }}
/* Preview panel */
.preview-panel {{ display:flex; flex-direction:column; background: var(--bg); overflow:hidden; min-width:0; }}
.preview-head {{ padding:12px 18px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:10px; background:var(--panel-2); }}
.preview-head .fname {{ font-size:14px; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.preview-head .ftag {{ font-size:11px; padding:2px 8px; border-radius:999px; background:var(--line); color:var(--muted); }}
.preview-body {{ flex:1; overflow:auto; padding:0; }}
.preview-body pre {{ margin:0; padding:18px; font-family:"JetBrains Mono", "Cascadia Code", Consolas, "Courier New", monospace; font-size:13px; line-height:1.55; white-space:pre-wrap; word-break:break-word; overflow-wrap:anywhere; tab-size:4; }}
.placeholder {{ display:flex; flex-direction:column; align-items:center; justify-content:center; height:100%; color:var(--muted); gap:10px; text-align:center; padding:30px; }}
.placeholder .big {{ font-size:46px; opacity:.5; }}
.notice {{ margin:18px; padding:16px; border-radius:12px; background:var(--panel); border:1px solid var(--line); }}
.notice.warn {{ border-color:#f59e0b; }}
.notice.binary {{ border-color:#64748b; }}
.notice h3 {{ margin:0 0 8px; font-size:15px; }}
.notice p {{ margin:4px 0; color:var(--muted); font-size:13px; }}
.stats {{ font-size:11px; color:var(--muted); padding:6px 18px; border-top:1px solid var(--line); background:var(--panel); display:flex; gap:14px; flex-wrap:wrap; }}
.stats .warn {{ color:#f59e0b; }}
@keyframes fadein {{ from {{ opacity:0; transform: translateY(4px);}} to {{opacity:1; transform:none;}} }}
.preview-body.preview-anim pre, .preview-body.preview-anim .notice {{ animation: fadein .18s ease; }}
/* Search */
.search {{ padding:8px 18px; border-bottom:1px solid var(--line); background:var(--panel); }}
.search input {{ width:100%; padding:7px 11px; border-radius:8px; border:1px solid var(--line); background:var(--bg); color:var(--text); font-size:13px; outline:none; }}
.search input:focus {{ border-color: var(--accent); }}
mark {{ background:#fde68a; color:#1f2937; border-radius:3px; padding:0 2px; }}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="logo">S</div>
    <h1>SeeTheFiles</h1>
    <span class="path">__ROOT_PATH__</span>
  </header>
  <div class="tree-panel">
    <div class="search"><input id="search" placeholder="过滤节点… (支持名称子串)" oninput="filterTree(this.value)"></div>
    <ul>__TREE__</ul>
  </div>
  <div class="splitter" id="splitter" title="拖动调整宽度"></div>
  <div class="preview-panel">
    <div class="preview-head">
      <span class="fname" id="pv-name">未选择文件</span>
      <span class="ftag" id="pv-tag"></span>
    </div>
    <div class="preview-body" id="pv-body">
      <div class="placeholder"><div class="big">🗂️</div><div>点击左侧文件节点预览内容<br>目录节点可点击折叠 / 展开</div></div>
    </div>
    <div class="stats" id="stats"></div>
  </div>
</div>
<script>
const PAYLOADS = __PAYLOADS__;
const STATS = __STATS__;
function renderStats(){{
  const el = document.getElementById('stats');
  let html = `<span>📄 文件 {{STATS.files}}</span><span>📁 目录 {{STATS.dirs}}</span>`;
  html += `<span>📦 内嵌预览 {{STATS.embedded}} 个 · {{formatSize(STATS.embedded_bytes)}} / {{formatSize(STATS.max_embed_bytes)}}</span>`;
  if (STATS.truncated_files) html += `<span class="warn">⚠ 已达到文件上限 {{STATS.max_files}}，已截断</span>`;
  if (STATS.truncated_depth) html += `<span class="warn">⚠ 已达深度上限 {{STATS.max_depth}}，已截断</span>`;
  if (STATS.symlink_loops) html += `<span class="warn">🔗 跳过符号链接环 {{STATS.symlink_loops}} 处</span>`;
  if (STATS.embedded < STATS.files) html += `<span class="warn">📦 内嵌预览 {{STATS.embedded}}/${STATS.files}（其余已禁用以控体积）</span>`;
  el.innerHTML = html;
}}
renderStats();

function toggle(row){{
  const li = row.closest('li.node.dir');
  li.classList.toggle('collapsed');
}}
function clearActive(){{ document.querySelectorAll('.node.file.active').forEach(n=>n.classList.remove('active')); }}
function escapeHtml(s){{ return s.replace(/[&<>"']/g, c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}

function preview(row){{
  const li = row.closest('li.node.file');
  const id = li.getAttribute('data-id');
  clearActive(); li.classList.add('active');
  const name = li.querySelector('.label').textContent;
  const body = document.getElementById('pv-body');
  const tag = document.getElementById('pv-tag');
  document.getElementById('pv-name').textContent = name;
  const p = PAYLOADS[id];
  if (!p) {{
    tag.textContent = '未知';
    body.innerHTML = `<div class="notice"><h3>无法预览</h3><p>未找到该文件的预览信息。</p></div>`;
    return;
  }}
  if (!p.ok) {{
    if (p.kind === 'toomany') {{
      tag.textContent = '预览已禁用';
      body.innerHTML = `<div class="notice warn"><h3>📦 预览已禁用</h3><p>${{escapeHtml(p.reason||'')}}</p><p>大小：{{formatSize(p.size)}}</p></div>`;
      return;
    }}
    if (p.kind === 'pdf-large') {{
      tag.textContent = 'PDF(过大)';
      body.innerHTML = `<div class="notice warn"><h3>📄 PDF 过大</h3><p>${{escapeHtml(p.reason||'')}}</p><p>大小：{{formatSize(p.size)}}</p></div>`;
      return;
    }}
    tag.textContent = p.kind === 'binary' ? '二进制' : '错误';
    body.innerHTML = `<div class="notice ${{p.kind==='binary'?'binary':'warn'}}"><h3>${{p.kind==='binary'?'🚫 二进制文件':'⚠ 无法预览'}}</h3><p>${{escapeHtml(p.reason||'')}}</p><p>大小：{{formatSize(p.size)}}</p></div>`;
    return;
  }}
  // PDF: render inline via a data: URI so the browser's built-in PDF viewer
  // shows it. data: is NOT a file: URL, so this avoids the "file: URLs are
  // unique security origins" cross-origin block. src is set via the DOM
  // property (not an attribute string) so the large base64 never lands in
  // an HTML attribute. A "open in new tab" button uses a Blob URL as a
  // fallback when inline embedding is blocked by the browser.
  if (p.kind === 'pdf') {{
    tag.textContent = 'PDF';
    body.innerHTML = '<div class="pdf-wrap"><embed id="pdf-emb" type="application/pdf"></div><div class="pdf-fallback">若上方未显示 PDF，可<button id="pdf-open-btn" type="button">在新标签页打开</button></div>';
    document.getElementById('pdf-emb').src = 'data:application/pdf;base64,' + p.content;
    document.getElementById('pdf-open-btn').onclick = function(){{ openPdfBlob(p.content); }};
    body.classList.remove('preview-anim'); void body.offsetWidth; body.classList.add('preview-anim');
    return;
  }}
  // Office OOXML: text extracted server-side, shown like a text preview.
  if (p.kind === 'docx' || p.kind === 'xlsx' || p.kind === 'pptx') {{
    const labels = {{docx:'Word', xlsx:'Excel', pptx:'PowerPoint'}};
    tag.textContent = labels[p.kind] + '(文本)';
    let content = escapeHtml(p.content);
    if (p.truncated) content += '\\n\\n… (已截断，仅显示前 ' + formatSize(PAYLOADS_MAX) + '，完整内容请用 Office 打开)';
    body.innerHTML = '<pre>' + content + '</pre>';
    body.classList.remove('preview-anim'); void body.offsetWidth; body.classList.add('preview-anim');
    return;
  }}
  tag.textContent = p.kind === 'large' ? '大文件(截断)' : '文本';
  let content = escapeHtml(p.content);
  if (p.truncated) {{
    content += '\\n\\n… (已截断，仅显示前 ' + formatSize(PAYLOADS_MAX) + '，完整内容请用编辑器打开)';
  }}
  body.innerHTML = '<pre>' + content + '</pre>';
  body.classList.remove('preview-anim'); void body.offsetWidth; body.classList.add('preview-anim');
}}
const PAYLOADS_MAX = __MAX_PREVIEW__;
function formatSize(n){{
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  return (n/1024/1024).toFixed(1) + ' MB';
}}

// Fallback for PDF preview: decode the base64 to a Blob and open it in a new
// tab via a Blob URL. Blob URLs are same-origin to the file:// page, so this
// works even where inline <embed> is blocked. Used when the user clicks the
// "在新标签页打开" button under an inline PDF that failed to render.
function openPdfBlob(b64){{
  try {{
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i=0;i<bin.length;i++) bytes[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], {{type:'application/pdf'}}));
    window.open(url);
  }} catch(e){{ alert('无法打开 PDF: ' + (e&&e.message||e)); }}
}}

function filterTree(q){{
  q = q.trim().toLowerCase();
  const lis = document.querySelectorAll('.tree-panel li.node');
  if (!q) {{ lis.forEach(li=>li.style.display=''); document.querySelectorAll('.node.dir').forEach(li=>li.classList.remove('collapsed')); return; }}
  lis.forEach(li=>{{
    const label = li.querySelector('.label');
    const match = label && label.textContent.toLowerCase().includes(q);
    li.style.display = match ? '' : 'none';
    if (match && li.classList.contains('dir')) li.classList.remove('collapsed');
  }});
}}
document.addEventListener('keydown', e=>{{ if(e.key==='/' && document.activeElement!==document.getElementById('search')){{ e.preventDefault(); document.getElementById('search').focus(); }} }});

// Resizable splitter between the tree and preview panels. The tree column
// width is a CSS var (--tree-w) on .app; dragging the splitter updates it
// live and persists to localStorage so the chosen split survives reloads.
// Uses Pointer Events (mouse + touch); setPointerCapture keeps receiving
// moves even when the cursor leaves the 7px bar.
(function(){{
  const splitter = document.getElementById('splitter');
  const root = document.querySelector('.app');
  if (!splitter || !root) return;
  const KEY = 'seethefiles.treeW';
  const saved = parseFloat(localStorage.getItem(KEY));
  if (saved && saved > 0) root.style.setProperty('--tree-w', saved + '%');
  let dragging = false;
  splitter.addEventListener('pointerdown', (e) => {{
    dragging = true; splitter.classList.add('dragging');
    document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none';
    try {{ splitter.setPointerCapture(e.pointerId); }} catch(_) {{}}
    e.preventDefault();
  }});
  document.addEventListener('pointermove', (e) => {{
    if (!dragging) return;
    // Tree starts at x=0, so clientX == desired tree width in px.
    const pct = e.clientX / window.innerWidth * 100;
    const clamped = Math.max(15, Math.min(80, pct));
    root.style.setProperty('--tree-w', clamped + '%');
  }});
  const stop = () => {{
    if (!dragging) return;
    dragging = false; splitter.classList.remove('dragging');
    document.body.style.cursor = ''; document.body.style.userSelect = '';
    const cur = root.style.getPropertyValue('--tree-w');
    if (cur) localStorage.setItem(KEY, cur);
  }};
  document.addEventListener('pointerup', stop);
  document.addEventListener('pointercancel', stop);
}})();
</script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# CLI entry (CAP-1: right-click input + repeat-trigger race safety)
# --------------------------------------------------------------------------- #

def _prepare_cache() -> Path:
    """Return a dedicated cache dir for generated views and prune stale files.

    We no longer dump every run's HTML straight into the OS temp root and forget it
    (that leaked a file per right-click, forever). Instead we use one subdir and, on
    each launch, delete files older than 24h — never the freshly written one, so the
    browser (which loads asynchronously after this process may exit) is never starved.
    """
    cache = Path(tempfile.gettempdir()) / "see_the_files_cache"
    try:
        cache.mkdir(exist_ok=True)
    except OSError:
        return cache
    now = __import__("time").time()
    for f in cache.glob("see_the_files_*.html"):
        try:
            if now - f.stat().st_mtime > 24 * 3600:
                f.unlink()
        except OSError:
            pass
    return cache


def _open_in_browser(path: Path) -> None:
    """Open the generated HTML in the default browser (used as the 'popup')."""
    url = path.as_uri() if hasattr(path, "as_uri") else ("file://" + url2pathname(str(path)))
    try:
        webbrowser.open(url)
    except Exception:
        sys.stderr.write(f"[info] 无法自动打开浏览器，请手动打开: {path}\n")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="see_the_files",
        description="SeeTheFiles — 将任意文件夹渲染为美观的思维导图式结构视图 (CLI + 右键菜单)。",
    )
    parser.add_argument("path", nargs="?", help="目标文件夹路径（右键菜单的 %1）")
    parser.add_argument("--config", help="指定 config.toml 路径")
    parser.add_argument("--max-depth", type=int, help="覆盖最大扫描深度")
    parser.add_argument("--max-files", type=int, help="覆盖最大文件数量上限")
    parser.add_argument("--max-preview", type=int, help="覆盖单文件预览字节上限")
    parser.add_argument("--ignore", action="append", default=[], help="追加忽略的目录/文件名（可多次）")
    parser.add_argument("--no-open", action="store_true", help="只生成 HTML 不打开浏览器")
    parser.add_argument("--output", help="指定输出 HTML 路径（默认写入系统临时目录唯一文件）")
    parser.add_argument("--print-json", action="store_true", help="仅打印目录树 JSON 到 stdout（调试）")
    args = parser.parse_args(argv)

    # On Windows --windowed builds (Run.exe launched from Explorer) there is no
    # console, so Python sets sys.stdout/sys.stderr to None. Any code that does
    # `print(...)` or `sys.stderr.write(...)` would then raise
    # "AttributeError: 'NoneType' object has no attribute 'write'". Redirect both
    # streams to a log file next to the exe so status messages are preserved and
    # harmless instead of crashing the whole run.
    if sys.stdout is None or sys.stderr is None:
        try:
            _log_dir = Path(sys.argv[0]).resolve().parent
            _log_path = _log_dir / "SeeTheFiles.log"
            _fh = open(_log_path, "a", encoding="utf-8")
            if sys.stdout is None:
                sys.stdout = _fh
            if sys.stderr is None:
                sys.stderr = _fh
        except OSError:
            # Last-resort: swallow so we never crash on logging setup.
            class _Null:
                def write(self, *a, **k):
                    return 0
                def flush(self, *a, **k):
                    return None
            if sys.stdout is None:
                sys.stdout = _Null()
            if sys.stderr is None:
                sys.stderr = _Null()

    target = args.path
    if not target:
        parser.print_help()
        return 1
    root = Path(target)
    if not root.exists():
        sys.stderr.write(f"[error] 路径不存在: {root}\n")
        return 2
    if not root.is_dir():
        sys.stderr.write(f"[error] 不是文件夹: {root}\n")
        return 2

    cfg = load_config(args.config)
    if args.max_depth is not None:
        cfg.max_depth = args.max_depth
    if args.max_files is not None:
        cfg.max_files = args.max_files
    if args.max_preview is not None:
        cfg.max_preview_bytes = args.max_preview
    for extra in args.ignore:
        cfg.ignore_names.add(extra)

    try:
        scan = scan_directory(root, cfg)
    except Exception as exc:  # pragma: no cover - defensive
        sys.stderr.write(f"[error] 扫描失败: {exc}\n")
        return 3

    if args.print_json:
        print(json.dumps(scan.tree, ensure_ascii=False, indent=2))
        return 0

    html_doc = render_html(scan, cfg, str(root.resolve()))

    # CAP-1 race safety: each invocation writes a UNIQUE temp file (pid + counter +
    # timestamp) so repeated triggers on the same folder never overwrite each other
    # and never collide. Files live in a dedicated cache dir that is pruned on launch
    # (see _prepare_cache) so right-clicking many times does not leak temp files.
    if args.output:
        out_path = Path(args.output).resolve()
    else:
        cache = _prepare_cache()
        unique = f"see_the_files_{root.resolve().name}_{os.getpid()}_{_INSTANCE_COUNTER}_{int(__import__('time').time()*1000)}.html"
        out_path = cache / unique
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_doc, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"[error] 无法写入输出文件 {out_path}: {exc}\n")
        return 4

    sys.stderr.write(
        f"[ok] 文件 {scan.total_files} 个 / 目录 {scan.total_dirs} 个"
        + (f" | ⚠ 已达文件上限截断" if scan.truncated_by_files else "")
        + (f" | ⚠ 已达深度上限截断" if scan.truncated_by_depth else "")
        + (f" | 🔗 跳过 symlink 环 {scan.skipped_symlink_loops} 处" if scan.skipped_symlink_loops else "")
        + f"\n[ok] 视图: {out_path}\n"
    )
    if not args.no_open:
        _open_in_browser(out_path)
    return 0


_INSTANCE_COUNTER = 0


def _bump_instance() -> int:
    global _INSTANCE_COUNTER
    _INSTANCE_COUNTER += 1
    return _INSTANCE_COUNTER


if __name__ == "__main__":
    _bump_instance()
    sys.exit(main())
