# analyzer/module_map.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Tuple
from analyzer.config import AnalyzerCfg
from analyzer.fs import detect_src_roots, contiguous_pkg_chain
from adapters.canonical import normalize_root as norm_root_SSOT
@dataclass(frozen=True)
class _Roots:
    repo_root: Path
    src_roots: Tuple[Path, ...]



def _normalize_root(root: Path | str) -> Path:
    """
    Make sure the provided root is a directory-like 'scan root' (read-only):
      - expanduser, resolve (non-strict)
      - if points to a file, use its parent
    Delegates to adapters.canonical.normalize_root (SSOT).
    """
    p = norm_root_SSOT(root)
    if p is not None:
        return p
    # Conservative fallback (shouldn't normally run)
    r = Path(root).expanduser()
    try:
        r = r.resolve(strict=False)
    except Exception:
        pass
    return r if r.is_dir() else r.parent


class ModuleMap:
    """
    Bidirectional mapping policy between file paths and dotted module names.

    Key behaviors (cfg-driven):
      - src layout dirs (e.g., "src/") are treated as candidate package roots.
      - If allow_namespace_pkgs=True, directories without __init__.py can still
        count towards module names (PEP 420).
      - If honor_dunder_init=True, '__init__.py' collapses to its package name.

    Fast-path caching keeps lookups O(1) after first hit.
    """

    def __init__(self, repo_root: Path, cfg: AnalyzerCfg) -> None:
        self._cfg = cfg
        repo_root = _normalize_root(repo_root)

        # Use fs helper instead of hand-rolling src_roots
        self._roots = _Roots(
            repo_root=repo_root,
            src_roots=detect_src_roots(repo_root, cfg),
        )

        # Caches
        self._path_to_module: Dict[Path, str] = {}
        self._module_to_path: Dict[str, Path] = {}

    # -----------------------
    # public API
    # -----------------------

    @property
    def repo_root(self) -> Path:
        return self._roots.repo_root

    def module_name_for_path(self, file_path: Path) -> Optional[str]:
        """
        Convert a Python file path to a dotted module name.
        Honors src layout and namespace package policy.
        Returns None for non-.py files or if file is outside roots.
        """
        file_path = Path(file_path).resolve(strict=False)

        # Require .py (case-insensitive)
        if file_path.suffix.lower() != ".py":
            return None

        cached = self._path_to_module.get(file_path)
        if cached:
            return cached

        anchor = self._choose_anchor(file_path)
        if anchor is None:
            return None

        rel = file_path.relative_to(anchor)
        parts = list(rel.parts)
        if not parts:
            return None

        # strip suffix
        parts[-1] = Path(parts[-1]).stem

        # collapse __init__ to package (if configured)
        if self._cfg.honor_dunder_init and parts and parts[-1] == "__init__":
            parts = parts[:-1]

        # enforce package chain rules if not allowing namespace pkgs
        if not self._cfg.allow_namespace_pkgs:
            # require contiguous chain of __init__.py from anchor to parent
            if not contiguous_pkg_chain(anchor, file_path.parent): 
                return None

        dotted = ".".join([p for p in parts if p])
        if not dotted:
            return None

        self._path_to_module[file_path] = dotted
        # only cache module->path for non-colliding names
        self._module_to_path.setdefault(dotted, file_path)
        return dotted

    def path_for_module(self, module: str) -> Optional[Path]:
        """
        Best-effort physical path for a dotted module. Tries file, then package.
        """
        module = (module or "").strip(".")
        if not module:
            return None

        cached = self._module_to_path.get(module)
        if cached is not None:
            return cached

        # search both repo root and src roots
        for root in (self._roots.src_roots + (self._roots.repo_root,)):
            # module -> file
            f = root.joinpath(*module.split("."))
            file_candidate = (f.with_suffix(".py")).resolve(strict=False)
            if file_candidate.exists():
                self._module_to_path[module] = file_candidate
                self._path_to_module.setdefault(file_candidate, module)
                return file_candidate

            # module -> package/__init__.py
            pkg_init = (f / "__init__.py").resolve(strict=False)
            if pkg_init.exists():
                self._module_to_path[module] = pkg_init
                # note: if honor_dunder_init, module name is the package itself
                self._path_to_module.setdefault(pkg_init, module)
                return pkg_init

        return None

    # -----------------------
    # helpers (file-private)
    # -----------------------

    def _choose_anchor(self, file_path: Path) -> Optional[Path]:
        """
        Select the nearest plausible root for 'file_path':
          - prefer src_roots if file is under one;
          - else fall back to repo_root if within it;
          - else None.
        """
        for s in self._roots.src_roots:
            try:
                file_path.relative_to(s)
                return s
            except Exception:
                pass
        try:
            file_path.relative_to(self._roots.repo_root)
            return self._roots.repo_root
        except Exception:
            return None
