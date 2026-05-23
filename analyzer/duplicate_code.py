# backend/saas_analyzer/analyzer/duplicate_code.py
from __future__ import annotations

import os
import re
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple, Iterable, Set
from datetime import datetime, timezone
from pathlib import Path

CloneScope = Literal["block", "function", "class", "file"]
CloneMatch = Literal["exact_normalized", "exact_raw", "near_miss", "structural"]

# ----------------------- dataclasses (public) -----------------------
@dataclass
class CloneMember:
    file_id: str
    start_line: int
    end_line: int


@dataclass
class CloneGroup:
    id: str
    # Scope: what is duplicated (for this detector: always "block")
    kind: CloneScope
    # Match mode: how similarity was judged
    match: CloneMatch
    token_len: int
    score: float
    members: List[CloneMember] = field(default_factory=list)
    norm: str = ""           # e.g. "idents,space"
    window_lines: int = 0    # actual number of normalized lines in the window
    signature: str = ""      # raw signature or hash used
    preview: str = ""        # first line of canonical snippet
    category: str = ""       # "header", "body", "block", etc.
    files: int = 0           # distinct files participating in this group
    severity: int = 0        # heuristic: lines * (files-1)


@dataclass
class DuplicateCodeReport:
    version: str
    generated_at: str
    groups: List[CloneGroup] = field(default_factory=list)
    files_with_clones: int = 0
    summary: Dict[str, Any] = field(default_factory=dict)


# ----------------------- diagnostics helpers -----------------------
def _p(*parts) -> None:
    try:
        print("[DUP]", *parts)
    except Exception:
        pass


def _cfgval(cfg: Any, name: str, default: Any) -> Any:
    try:
        return getattr(cfg, name, default)
    except Exception:
        return default


def _abs_path(path: str, scan_root: Optional[str | Path]) -> str:
    if not path:
        return path
    p = Path(path)
    if p.is_absolute():
        return p.as_posix()
    if scan_root:
        return (Path(scan_root) / p).as_posix()
    return p.as_posix()


# ----------------------- normalization -----------------------
_COMMENT_RE = re.compile(r"#.*$")
_IDENT_RE   = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


def _normalize_line(s: str, *, strip_comments: bool = True, normalize_idents: bool = False) -> str:
    if strip_comments:
        s = _COMMENT_RE.sub("", s)
    s = s.rstrip()
    s = " ".join(s.split())
    if not s:
        return ""
    if normalize_idents:
        def repl(m: re.Match[str]) -> str:
            tok = m.group(1)
            if tok in {
                "def", "class", "return", "if", "elif", "else", "for", "while", "try", "except", "with", "as",
                "from", "import", "pass", "break", "continue", "yield", "lambda", "in", "is", "and", "or", "not",
                "True", "False", "None"
            }:
                return tok
            if tok.isdigit():
                return tok
            return "ID"
        s = _IDENT_RE.sub(repl, s)
    return s


def _preprocess_file(apath: str, *, normalize_idents: bool) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    try:
        with open(apath, "r", encoding="utf-8", errors="ignore") as f:
            for i, raw in enumerate(f, start=1):
                nl = _normalize_line(raw, strip_comments=True, normalize_idents=normalize_idents)
                if nl:
                    out.append((i, nl))
    except Exception:
        pass
    return out


def _sliding_windows(lines: List[Tuple[int, str]], k: int) -> Iterable[Tuple[int, Tuple[str, ...]]]:
    if k <= 0 or len(lines) < k:
        return
    text_only = [t for _, t in lines]
    for i in range(0, len(text_only) - k + 1):
        start_line = lines[i][0]
        yield start_line, tuple(text_only[i:i + k])


# ----------------------- exact window grouper (with stats) -----------------------
@dataclass
class GrouperStats:
    total_windows: int = 0
    multi_hit_windows: int = 0
    cross_file_windows: int = 0
    kept_groups: int = 0


def _grouper_exact(
    files: List[Tuple[str, str, List[Tuple[int, str]]]],
    *,
    min_lines: int,
    allow_within_file: bool,
    max_groups: int,
) -> Tuple[List[Tuple[Tuple[str, ...], List[Tuple[str, int, int]]]], GrouperStats]:
    """
    Returns (groups, stats), where groups are:
      [ (key_tuple_lines, [ (file_id, start, end), ... ]), ... ]
    """
    stats = GrouperStats()
    index: Dict[Tuple[str, ...], List[Tuple[str, int, int]]] = {}

    for file_id, _ap, norm_lines in files:
        for start, win in _sliding_windows(norm_lines, min_lines):
            end = start + (min_lines - 1)
            stats.total_windows += 1
            index.setdefault(win, []).append((file_id, start, end))

    groups: List[Tuple[Tuple[str, ...], List[Tuple[str, int, int]]]] = []
    for key, members in index.items():
        if len(members) >= 2:
            stats.multi_hit_windows += 1
            files_in = {m[0] for m in members}
            if len(files_in) >= 2:
                stats.cross_file_windows += 1
            if allow_within_file or len(files_in) >= 2:
                groups.append((key, members))

    groups.sort(key=lambda g: (-len(g[1]), -len(g[0])))
    groups = groups[:max_groups]
    stats.kept_groups = len(groups)
    return groups, stats


# ----------------------- snippet classification helpers -----------------------
def _classify_snippet(lines: Tuple[str, ...]) -> str:
    """Crude category: 'header' (imports-only), 'body' (def/class), else 'block'."""
    if not lines:
        return "block"
    non_empty = [ln.strip() for ln in lines if ln.strip()]
    if not non_empty:
        return "block"
    first = non_empty[0]
    if all(ln.startswith("import ") or ln.startswith("from ") for ln in non_empty):
        return "header"
    if first.startswith("def ") or first.startswith("class "):
        return "body"
    return "block"


def _severity_for_group(num_lines: int, file_ids: Set[str]) -> int:
    """Simple heuristic: more lines and more files -> higher priority."""
    f = max(0, len(file_ids) - 1)
    return int(max(0, num_lines) * f)


# ----------------------- public API -----------------------
def analyze_duplicate_code(
    *,
    graph: Dict[str, Any],
    seeds: Optional[List[str]] = None,
    extra_roots: Optional[List[str]] = None,
    cfg: Optional[Dict[str, Any] | Any] = None,
    scan_root: Optional[str | Path] = None,
    parsed_universe: Optional[List[str] | set[str]] = None,
) -> DuplicateCodeReport:
    """
    MVP duplicate detector with diagnostics + relaxed fallback:
      • Exact matches of N consecutive normalized lines across files
      • If strict pass finds nothing, retry with dup_min_lines=3 and allow_within_file=True
    """
    nodes = (graph.get("nodes") or {})
    edges = (graph.get("edges") or [])
    _ = (seeds, extra_roots)

    _p("BEGIN --- Duplicate Code Analysis")
    _p("graph:", f"nodes={len(nodes)}", f"edges={len(edges)}")

    # Params
    min_tokens        = _cfgval(cfg, "dup_min_tokens", 25)               # informational
    normalize_idents  = _cfgval(cfg, "dup_normalize_idents", True)
    allow_within_file = _cfgval(cfg, "dup_allow_within_file", False)
    min_span_tokens   = _cfgval(cfg, "dup_min_span_tokens", 25)          # informational
    max_groups        = int(_cfgval(cfg, "dup_max_groups", 200))
    min_lines         = int(_cfgval(cfg, "dup_min_lines", 5))            # strict pass window

    _p(
        "seeds=", len(seeds or []),
        "extra_roots=", len(extra_roots or []),
        "cfg_type=", type(cfg).__name__ if cfg is not None else "NoneType",
    )
    _p(
        "params:",
        f"min_tokens={min_tokens}",
        f"normalize_idents={normalize_idents}",
        f"allow_within_file={allow_within_file}",
        f"min_span_tokens={min_span_tokens}",
        f"max_groups={max_groups}",
        f"min_lines={min_lines}",
    )
    _p("scan_root=", scan_root if scan_root else "<none>")

    # Universe filter
    uni: Optional[Set[str]] = set(parsed_universe) if parsed_universe else None

    # Collect & preprocess
    prepped: List[Tuple[str, str, List[Tuple[int, str]]]] = []
    missing = 0
    preview_paths: List[str] = []
    for mid, meta in nodes.items():
        if uni and mid not in uni:
            continue

        # Prefer explicit path from node meta; fall back to node id
        rel = str((meta or {}).get("path") or "") or str(mid)
        ap = _abs_path(rel, scan_root)
        preview_paths.append(rel or "")

        if ap and os.path.exists(ap):
            norm = _preprocess_file(ap, normalize_idents=normalize_idents)
            prepped.append((mid, ap, norm))
        else:
            print("[DUP:SCAN]", "skip-missing", mid, "path=", rel)
            missing += 1

    try:
        _p("files preview (first 8):", [p for p in preview_paths[:8]])
    except Exception:
        pass

    # -------- Strict pass --------
    raw_groups, stats = _grouper_exact(
        prepped,
        min_lines=max(2, min_lines),
        allow_within_file=allow_within_file,
        max_groups=max_groups,
    )
    _p(
        "strict: total_windows=", stats.total_windows,
        "multi_hit=", stats.multi_hit_windows,
        "cross_file=", stats.cross_file_windows,
        "kept_groups=", stats.kept_groups,
    )

    groups: List[CloneGroup] = []
    file_hit_set: Set[str] = set()

    norm_desc = "strip_comments,idents" if normalize_idents else "strip_comments"

    def _emit_groups(_raw, *, match: CloneMatch = "exact_normalized") -> None:
        idx0 = len(groups)
        for i, (key_lines, members) in enumerate(_raw, start=1):
            gid = f"G{idx0 + i:04d}"
            # key_lines are normalized lines in the window
            num_lines = len(key_lines)
            token_len = num_lines  # here "tokens" == # normalized lines
            mem_objs: List[CloneMember] = []
            file_ids: Set[str] = set()

            for file_id, start, end in members:
                mem_objs.append(CloneMember(file_id=file_id, start_line=start, end_line=end))
                file_ids.add(file_id)
                file_hit_set.add(file_id)

            category = _classify_snippet(key_lines)
            severity = _severity_for_group(num_lines, file_ids)
            try:
                sig_src = "\n".join(key_lines)
                signature = hashlib.sha1(sig_src.encode("utf-8", errors="ignore")).hexdigest()
            except Exception:
                signature = ""

            preview = key_lines[0].strip() if key_lines else ""

            groups.append(CloneGroup(
                id=gid,
                kind="block",                          # scope: contiguous block of lines
                match=match or "exact_normalized",     # detection mode
                token_len=token_len,
                score=1.0,
                members=mem_objs,
                norm=norm_desc,
                window_lines=num_lines,
                signature=signature,
                preview=preview,
                category=category,
                files=len(file_ids),
                severity=severity,
            ))

    _emit_groups(raw_groups, match="exact_normalized")

    # -------- Relaxed fallback (only if strict produced nothing) --------
    fallback_used = False
    fb_stats = None
    if not groups:
        fallback_used = True
        fb_min_lines = max(2, min(3, min_lines))  # go down to 3 (or 2 if configured lower)
        _p("fallback: enabling allow_within_file=True and lowering min_lines to", fb_min_lines)
        raw_fb, fb_stats = _grouper_exact(
            prepped,
            min_lines=fb_min_lines,
            allow_within_file=True,
            max_groups=min(max_groups, 200),
        )
        _p(
            "fallback: total_windows=", fb_stats.total_windows,
            "multi_hit=", fb_stats.multi_hit_windows,
            "cross_file=", fb_stats.cross_file_windows,
            "kept_groups=", fb_stats.kept_groups,
        )
        _emit_groups(raw_fb, match="exact_normalized")

    files_scanned = sum(1 for _mid, _ap, norm in prepped if norm)
    files_with_clones = len(file_hit_set)

    _p("files_scanned:", files_scanned)
    _p("groups_found:", len(groups))

    # Final summary
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary: Dict[str, Any] = {
        "total_groups": len(groups),
        "largest_group": (max((len(g.members) for g in groups), default=0) or None),
        "files_considered": len(prepped),
        "files_missing": missing,
        "params": {
            "min_tokens": min_tokens,
            "normalize_idents": normalize_idents,
            "allow_within_file": allow_within_file,
            "min_span_tokens": min_span_tokens,
            "max_groups": max_groups,
            "min_lines": min_lines,
            "fallback_used": fallback_used,
            "fallback_min_lines": (fb_stats and (3 if min_lines >= 3 else 2)) if fallback_used else None,
        },
        "diagnostics": {
            "strict": {
                "total_windows": stats.total_windows,
                "multi_hit_windows": stats.multi_hit_windows,
                "cross_file_windows": stats.cross_file_windows,
                "kept_groups": stats.kept_groups,
            },
            "fallback": {
                "used": fallback_used,
                "total_windows": getattr(fb_stats, "total_windows", 0),
                "multi_hit_windows": getattr(fb_stats, "multi_hit_windows", 0),
                "cross_file_windows": getattr(fb_stats, "cross_file_windows", 0),
                "kept_groups": getattr(fb_stats, "kept_groups", 0),
            },
        },
    }

    _p("summary total_groups=", summary["total_groups"], "files_with_clones=", files_with_clones)
    _p("END --- Duplicate Code Analysis")

    return DuplicateCodeReport(
        version="1",
        generated_at=now,
        groups=groups,
        files_with_clones=files_with_clones,
        summary=summary,
    )
