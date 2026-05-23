from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional
import json
import re

from .canonical_web import to_posix


@dataclass(frozen=True)
class TSPathMap:
    """
    Minimal tsconfig path mapping.

    Fields
    ------
    - tsconfig_rel_dir:
        POSIX path to the directory containing the effective tsconfig,
        relative to repo root.

    - paths_base_dir:
        POSIX repo-root-relative directory from which `compilerOptions.paths`
        targets should be interpreted.

        This is the normalized resolution anchor used by expand_ts_paths().

    - paths:
        compilerOptions.paths mapping (pattern -> target patterns)

    - uses_legacy_base_url:
        True when the effective mapping was influenced by compilerOptions.baseUrl.
        This lets downstream diagnostics surface legacy TS resolution behavior
        without making baseUrl a core concept in the resolver contract.

    Notes
    -----
    This object does not itself resolve specifiers; it only describes how to
    expand path aliases into repo-root-anchored candidate paths.
    """
    tsconfig_rel_dir: str
    paths_base_dir: str
    paths: Dict[str, List[str]]
    uses_legacy_base_url: bool


# ─────────────────────────────────────────────────────────────────────────────
# JSONC loader helpers (tsconfig commonly allows comments + trailing commas)
# ─────────────────────────────────────────────────────────────────────────────

_JSONC_COMMENT_RE = re.compile(
    r"""
    (//[^\n]*$)           |  # line comments
    (/\*.*?\*/)              # block comments
    """,
    re.MULTILINE | re.DOTALL,
)
_JSONC_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _load_json_loose(p: Path) -> Optional[dict]:
    """
    Load tsconfig-ish JSON (JSONC-friendly).

    Supports:
      - // and /* */ comments
      - trailing commas

    NOTE:
      This is intentionally lightweight. If you need full JSON5 later, swap in a
      dedicated parser; keep this minimal for now.
    """
    try:
        text = p.read_text(encoding="utf-8")
        text = _JSONC_COMMENT_RE.sub("", text)
        text = _JSONC_TRAILING_COMMA_RE.sub(r"\1", text)
        return json.loads(text)
    except Exception:
        return None


def _norm_paths(paths_obj: object) -> Dict[str, List[str]]:
    if not isinstance(paths_obj, dict):
        return {}
    norm_paths: Dict[str, List[str]] = {}
    for k, v in paths_obj.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            continue
        out: List[str] = []
        for item in v:
            if isinstance(item, str) and item.strip():
                out.append(to_posix(item).strip())
        if out:
            norm_paths[to_posix(k).strip()] = out
    return norm_paths


def _strip_leading_current_dir(p: str) -> str:
    """
    Remove leading "./" segments and a single leading "/" if present,
    but DO NOT destroy "../" parent traversal.
    """
    p = to_posix(p).strip()
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        p = p[1:]
    return p


def _join_posix_parts(*parts: str) -> str:
    """
    Join path fragments as normalized POSIX, skipping empty parts.
    Does not collapse '..' traversal beyond normal path-string cleanup.
    """
    cleaned: List[str] = []
    for part in parts:
        s = to_posix(part or "").strip()
        if not s or s == ".":
            continue
        cleaned.append(s.strip("/"))
    joined = "/".join(p for p in cleaned if p != "")
    return _strip_leading_current_dir(joined)


def _compute_paths_base_dir(*, tsconfig_rel_dir: str, base_url: Optional[str]) -> str:
    """
    Normalize the effective directory from which TS `paths` targets should be
    interpreted, expressed as a repo-root-relative POSIX path.

    Semantics preserved:
      - no baseUrl -> use tsconfig directory
      - baseUrl="." -> use tsconfig directory
      - baseUrl="src" -> use <tsconfig_rel_dir>/src
    """
    ts_dir = _strip_leading_current_dir(tsconfig_rel_dir or "")
    bu = (base_url or "").strip()
    bu = to_posix(bu).strip().rstrip("/")
    if bu == ".":
        bu = ""
    return _join_posix_parts(ts_dir, bu)


def _load_tsconfig_paths_from_file(*, repo_root: Path, tsconfig_path: Path) -> Optional[TSPathMap]:
    """
    Load compilerOptions.paths and normalize the effective base directory used
    to interpret those paths.

    Legacy compatibility:
      - compilerOptions.baseUrl is still understood when present
      - but it is normalized immediately into `paths_base_dir`
      - downstream code should rely on `paths_base_dir`, not raw baseUrl
    """
    if not tsconfig_path.is_file():
        return None

    data = _load_json_loose(tsconfig_path)
    if not isinstance(data, dict):
        return None

    co = data.get("compilerOptions") or {}
    if not isinstance(co, dict):
        co = {}

    raw_base_url = co.get("baseUrl")
    if isinstance(raw_base_url, str):
        base_url = to_posix(raw_base_url).strip().rstrip("/")
        if base_url == ".":
            base_url = ""
        uses_legacy_base_url = True
    else:
        base_url = None
        uses_legacy_base_url = False

    norm_paths = _norm_paths(co.get("paths"))

    try:
        tsconfig_rel_dir = to_posix(tsconfig_path.parent.resolve().relative_to(repo_root.resolve()))
    except Exception:
        tsconfig_rel_dir = to_posix(tsconfig_path.parent)

    tsconfig_rel_dir = _strip_leading_current_dir(tsconfig_rel_dir)
    paths_base_dir = _compute_paths_base_dir(tsconfig_rel_dir=tsconfig_rel_dir, base_url=base_url)

    return TSPathMap(
        tsconfig_rel_dir=tsconfig_rel_dir,
        paths_base_dir=paths_base_dir,
        paths=norm_paths,
        uses_legacy_base_url=uses_legacy_base_url,
    )


def _resolve_extends_path(*, tsconfig_path: Path, extends_value: str) -> Optional[Path]:
    """
    Resolve tsconfig "extends" to an actual file path.

    Supports:
      - relative paths from tsconfig directory
      - may omit .json
      - package-based extends via node_modules lookup
        (e.g. '@tsconfig/node18/tsconfig.json')
    """
    ext = (extends_value or "").strip()
    if not ext:
        return None

    base_dir = tsconfig_path.parent

    # 1) Relative or absolute paths (including Windows drive "C:\\..." etc.)
    if ext.startswith(".") or ext.startswith("/") or ":" in ext:
        cand = (base_dir / ext).resolve()
        if cand.is_file():
            return cand

        if cand.suffix.lower() != ".json":
            cand2 = Path(str(cand) + ".json")
            if cand2.is_file():
                return cand2.resolve()

        return None

    # 2) Package-based extends: walk upwards for node_modules/<ext>[.json]
    cur = base_dir.resolve()
    while True:
        nm = (cur / "node_modules" / ext).resolve()
        if nm.is_file():
            return nm

        if nm.suffix.lower() != ".json":
            nm2 = Path(str(nm) + ".json")
            if nm2.is_file():
                return nm2.resolve()

        if cur.parent == cur:
            break
        cur = cur.parent

    return None


def _raw_base_url_from_tsconfig(tsconfig_path: Path) -> Optional[str]:
    """
    Read only the local compilerOptions.baseUrl from a tsconfig file.

    Returns:
      - None if baseUrl is not defined locally
      - "" for "."
      - normalized POSIX relative string otherwise
    """
    data = _load_json_loose(tsconfig_path)
    if not isinstance(data, dict):
        return None

    co = data.get("compilerOptions") or {}
    if not isinstance(co, dict):
        return None

    raw = co.get("baseUrl")
    if not isinstance(raw, str):
        return None

    out = to_posix(raw).strip().rstrip("/")
    if out == ".":
        out = ""
    return out


def _merge_pathmaps(
    parent: Optional[TSPathMap],
    child: TSPathMap,
    *,
    child_local_base_url: Optional[str],
) -> TSPathMap:
    """
    Merge tsconfig mappings following TS precedence:

      - child local baseUrl overrides parent effective baseUrl when present
      - child paths keys override parent paths keys
      - tsconfig_rel_dir should be the child's directory (the closest file)

    We do NOT preserve raw baseUrl as a first-class field. Instead, we compute
    the child's effective `paths_base_dir`.
    """
    if parent is None:
        return child

    if child_local_base_url is not None:
        effective_paths_base_dir = _compute_paths_base_dir(
            tsconfig_rel_dir=child.tsconfig_rel_dir,
            base_url=child_local_base_url,
        )
        uses_legacy_base_url = True
    else:
        parent_base_rel_to_child = ""
        parent_base = _strip_leading_current_dir(parent.paths_base_dir)
        parent_ts_dir = _strip_leading_current_dir(parent.tsconfig_rel_dir)

        if parent_base == parent_ts_dir:
            parent_base_rel_to_child = ""
        elif parent_ts_dir and parent_base.startswith(parent_ts_dir + "/"):
            parent_base_rel_to_child = parent_base[len(parent_ts_dir) + 1 :]
        else:
            # Defensive fallback: preserve the already-normalized parent base as-is
            # only if it cannot be expressed relative to parent tsconfig dir.
            # In normal TS semantics, this branch should rarely be hit.
            parent_base_rel_to_child = parent_base

        effective_paths_base_dir = _compute_paths_base_dir(
            tsconfig_rel_dir=child.tsconfig_rel_dir,
            base_url=parent_base_rel_to_child,
        )
        uses_legacy_base_url = parent.uses_legacy_base_url

    merged_paths: Dict[str, List[str]] = dict(parent.paths)
    merged_paths.update(child.paths)

    return TSPathMap(
        tsconfig_rel_dir=child.tsconfig_rel_dir,
        paths_base_dir=effective_paths_base_dir,
        paths=merged_paths,
        uses_legacy_base_url=uses_legacy_base_url,
    )


# ─────────────────────────────────────────────────────────────────────────────
# tsconfig cache
# ─────────────────────────────────────────────────────────────────────────────

class TSConfigCache:
    def __init__(self) -> None:
        # Effective mapping by tsconfig abs path (extends merged)
        self._effective_by_tsconfig_abs: Dict[str, Optional[TSPathMap]] = {}
        # Nearest tsconfig.json abs path by directory (repo-relative POSIX dir key)
        self._nearest_tsconfig_by_dir_rel: Dict[str, Optional[str]] = {}

    def find_nearest_tsconfig(self, *, repo_root: Path, src_file_rel_posix: str) -> Optional[Path]:
        """
        Walk upwards from the file's directory looking for tsconfig.json.
        Cache by repo-relative directory key.
        """
        repo_root_abs = repo_root.resolve()

        src_file_rel_posix = _strip_leading_current_dir(src_file_rel_posix)
        src_dir_rel = to_posix(str(PurePosixPath(src_file_rel_posix).parent)).rstrip("/")
        if src_dir_rel in ("", "."):
            src_dir_rel = ""

        if src_dir_rel in self._nearest_tsconfig_by_dir_rel:
            ts_abs = self._nearest_tsconfig_by_dir_rel[src_dir_rel]
            return Path(ts_abs) if ts_abs else None

        cur = (repo_root_abs / src_dir_rel).resolve()

        try:
            cur.relative_to(repo_root_abs)
        except Exception:
            cur = repo_root_abs

        while True:
            cand = cur / "tsconfig.json"
            if cand.is_file():
                ts_abs = str(cand.resolve())
                self._nearest_tsconfig_by_dir_rel[src_dir_rel] = ts_abs
                return Path(ts_abs)

            if cur == repo_root_abs:
                break
            nxt = cur.parent
            if nxt == cur:
                break
            cur = nxt

        self._nearest_tsconfig_by_dir_rel[src_dir_rel] = None
        return None

    def load_effective_pathmap(self, *, repo_root: Path, tsconfig_path: Path) -> Optional[TSPathMap]:
        """
        Load a tsconfig and merge any extends chain into an effective TSPathMap.
        Cached by tsconfig absolute path.
        """
        repo_root_abs = repo_root.resolve()
        ts_abs = str(tsconfig_path.resolve())
        if ts_abs in self._effective_by_tsconfig_abs:
            return self._effective_by_tsconfig_abs[ts_abs]

        base = _load_tsconfig_paths_from_file(repo_root=repo_root_abs, tsconfig_path=tsconfig_path)
        if base is None:
            self._effective_by_tsconfig_abs[ts_abs] = None
            return None

        parent_map: Optional[TSPathMap] = None
        data = _load_json_loose(tsconfig_path) or {}
        ext_val = data.get("extends")
        if isinstance(ext_val, str) and ext_val.strip():
            parent_path = _resolve_extends_path(tsconfig_path=tsconfig_path, extends_value=ext_val)
            if parent_path is not None:
                parent_map = self.load_effective_pathmap(repo_root=repo_root_abs, tsconfig_path=parent_path)

        child_local_base_url = _raw_base_url_from_tsconfig(tsconfig_path)
        eff = _merge_pathmaps(parent_map, base, child_local_base_url=child_local_base_url)
        self._effective_by_tsconfig_abs[ts_abs] = eff
        return eff


# single cache instance for the process/run
_TS_CACHE = TSConfigCache()


# ─────────────────────────────────────────────────────────────────────────────
# Public APIs
# ─────────────────────────────────────────────────────────────────────────────

def get_pathmap_for_file(*, repo_root: Path, src_file_rel_posix: str) -> Optional[TSPathMap]:
    """
    Nearest-config API:
      - find nearest tsconfig.json for the file
      - load effective mapping (extends merged)

    IMPORTANT:
      In the baseline-first resolution strategy, this is intended as a FALLBACK
      pathmap (used only when the baseline single-config pathmap fails).
    """
    repo_root_abs = repo_root.resolve()
    tsconfig_path = _TS_CACHE.find_nearest_tsconfig(repo_root=repo_root_abs, src_file_rel_posix=src_file_rel_posix)
    if tsconfig_path is None:
        return None
    return _TS_CACHE.load_effective_pathmap(repo_root=repo_root_abs, tsconfig_path=tsconfig_path)


def load_repo_tsconfig_paths(repo_root: Path) -> Optional[TSPathMap]:
    """
    Back-compat: load repo_root/tsconfig.json if present (NO extends merge).
    Prefer load_effective_repo_tsconfig_paths() for baseline correctness.
    """
    repo_root_abs = repo_root.resolve()
    return _load_tsconfig_paths_from_file(repo_root=repo_root_abs, tsconfig_path=(repo_root_abs / "tsconfig.json"))


def load_effective_repo_tsconfig_paths(repo_root: Path) -> Optional[TSPathMap]:
    """
    Baseline single-config API:
      - load repo_root/tsconfig.json as an effective TSPathMap (extends merged)

    This is the recommended "single config baseline" pathmap.
    """
    repo_root_abs = repo_root.resolve()
    tsconfig_path = repo_root_abs / "tsconfig.json"
    if not tsconfig_path.is_file():
        return None
    return _TS_CACHE.load_effective_pathmap(repo_root=repo_root_abs, tsconfig_path=tsconfig_path)


def get_pathmaps_for_file_fallback(*, repo_root: Path, src_file_rel_posix: str) -> List[TSPathMap]:
    """
    Return pathmaps in fallback order for alias resolution:

      1) repo-root effective tsconfig (single-config baseline)
      2) nearest effective tsconfig for the file (fallback-only)

    Intended usage:
      - try alias resolution with [0]
      - only if still unresolved, try [1]

    Dedupes when nearest effectively equals repo-root.
    """
    repo_root_abs = repo_root.resolve()

    out: List[TSPathMap] = []
    pm_root = load_effective_repo_tsconfig_paths(repo_root_abs)
    if pm_root is not None:
        out.append(pm_root)

    pm_near = get_pathmap_for_file(repo_root=repo_root_abs, src_file_rel_posix=src_file_rel_posix)
    if pm_near is None:
        return out

    if not out:
        out.append(pm_near)
        return out

    same = (
        pm_near.tsconfig_rel_dir == out[0].tsconfig_rel_dir
        and pm_near.paths_base_dir == out[0].paths_base_dir
        and pm_near.paths == out[0].paths
        and pm_near.uses_legacy_base_url == out[0].uses_legacy_base_url
    )
    if not same:
        out.append(pm_near)

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Path expansion
# ─────────────────────────────────────────────────────────────────────────────

def _pattern_match(spec: str, pattern: str) -> Optional[str]:
    """
    Match TS path patterns with at most one '*' wildcard.
    Returns the star-capture if matched, else None.
    """
    if "*" not in pattern:
        return "" if spec == pattern else None
    pre, post = pattern.split("*", 1)
    if not spec.startswith(pre):
        return None
    if post and not spec.endswith(post):
        return None
    mid = spec[len(pre) : (len(spec) - len(post) if post else len(spec))]
    return mid


def expand_ts_paths(*, pm: TSPathMap, spec: str) -> List[str]:
    """
    Expand an alias spec into candidate repo-root-anchored paths (POSIX),
    WITHOUT checking existence.

    IMPORTANT:
      - pm.paths_base_dir is already normalized to the repo-root-relative
        directory from which TS path targets should be interpreted.
      - expand_ts_paths() therefore no longer needs to reason about raw baseUrl.
    """
    spec = to_posix(spec).strip()
    if not spec or not pm.paths:
        return []

    out: List[str] = []
    base_dir = _strip_leading_current_dir(pm.paths_base_dir or "")

    for pat, targets in pm.paths.items():
        star = _pattern_match(spec, pat)
        if star is None:
            continue

        for tgt in targets:
            cand = tgt.replace("*", star) if "*" in tgt else tgt
            cand = _strip_leading_current_dir(cand)
            joined = _join_posix_parts(base_dir, cand)

            if joined and joined not in out:
                out.append(joined)

    return out