from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from ..build_context import BuildContext


@dataclass
class AnalyzerBuildResult:
    """
    Result of running the analyzer build pipeline.

    - norm: normalized merged representation (usually {"meta":..., "nodes":..., "edges":...})
    - artifacts_for_mode: the artifact directory that the UI / downstream mode should use
    - art: raw artifact-ish object returned by classic build (if any)
    - discovery_manifest_path: path to discovery_manifest.json if used
    - discovery_manifest_summary: small summary dict for logging/diagnostics
    """
    norm: Optional[Dict[str, Any]]
    artifacts_for_mode: Optional[Path]
    art: Any = None
    discovery_manifest_path: Optional[Path] = None
    discovery_manifest_summary: Optional[Dict[str, Any]] = None


class InitArtifactsCtxFn(Protocol):
    def __call__(
        self,
        *,
        ctx: BuildContext,
        store_root: Path,
        scan_root: Path,
        allow_output_in_repo: bool,
        output_path: Optional[Path],
        clean: bool,
    ) -> Optional[BuildContext]:
        ...


class AttachWorkspaceDirFn(Protocol):
    def __call__(self, *, ctx: BuildContext, store_root: Path) -> Optional[BuildContext]:
        ...


class FollowUpAnalyzerFn(Protocol):
    def __call__(self, *, ctx: BuildContext) -> None:
        ...


class RunBucketAnalyzersFn(Protocol):
    def __call__(
        self,
        *,
        scan_root: Path,
        store_root: Path,
        artifacts_dir: Path,
        cfg_like: Any,
        manifest_path: Optional[Path],
    ) -> Optional[Dict[str, Any]]:
        ...


class PrepareZonesModeFn(Protocol):
    def __call__(self, *, ctx: BuildContext) -> Optional[Path]:
        ...


class CleanupTempFn(Protocol):
    def __call__(self, *, tmp_root: Path) -> None:
        ...


class BuildZonesFn(Protocol):
    def __call__(self, *, ctx: BuildContext, artifacts_dir: Path) -> Any:
        ...


class RunClassicBuildFn(Protocol):
    def __call__(self, *, ctx: BuildContext) -> Any:
        ...


class ZonesLoggerFn(Protocol):
    def __call__(self, *, ctx: BuildContext, zones_art: Any) -> None:
        ...
