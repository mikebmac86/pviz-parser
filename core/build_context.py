# core/build_context.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List

try:
    from analyzer.config import AnalyzerCfg
except Exception:  # pragma: no cover - keep type hints flexible if import fails
    AnalyzerCfg = Any  # type: ignore[misc]


@dataclass
class BuildContext:
    """
    UI-agnostic description of a single PViz build.

    This bundles all of the analyzer- and artifact-related inputs that the
    build pipeline needs, without depending on Qt or MainWindow:

      • scan_root:     the codebase root being analyzed
      • store_root:    sandboxed store root (e.g. <root>/.pviz_store)
      • artifacts_dir: concrete artifacts directory for this build/mode
      • cfg:           analyzer configuration (AnalyzerCfg or compatible)
      • mode:          requested build mode ("classic", "zones", "llm_json", ...)
      • eff_mode:      effective mode after any auto-tweaks
      • files:         resolved Python files to analyze
      • home_id:       canonical "start file" / home node id
      • bus:           event/log bus used by analyzer / builders

    The UI layer (entry_main/on_build_artifacts) should be responsible for
    constructing this from window/workspace state; the core build pipeline
    should operate only on BuildContext.
    """

    scan_root: Path
    store_root: Path
    artifacts_dir: Path

    cfg: AnalyzerCfg  # or compatible "cfg-like" structure
    mode: str
    eff_mode: str

    files: List[str]
    home_id: str

    bus: Any

    @property
    def is_zones(self) -> bool:
        return self.mode == "zones"

    @property
    def is_llm_json(self) -> bool:
        return self.mode == "llm_json"

    @property
    def mode_or_classic(self) -> str:
        return self.mode or "classic"


def make_build_context(
    *,
    scan_root: Path,
    store_root: Path,
    artifacts_dir: Path,
    cfg: AnalyzerCfg,
    mode: str,
    eff_mode: str,
    files: Iterable[str],
    home_id: str,
    bus: Any,
) -> BuildContext:
    """
    Convenience constructor that normalizes iterable inputs (e.g. files).

    This is intentionally free of any Qt/MainWindow dependency so that it
    can be used both by the desktop UI and a future headless/SaaS backend.
    """
    return BuildContext(
        scan_root=scan_root,
        store_root=store_root,
        artifacts_dir=artifacts_dir,
        cfg=cfg,
        mode=mode,
        eff_mode=eff_mode,
        files=list(files),
        home_id=home_id,
        bus=bus,
    )
