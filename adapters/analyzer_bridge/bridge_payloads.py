from __future__ import annotations
from typing import Any, Dict, List, Optional, Sequence
import os
from pathlib import Path

from .internals import build_import_row_annotations
from adapters.canonical import (
    repo_rel,
    to_posix,
    canon_file_id,
    canon_module_id,
)
from adapters.analyzer_bridge.internals import (
    as_list,
    attr_or_key,
)


def _dedupe_preserve_order(seq: Sequence[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _to_name(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.startswith(("def ", "class ")):
            s = s.split(" ", 1)[1].strip() or s
        return s or None
    if isinstance(v, dict):
        name = v.get("name") or v.get("label") or v.get("id")
        return str(name) if name else None
    for attr in ("name", "label", "id"):
        try:
            name = getattr(v, attr, None)
            if isinstance(name, str) and name:
                return name
        except Exception:
            pass
    try:
        s = str(v)
        if s and s != "(none)":
            return s
    except Exception:
        pass
    return None


def _clean_and_normalize_import_rows(rows_like) -> List[str]:
    out: List[str] = []
    for r in as_list(rows_like):
        s = r if isinstance(r, str) else _to_name(r) or ""
        s = (s or "").strip()
        if not s:
            continue
        # strip trailing comments
        i = s.find("#")
        if i == 0:
            continue
        if i > 0:
            s = s[:i].rstrip()
        if not s:
            continue
        if s.startswith("from ") or s.startswith("import "):
            out.append(s)
        else:
            out.append(f"import {s}")
    return _dedupe_preserve_order(out)


def node_payload_from(node_id: str, n: Any, *, repo_root: Optional[Path | str]) -> Dict[str, Any]:
    """
    Authoritative payload builder used by analyzer.

    Assumptions under the current contracts:
      - `node_id` is a stable node identifier; for analyzer-backed graphs this
        is a **module id** relative to the scan root (e.g. 'downloader',
        'scrapy.core.engine', 'ui.app.run_gui').
      - The node record `n` may carry:
          • path/file: repo-relative or absolute path
          • module: dotted module id
    """
    root = to_posix(str(repo_root)) if repo_root else None

    # ---------- Canonical file-id (repo-rel POSIX) ----------
    # Prefer an explicit path/file on the node; fall back to id/module when needed.
    raw_path_like = (
        attr_or_key(n, "path")
        or attr_or_key(n, "file")
        or (str(node_id) if isinstance(node_id, str) else "")
    )

    canon_path = ""
    try:
        # canon_file_id knows how to handle both path-ish and dotted inputs;
        # we pass scan_root so it will keep things repo-relative when possible.
        canon_path = canon_file_id(raw_path_like, root, strict_suffix=False) or ""
    except Exception:
        canon_path = ""

    if not canon_path:
        # Retry from explicit module field if present
        raw_mod = attr_or_key(n, "module") or ""
        try:
            if raw_mod:
                canon_path = canon_file_id(raw_mod, root, strict_suffix=False) or ""
        except Exception:
            pass

    canon_path = to_posix(canon_path) if canon_path else ""

    # ---------- Canonical module (dotted) ----------
    # Prefer node_id itself when it behaves like a module id (no slashes, no .py).
    module = ""
    if isinstance(node_id, str):
        nid = node_id.strip()
        if nid and "/" not in nid and "\\" not in nid and not nid.endswith(".py"):
            module = nid

    if not module and canon_path:
        try:
            module = canon_module_id(canon_path, root) or ""
        except Exception:
            module = ""

    if not module:
        raw_mod = attr_or_key(n, "module") or ""
        try:
            if raw_mod:
                module = canon_module_id(raw_mod, root) or ""
        except Exception:
            # last resort: try making a module from the raw path-ish
            try:
                module = canon_module_id(raw_path_like, root) if raw_path_like else ""
            except Exception:
                module = ""

    # ---------- Header path for UI (repo-rel POSIX) ----------
    header_path = canon_path or repo_rel(
        attr_or_key(n, "path") or attr_or_key(n, "file"),
        repo_root,
    )
    if isinstance(header_path, str):
        header_path = to_posix(header_path)

    # ---------- Label: prefer module leaf; fall back to basename ----------
    if module:
        label = module.split(".")[-1]
    else:
        leaf = to_posix(str(header_path or node_id))
        label = os.path.basename(leaf.rstrip("/")) if leaf else str(node_id)

    # ---------- Imports ----------
    pre_rows = attr_or_key(n, "imports_rows") or (
        (attr_or_key(n, "extra") or {}).get("imports_rows")
        if isinstance(n, dict)
        else None
    )
    raw_imports = attr_or_key(n, "imports")
    imp_rows = (
        _clean_and_normalize_import_rows(pre_rows)
        if pre_rows
        else _clean_and_normalize_import_rows(raw_imports)
    )

    # Fallback: mine file only if still empty
    if not imp_rows:
        try:
            abs_hint = attr_or_key(n, "file") or attr_or_key(n, "path") or ""
            p: Optional[str] = None
            if isinstance(abs_hint, str) and os.path.isabs(abs_hint):
                p = abs_hint
            elif isinstance(canon_path, str) and canon_path:
                p = str(Path(root) / canon_path) if root else None
            if p and p.endswith(".py") and os.path.exists(p):
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read(64_000)
                lines = (ln.strip() for ln in text.splitlines())
                imp_rows = [
                    s
                    for s in lines
                    if s
                    and not s.startswith("#")
                    and (s.startswith("import ") or (s.startswith("from ") and " import " in s))
                ]
                imp_rows = _dedupe_preserve_order(imp_rows)
        except Exception:
            pass

    # ---------- Defs / exports ----------
    pre_defs = attr_or_key(n, "defs_rows") or (
        (attr_or_key(n, "extra") or {}).get("defs_rows")
        if isinstance(n, dict)
        else None
    )
    classes = _dedupe_preserve_order(as_list(attr_or_key(n, "classes")))
    functions = _dedupe_preserve_order(as_list(attr_or_key(n, "functions")))
    globals_ = _dedupe_preserve_order(as_list(attr_or_key(n, "globals")))
    raw_exports = attr_or_key(n, "exports")

    if pre_defs:
        def_rows = [
            s
            for s in _dedupe_preserve_order(as_list(pre_defs))
            if s and s != "(none)"
        ]
    else:
        seen: set[str] = set()
        acc: List[str] = []
        for s in [*classes, *functions, *globals_]:
            if s not in seen:
                seen.add(s)
                acc.append(s)
        def_rows = acc or ["(none)"]

    parsed_for_tags = attr_or_key(n, "parsed")
    import_rows_annotated, import_tag_counts = build_import_row_annotations(
        imp_rows,
        parsed_for_tags,
    )

    extra: Dict[str, Any] = {
        "imports": imp_rows,
        "imports_rows": imp_rows,
        "imports_display": [a.get("row") for a in import_rows_annotated]
        if import_rows_annotated
        else imp_rows,
        "defs_rows": def_rows,
        "import_rows_annotated": import_rows_annotated,
        "import_tag_counts": import_tag_counts,
    }

    # ---------- Final payload ----------
    payload: Dict[str, Any] = {
        "node_id": node_id,
        "label": label,
        "kind": "file",
        "path": header_path or "",
        "module": module or None,
        "package": None,
        "file": header_path or "",
        "extra": extra,
        "imports": imp_rows,
        "imports_rows": imp_rows,
        "imports_display": extra["imports_display"],
        "defs_rows": def_rows,
        "classes": classes,
        "functions": functions,
        "globals": globals_,
        "exports": [e for e in as_list(raw_exports) if isinstance(e, str)],
    }
    return {k: v for k, v in payload.items() if v is not None}
