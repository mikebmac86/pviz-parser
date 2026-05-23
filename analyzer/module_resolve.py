# analyzer/module_resolve.py
from __future__ import annotations
from typing import List, Optional, Tuple, Iterable, Set
from pathlib import Path
import re
from i_o.workspace_io import get_active_workspace
from analyzer.config import AnalyzerCfg
from analyzer.module_map import ModuleMap
from analyzer.imports_lex import has_dunder_getattr  # dynamic facade signal
from adapters.canonical import normalize_root as norm_root_SSOT

# ---------------------------
# Relative/absolute resolution
# ---------------------------

_IDENT_RX = re.compile(r"[^0-9A-Za-z_]+")

def _sanitize_ident(seg: str) -> str:
    """
    Make a segment safe for a dotted module:
      - replace non [0-9A-Za-z_] with '_'
      - collapse multiple '_' and strip
      - prefix '_' if segment starts with a digit
    """
    if not seg:
        return "_"
    s = _IDENT_RX.sub("_", seg)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "_"
    if s[0].isdigit():
        s = "_" + s
    return s

# top-level helpers (near other small helpers)
def _normalize_root(root: Path | str) -> Path:
    p = norm_root_SSOT(root)
    if p is None:
        if root:
            return Path(root).resolve()
        return get_active_workspace().scan_root
    return p


def absolutize_module(token: str, consumer_logical_module: str) -> Optional[str]:
    """
    Absolutize a possibly-relative module token using the consumer's logical module.

    Handles '', '.', '..', '.util', 'pkg.sub', etc.
    Rules:
      - ''          -> current package (drop the last component of consumer if present)
      - leading '.' -> relative to current package with Python semantics
      - otherwise   -> already absolute
    """
    t = (token or "").strip()
    cm = (consumer_logical_module or "").strip(".")

    if t == "":
        if not cm:
            return None
        # For bare '' (e.g., 'from . import x'), use the current PACKAGE.
        # If consumer has at least two segments, drop its leaf (module).
        # If consumer is already the package root (one segment), keep it.
        parts = cm.split(".")
        base_parts = parts[:-1] if len(parts) >= 2 else parts
        return ".".join(base_parts) if base_parts else None

    # Count leading dots
    i = 0
    while i < len(t) and t[i] == ".":
        i += 1
    if i == 0:
        # already absolute
        return t

    # relative import
    rest = t[i:]
    pkg_parts = cm.split(".") if cm else []

    # Only drop the leaf when the consumer has a leaf to drop (>=2 segments).
    # If the consumer is a package root (one segment), keep it.
    base_pkg = pkg_parts[:-1] if len(pkg_parts) >= 2 else pkg_parts

    # 'from ..x import y' -> i dots means go up (i-1)
    climb = max(0, i - 1)
    if climb > len(base_pkg):
        return None

    base_parts = base_pkg[: len(base_pkg) - climb]
    if rest:
        base_parts += [p for p in rest.split(".") if p]

    abs_mod = ".".join(base_parts)
    return abs_mod or None

def _path_to_file_id(p: Path, repo_root: Path) -> str:
    try:
        rr = _normalize_root(repo_root)
        pr = Path(p).resolve(strict=False)
        return pr.relative_to(rr).as_posix()
    except Exception:
        return Path(p).resolve(strict=False).as_posix()

def _resolve_absolute_with_ascend(abs_mod: str, m: ModuleMap) -> str:
    """
    Resolve an absolute dotted module -> file_id by ascending parents
    until a physical module is found or root is reached. Returns "" if none.
    """
    _dbg_analyzer = abs_mod.startswith("analyzer")

    probe = abs_mod
    while probe:
        p = m.path_for_module(probe)
        if p is not None:
            return _path_to_file_id(p, m.repo_root)
        if "." not in probe:
            break
        probe = probe.rsplit(".", 1)[0]

    return ""


def resolve_dotted(imported: str, consumer_mod: str, m: ModuleMap, cfg: AnalyzerCfg) -> str:
    """
    Resolve a dotted import string (absolute or relative) from the POV of consumer_mod.
    Returns canonical file_id (repo-relative path) or "" if unknown/external.
    """
    abs_mod = absolutize_module(imported, consumer_mod)
    if not abs_mod:
        return ""
    return _resolve_absolute_with_ascend(abs_mod, m)


def expand_targets(
    mod_token: str | None,
    names: List[str],
    star: bool,
    consumer_logical_module: str,
    *,
    level: int = 0,
) -> List[str]:
    # Reconstruct the true relative token by prefixing dots from `level`.
    dotted = ("." * level) + (mod_token or "")
    base = absolutize_module(dotted, consumer_logical_module)
    if not base:
        return []
    if star or not names:
        return [base]
    out, seen = [], set()
    for c in [*(f"{base}.{n}" for n in names), base]:
        if c not in seen:
            seen.add(c)
            out.append(c)

    return out
# ---------------------------
# Facade re-export inference
# ---------------------------

_RE_DEF_OR_ASSIGN_TMPL = r"^(?:def|class)\s+{sym}\b|^{sym}\s*="

def _re_def_or_assign(sym: str) -> re.Pattern[str]:
    return re.compile(_RE_DEF_OR_ASSIGN_TMPL.format(sym=re.escape(sym)), re.M)

def _iter_pkg_py(pkg_dir: Path) -> Iterable[Path]:
    for p in pkg_dir.glob("*.py"):
        if p.name == "__init__.py":
            continue
        yield p

def _guess_reexports(pkg_dir: Path, symbol: str) -> Set[Path]:
    """
    Heuristic search for top-level defs/assignments of `symbol` in pkg/*.py (excluding __init__.py).
    Returns a set of Paths for candidate modules.
    """
    hits: Set[Path] = set()
    pat = _re_def_or_assign(symbol)
    for py in _iter_pkg_py(pkg_dir):
        try:
            txt = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if pat.search(txt):
            hits.add(py)
    return hits

def _module_dir_for_pkg(pkg_module: str, m: ModuleMap) -> Optional[Path]:
    """
    Given a package logical module (e.g., 'i_o'), return its physical directory (containing __init__.py).
    """
    init_path = m.path_for_module(f"{pkg_module}.__init__")
    if init_path and init_path.exists():
        return init_path.parent
    # Handle namespace/flat packages where __init__ may be omitted:
    maybe = m.path_for_module(pkg_module)
    if maybe and maybe.is_dir():
        return maybe
    return None

def _file_ids(paths: Iterable[Path], m: ModuleMap) -> List[str]:
    out: List[str] = []
    for p in paths:
        out.append(_path_to_file_id(p, m.repo_root))
    return out

def _is_dynamic_package(pkg_module: str, m: ModuleMap) -> bool:
    """
    A package is considered 'dynamic' if its __init__.py defines __getattr__(name),
    suggesting runtime export by name.
    """
    init = m.path_for_module(f"{pkg_module}.__init__")
    if not init or not init.exists():
        return False
    try:
        src = init.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return has_dunder_getattr(src)

def _resolve_reexport_candidates(pkg_module: str, symbol: str, m: ModuleMap) -> List[str]:
    """
    If 'from pkg import symbol' cannot be resolved via concrete physical modules,
    search pkg/*.py for a defining def/class/assignment of 'symbol' and return
    SOFT candidate producer file_ids.
    """
    pkg_dir = _module_dir_for_pkg(pkg_module, m)
    if not pkg_dir:
        return []
    return _file_ids(_guess_reexports(pkg_dir, symbol), m)


# ---------------------------
# Public resolution helpers
# ---------------------------

# in analyzer/module_resolve.py

def module_names_for_path(
    root: Path,
    file_path: Path,
    cfg: Optional[AnalyzerCfg] = None,
) -> Tuple[str, str]:
    root = _normalize_root(root)
    fp = Path(file_path).resolve(strict=False)

    # physical dotted path relative to root (keep '__init__' if present)
    try:
        rel = fp.relative_to(root)
    except Exception:
        rel = Path(fp.name)

    phys_parts = list(rel.parts)
    if phys_parts:
        phys_parts[-1] = Path(phys_parts[-1]).stem
    # sanitize every part
    phys_parts = [_sanitize_ident(p) for p in phys_parts if p]
    physical = ".".join(phys_parts)

    # logical via ModuleMap (preferred)
    mm = ModuleMap(root, cfg or AnalyzerCfg())
    logical_raw = mm.module_name_for_path(fp) or ""

    if logical_raw:
        logical = ".".join(_sanitize_ident(p) for p in logical_raw.split(".") if p)
    else:
        logical = physical

    # --- validate; fallback if it still looks like a dotted path string ---
    # Heuristics: contains drive letter / colon / spaces / backslashes, or starts with 'C.'/'D.' etc.
    bad = (
        ":" in logical_raw or
        " " in logical_raw or
        "\\" in logical_raw or
        (len(logical_raw) >= 2 and logical_raw[1] == ":" ) or
        logical.startswith(("A.", "B.", "C.", "D.", "E.", "F.", "G.",
                            "H.", "I.", "J.", "K.", "L.", "M.", "N.",
                            "O.", "P.", "Q.", "R.", "S.", "T.", "U.",
                            "V.", "W.", "X.", "Y.", "Z."))
    )
    if bad:
        logical = physical

    return physical, logical


def drop_init_suffix(token: str) -> str:
    return token[:-9] if token.endswith(".__init__") else token

def logical_module_for_path(mapper: ModuleMap, file_path: Path) -> Optional[str]:
    """
    Convenience wrapper for callers that already hold a ModuleMap.
    """
    return mapper.module_name_for_path(file_path)


# ---------------------------
# External module labeling
# ---------------------------

def is_external_module(token: Optional[str], cfg: AnalyzerCfg, m: ModuleMap) -> bool:
    """
    True if 'token' cannot be mapped to a path in the workspace AND should be
    treated as external (by prefix or by absence).
    """
    if not token:
        return False
    # Fast accept if we can map it internally
    if m.path_for_module(token) is not None:
        return False
    top = token.split(".", 1)[0]
    prefixes = tuple(cfg.external_module_prefixes or ())
    # If user configured prefixes, prefer them; otherwise treat all unmapped as external.
    return not prefixes or token.startswith(prefixes) or top in prefixes


def producer_id_for_module(token: Optional[str], m: ModuleMap, cfg: AnalyzerCfg) -> str:
    """
    Return a stable producer id for a logical module token:
      - internal: repo-relative POSIX path (e.g., "pkg/mod.py" or "pkg/__init__.py")
      - external: "external:<top_pkg>"
    """
    if not token:
        return "external:unknown"
    p = m.path_for_module(token)
    if p is not None:
        # Use repo-relative POSIX path as the canonical node id.
        try:
            rel = p.relative_to(m.repo_root).as_posix()
        except Exception:
            rel = p.as_posix()
        return rel
    # External fallback
    top = token.split(".", 1)[0]
    return f"external:{top or 'unknown'}"


# ---------------------------
# Unified candidate resolver
# ---------------------------

def resolve_import_candidates(
    mod_token: str | None,
    names: List[str],
    star: bool,
    consumer_logical_module: str,
    m: ModuleMap,
    cfg: AnalyzerCfg,
    *,
    level: int = 0,
) -> Tuple[List[str], List[str]]:

    """
    Resolve an import into candidate producer file_ids.

    Returns (hard_ids, soft_ids):
      - hard_ids: physical files that directly match logical modules via ascend.
      - soft_ids: inferred providers via facade re-export search or dynamic packages.

    Algorithm:
      1) Expand logical targets (e.g., 'from i_o import export_scene_pdf' ->
         ['i_o.export_scene_pdf', 'i_o']).
      2) For each logical target, attempt physical resolution with ascend.
      3) If a 'from pkg import name' specific submodule cannot be resolved:
           - Try re-export inference across pkg/*.py for 'name' (soft candidates).
           - If pkg is dynamic (__getattr__), and cfg says to keep dynamic packages,
             add all direct children *.py as soft candidates (pattern-restricted if configured).
    """
    logicals = expand_targets(mod_token, names, star, consumer_logical_module, level=level)
    hard: List[str] = []
    soft: List[str] = []

    seen_hard: Set[str] = set()
    seen_soft: Set[str] = set()

    for target in logicals:
        # Try hard (physical ascend)
        hard_id = _resolve_absolute_with_ascend(target, m)
        if hard_id:
            if hard_id not in seen_hard:
                seen_hard.add(hard_id)
                hard.append(hard_id)
            continue

        # If the target is of the form "<pkg>.<symbol>" and unresolved, try facade inference.
        if "." in target:
            pkg, sym = target.rsplit(".", 1)
            # Re-export inference (soft)
            if cfg.infer_facade_reexports:
                cands = _resolve_reexport_candidates(pkg, sym, m)
                for cid in cands:
                    if cid not in seen_soft:
                        seen_soft.add(cid)
                        soft.append(cid)

            # Dynamic package keep (soft)
            if cfg.dynamic_package_keep != "off" and _is_dynamic_package(pkg, m):
                pkg_dir = _module_dir_for_pkg(pkg, m)
                if pkg_dir:
                    # Strategy: keep matching children (pattern vs all)
                    if cfg.dynamic_package_keep == "pattern" and cfg.dynamic_keep_patterns:
                        patterns = list(cfg.dynamic_keep_patterns)
                        for py in _iter_pkg_py(pkg_dir):
                            name = py.name
                            if any(Path(name).match(pat) for pat in patterns):
                                fid = _path_to_file_id(py, m.repo_root)
                                if fid not in seen_soft:
                                    seen_soft.add(fid)
                                    soft.append(fid)
                    else:
                        # keep all direct children *.py
                        for py in _iter_pkg_py(pkg_dir):
                            fid = _path_to_file_id(py, m.repo_root)
                            if fid not in seen_soft:
                                seen_soft.add(fid)
                                soft.append(fid)

        else:
            # unresolved bare module: no-op; could be external
            pass
    return hard, soft
