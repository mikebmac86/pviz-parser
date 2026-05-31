from __future__ import annotations

import json
import argparse
from pathlib import Path
import shutil

from analyzer.config import AnalyzerCfg
from core.json_export import build_llm_bundle_headless
from core.store_root import default_store_root


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _count_container(x) -> int:
    """
    Count nodes/edges in a tolerant way.
    Supports dict, list/tuple, or None.
    """
    if x is None:
        return 0
    if isinstance(x, dict):
        return len(x)
    if isinstance(x, (list, tuple)):
        return len(x)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="pviz",
        description="PViz headless analyzer: generate LLM bundle JSON from analyzer artifacts.",
    )

    p.add_argument("scan_root", type=Path, help="Root of project to analyze.")
    p.add_argument(
        "-o",
        "--output",
        required=True,
        type=Path,
        help="Output LLM bundle JSON file.",
    )
    p.add_argument(
        "--mode",
        default="zones",
        choices=["classic", "zones"],
        help="Build mode for analyzer + bundle (default: zones).",
    )
    p.add_argument(
        "--max-bytes",
        type=int,
        default=100_000_000,
        help="Max bytes per file (AnalyzerCfg.max_file_bytes).",
    )
    p.add_argument(
        "--store-root",
        type=Path,
        help="Override sandbox directory (default: per-user pviz store)",
    )
    p.add_argument(
        "--allow-output-in-repo",
        action="store_true",
        help="Allow writing the bundle output inside scan_root (default: disabled).",
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="FULL NUKE: delete the entire sandbox artifacts dir before running "
        "(prevents stale cross-repo reuse).",
    )
    args = p.parse_args(argv)

    scan_root = args.scan_root.expanduser().resolve()
    if not scan_root.exists() or not scan_root.is_dir():
        print(f"pviz: scan_root is not a directory: {scan_root}")
        return 2

    # Ensure .json suffix on output
    out_path = args.output.expanduser().resolve()
    if out_path.suffix.lower() != ".json":
        out_path = out_path.with_suffix(".json")

    # Guard: don't write into the scanned repo unless explicitly allowed
    if _is_within(out_path, scan_root) and not args.allow_output_in_repo:
        print(
            "pviz: refusing to write bundle inside scan_root:\n"
            f"  scan_root: {scan_root}\n"
            f"  output:    {out_path}\n"
            "Use --allow-output-in-repo if you really intend to write into the repo."
        )
        return 2

    store_root = (
        args.store_root.expanduser().resolve()
        if args.store_root
        else default_store_root()
    )
    store_root.mkdir(parents=True, exist_ok=True)

    cfg = AnalyzerCfg(max_file_bytes=args.max_bytes)
    artifacts_root = store_root / ".pviz" / "artifacts"

    if args.clean:
        # FULL NUKE: delete *everything* under artifacts_root.
        # This guarantees we don't reuse stale:
        # - discovery_manifest.json or discovery_manifest@v1.json
        # - sets/classic/*, analyzers/*, zones outputs, tmp dirs, etc.
        try:
            if artifacts_root.exists():
                shutil.rmtree(artifacts_root, ignore_errors=True)
        except Exception:
            pass
        # Recreate the directory so downstream code can assume it exists.
        artifacts_root.mkdir(parents=True, exist_ok=True)

    try:
        bundle_path, compressed_path, result = build_llm_bundle_headless(
            scan_root=scan_root,
            store_root=store_root,
            cfg=cfg,
            files=[],  # discovery handled internally
            home_id=scan_root.name,
            bundle_output=out_path,
            mode=args.mode,
            use_bucket_analyzers=(args.mode == "zones"),
        )
    except Exception as e:
        print(f"pviz: build failed: {type(e).__name__}: {e}")
        return 1

    # ---- Summary ----------------------------------------------------------
    print(f"Wrote standard format: {bundle_path}")

    # Show compressed format info if generated
    if compressed_path and compressed_path.exists():
        standard_size = bundle_path.stat().st_size
        compressed_size = compressed_path.stat().st_size
        savings_pct = (1 - compressed_size / standard_size) * 100
        print(f"Wrote compressed format: {compressed_path}")
        print(
            f"   Compression: {standard_size:,} -> {compressed_size:,} bytes ({savings_pct:.0f}% smaller)"
        )

    bundle_obj: dict | None = None
    norm = result.norm if isinstance(getattr(result, "norm", None), dict) else {}

    try:
        bundle_obj = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
        nodes = _count_container(bundle_obj.get("nodes"))
        edges = _count_container(bundle_obj.get("edges"))
        print(f"Nodes: {nodes}, Edges: {edges}")
    except Exception:
        nodes = _count_container(norm.get("nodes"))
        edges = _count_container(norm.get("edges"))
        print(f"Nodes: {nodes}, Edges: {edges}")

    # ---- FolderIndex / inclusion (optional) -------------------------------
    meta = None
    if isinstance(bundle_obj, dict):
        meta = bundle_obj.get("meta")
    elif isinstance(norm, dict):
        meta = norm.get("meta")

    if isinstance(meta, dict):
        included = meta.get("included_count")
        eligible = meta.get("eligible_count")
        if included is not None or eligible is not None:
            print("FolderIndex:")
            if eligible is not None:
                print(f"  Eligible files: {eligible}")
            if included is not None:
                print(f"  Included in analysis: {included}")

    # ---- Discovery manifest (optional, language-agnostic) -----------------
    if result.discovery_manifest_path:
        print(f"Discovery manifest: {result.discovery_manifest_path}")
        summary = result.discovery_manifest_summary or {}
        by_lang = summary.get("by_lang")
        total = summary.get("total_files")

        if total is not None:
            print(f"  Total files discovered: {total}")
        if isinstance(by_lang, dict) and by_lang:
            print("  Files by language:")
            for lang, count in sorted(by_lang.items()):
                print(f"    {lang}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
