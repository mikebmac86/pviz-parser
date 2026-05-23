# backend/saas_analyzer/analyzer/go/parse_dispatch.py
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass
class GoAstParsedFile:
    path: Path
    parse_status: str
    error_snippet: Optional[str]
    package: Optional[str]
    classes: List[str]
    functions: List[str]
    globals: List[str]
    all_exports: List[str]
    loc_code: Optional[int]

    # Rich optional fields (merge-trimmable)
    imports: Optional[List[dict]] = None
    symbols: Optional[List[dict]] = None
    build: Optional[dict] = None


def _default_helper_path() -> Path:
    # Prefer explicit env var (useful in docker)
    env = os.environ.get("PVIZ_GOEXTRACT_BIN", "").strip()
    if env:
        return Path(env)

    # Default relative to repo root layout: tools/goextract/goextract
    # Adjust if your worker image copies it elsewhere.
    return Path("tools/goextract/goextract")


def _cap_mask(*, include_docs: bool, include_imports: bool, include_build: bool) -> int:
    """
    Capability bitmask for what we requested/loaded for a given file.
      bit0: docs (affects SymbolRec.Doc population)
      bit1: imports (pf.imports)
      bit2: build (pf.build)
    """
    m = 0
    if include_docs:
        m |= 1 << 0
    if include_imports:
        m |= 1 << 1
    if include_build:
        m |= 1 << 2
    return m


def _abs_key(p: Path | str) -> str:
    """
    Canonical cache key aligned with goextract's filepath.Abs behavior:
      - use absolute path
      - DO NOT realpath/resolve symlinks
    """
    try:
        return os.path.abspath(os.fspath(p))
    except Exception:
        try:
            return str(Path(p).absolute())
        except Exception:
            return str(p)


def _is_go_file(p: str) -> bool:
    return p.lower().endswith(".go")


class GoBatchParser:
    """
    Batch Go AST parser backed by the goextract helper.

    Behavior:
      - Cache is APPENDABLE (safe for long-lived worker processes).
      - load_batch() may be called multiple times and will:
          * only run goextract for files missing from cache OR missing requested capabilities
          * merge results into the existing cache
      - Cache is keyed by absolute path string (abspath), not realpath.

    Notes:
      - If the same file is requested again with "richer" options (imports/build/docs),
        load_batch() will re-run for that file to fill missing optional fields.
      - For correctness with the "mandatory cache" posture elsewhere:
          * if goextract omits a result entry for a requested file, we insert a synthetic
            error record so downstream bp.get() is a cache hit (not a cache miss).
    """

    def __init__(self, helper_bin: Path):
        self.helper_bin = helper_bin
        self._cache: Dict[str, GoAstParsedFile] = {}
        self._caps: Dict[str, int] = {}  # path_str -> capability mask present

    def reset(self) -> None:
        """Clear all cached parsed results (explicit escape hatch)."""
        self._cache.clear()
        self._caps.clear()

    def load_batch(
        self,
        files: Iterable[Path],
        *,
        include_docs: bool = True,
        include_imports: bool = True,
        include_build: bool = True,
        timeout_s: int = 120,
    ) -> None:
        """
        Run the Go helper for a batch of files and cache results.

        Safe to call multiple times:
          - Only parses files that are missing in cache OR missing requested capabilities.
          - Deterministic request ordering.
        """
        # Normalize + filter to absolute .go paths (no resolve/realpath)
        file_list: List[str] = []
        for p in files:
            try:
                ap = _abs_key(p)
            except Exception:
                continue
            if ap and _is_go_file(ap):
                file_list.append(ap)

        # Deterministic order + de-dupe
        file_list = sorted(set(file_list))
        if not file_list:
            return

        wanted = _cap_mask(
            include_docs=bool(include_docs),
            include_imports=bool(include_imports),
            include_build=bool(include_build),
        )

        # Only parse files that need work (missing or lacking requested capabilities)
        to_run: List[str] = []
        for ap in file_list:
            have_caps = self._caps.get(ap, 0)
            if ap not in self._cache or (have_caps & wanted) != wanted:
                to_run.append(ap)

        if not to_run:
            return

        req = {
            "files": to_run,
            "include_docs": bool(include_docs),
            "include_imports": bool(include_imports),
            "include_build": bool(include_build),
        }

        bin_path = self.helper_bin
        if not bin_path.exists():
            raise FileNotFoundError(
                f"Go extractor binary not found at {bin_path}. "
                f"Set PVIZ_GOEXTRACT_BIN or build tools/goextract/goextract."
            )

        proc = subprocess.run(
            [str(bin_path)],
            input=json.dumps(req).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )

        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="ignore")[:400]
            raise RuntimeError(f"goextract_failed rc={proc.returncode} stderr={stderr}")

        out = json.loads((proc.stdout or b"{}").decode("utf-8", errors="ignore") or "{}")

        # Fatal shape
        if isinstance(out, dict) and out.get("fatal"):
            raise RuntimeError(f"goextract_fatal:{out.get('fatal')}")

        results = out.get("results") if isinstance(out, dict) else None
        if not isinstance(results, dict):
            raise RuntimeError("goextract_invalid_output:missing_results")

        # Merge results into cache; compute *actual* capability mask from returned shape.
        returned_keys: set[str] = set()

        for abs_path, rec in results.items():
            if not isinstance(abs_path, str) or not isinstance(rec, dict):
                continue

            # Normalize returned path to our cache key scheme (abspath, no resolve)
            key = _abs_key(abs_path)
            returned_keys.add(key)

            path_obj = Path(abs_path)

            pf = GoAstParsedFile(
                path=path_obj,
                parse_status=str(rec.get("parse_status") or "error"),
                error_snippet=(str(rec.get("error_snippet"))[:200] if rec.get("error_snippet") else None),
                package=(str(rec.get("package")) if rec.get("package") else None),
                classes=list(rec.get("classes") or []),
                functions=list(rec.get("functions") or []),
                globals=list(rec.get("globals") or []),
                all_exports=list(rec.get("all_exports") or []),
                loc_code=(int(rec.get("loc_code")) if rec.get("loc_code") is not None else None),
                imports=rec.get("imports") if ("imports" in rec or "Imports" in rec) else None,
                symbols=rec.get("symbols") if ("symbols" in rec or "Symbols" in rec) else None,
                build=rec.get("build") if ("build" in rec or "Build" in rec) else None,
            )

            # Determine what capabilities we actually got back in this record.
            # Docs is not directly inferable from content (it affects Doc fields inside symbols),
            # but if we ran goextract with include_docs, we consider the docs capability satisfied.
            got = 0
            if include_docs:
                got |= 1 << 0
            if include_imports and ("imports" in rec or "Imports" in rec):
                got |= 1 << 1
            if include_build and ("build" in rec or "Build" in rec):
                got |= 1 << 2

            self._cache[key] = pf
            self._caps[key] = self._caps.get(key, 0) | got

        # Ensure every requested file is a cache hit (avoid downstream cache-miss errors).
        # If goextract omitted an entry, insert a synthetic error record.
        for ap in to_run:
            if ap in returned_keys:
                continue
            self._cache[ap] = GoAstParsedFile(
                path=Path(ap),
                parse_status="error",
                error_snippet="goextract_missing_result",
                package=None,
                classes=[],
                functions=[],
                globals=[],
                all_exports=[],
                loc_code=None,
                imports=None,
                symbols=None,
                build=None,
            )
            # We attempted to load with `wanted`; keep caps at least indicating the attempt.
            # (We *do not* claim imports/build were actually returned.)
            # Mark docs attempt only if requested, since it's otherwise ambiguous.
            got = 0
            if include_docs:
                got |= 1 << 0
            self._caps[ap] = self._caps.get(ap, 0) | got

    def get(self, path: Path) -> Optional[GoAstParsedFile]:
        try:
            return self._cache.get(_abs_key(path))
        except Exception:
            return None

    def cache_size(self) -> int:
        """Lightweight introspection for diagnostics."""
        return len(self._cache)

    def caps_for(self, path: Path) -> int:
        """Return capability mask currently cached for this file (0 if unknown)."""
        try:
            return self._caps.get(_abs_key(path), 0)
        except Exception:
            return 0


_singleton: Optional[GoBatchParser] = None


def get_go_batch_parser() -> GoBatchParser:
    global _singleton
    if _singleton is None:
        _singleton = GoBatchParser(_default_helper_path())
    return _singleton
