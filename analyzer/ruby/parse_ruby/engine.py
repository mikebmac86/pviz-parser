from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from adapters.canonical import repo_rel, to_posix

from .models import RubyAnalysis, RubyAnalysisRequest, ruby_analysis_from_json


class RubyParserUnavailable(RuntimeError):
    """The pviz-ruby-extract CLI could not be located or failed to start."""


class RubyParserError(RuntimeError):
    """The CLI ran but produced invalid or unexpected output."""


def _repo_tool_candidate() -> Optional[Path]:
    here = Path(__file__).resolve()

    exe_name = "pviz-ruby-extract.bat" if os.name == "nt" else "pviz-ruby-extract"

    for parent in here.parents:
        candidates = [
            parent / "tools" / "rubyextract" / "bin" / exe_name,
            parent / "tools" / "rubyextract" / "exe" / exe_name,
            parent / "backend" / "tools" / "rubyextract" / "bin" / exe_name,
            parent / "backend" / "tools" / "rubyextract" / "exe" / exe_name,
        ]

        for p in candidates:
            if p.exists():
                return p

    return None


def find_rubyparser_cli(explicit: Optional[Path] = None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise RubyParserUnavailable(
            f"pviz-ruby-extract not found at explicit path: {p}"
        )

    env = os.environ.get("PVIZ_RUBYPARSER_BIN", "").strip()
    if env:
        p = Path(env)
        if p.exists():
            return p
        raise RubyParserUnavailable(f"PVIZ_RUBYPARSER_BIN not found: {p}")

    exe_name = "pviz-ruby-extract.bat" if os.name == "nt" else "pviz-ruby-extract"
    found = shutil.which(exe_name)
    if found:
        return Path(found)

    cand = _repo_tool_candidate()
    if cand:
        return cand

    raise RubyParserUnavailable(
        "pviz-ruby-extract not found via explicit path, env, PATH, or repo tools fallback"
    )


def _timeout_from_cfg(cfg: object, default: int = 120) -> int:
    try:
        return int(getattr(cfg, "rubyparser_timeout_s", default) or default)
    except Exception:
        return default


def _max_bytes_from_cfg(cfg: object, default: int = 200_000_000) -> int:
    try:
        v = getattr(cfg, "max_bytes_per_file", None)
        if isinstance(v, int) and v > 0:
            return v
    except Exception:
        pass

    try:
        v = getattr(cfg, "max_file_bytes", None)
        if isinstance(v, int) and v > 0:
            return v
    except Exception:
        pass

    return default


def _rel_files(repo_root: Path, files: Sequence[Path]) -> List[str]:
    root = Path(repo_root).resolve()
    out: List[str] = []

    for p in files:
        try:
            ap = Path(p).resolve()
            ap.relative_to(root)
        except Exception:
            continue

        rel = to_posix(repo_rel(ap, root))
        if rel:
            out.append(rel)

    return sorted(set(out))


def build_ruby_analysis_request(
    *,
    repo_root: Path,
    files: Sequence[Path],
    cfg: object,
) -> RubyAnalysisRequest:
    return RubyAnalysisRequest(
        repo_root=Path(repo_root).resolve(),
        files=_rel_files(Path(repo_root).resolve(), files),
        rails_mode=str(getattr(cfg, "rails_mode", "auto") or "auto"),
        include_constant_refs=bool(getattr(cfg, "include_constant_refs", True)),
        include_method_calls=bool(getattr(cfg, "include_method_calls", True)),
        include_rails_dsl=bool(getattr(cfg, "include_rails_dsl", True)),
        include_bundler_index=bool(getattr(cfg, "include_bundler_index", True)),
        include_dynamic_require_facts=bool(
            getattr(cfg, "include_dynamic_require_facts", True)
        ),
        max_bytes_per_file=_max_bytes_from_cfg(cfg),
    )


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


# Schema versions the Ruby CLI is known to emit.  "ruby_analysis@v1" is the
# current production value; "ruby_extract_result@v1" is reserved for a
# potential future rename so we don't hard-fail on a minor version bump.
_KNOWN_SCHEMAS = {"ruby_analysis@v1", "ruby_extract_result@v1"}


def _validate_analysis(
    analysis: RubyAnalysis,
    *,
    requested_files: Iterable[str],
) -> None:
    if analysis.schema_version not in _KNOWN_SCHEMAS:
        raise RubyParserError(
            f"unexpected Ruby analysis schema_version: {analysis.schema_version!r}"
        )

    requested = {str(x).strip() for x in requested_files if str(x).strip()}

    for rel in analysis.files.keys():
        s = str(rel).replace("\\", "/").strip()

        if not s:
            raise RubyParserError("Ruby analysis contained empty file path")

        if s.startswith("/") or s.startswith("../") or "/../" in s:
            raise RubyParserError(
                f"Ruby analysis contained unsafe file path: {rel!r}"
            )

    # Missing files are a soft signal (parser may legitimately skip some), not
    # a hard failure.  Record the count so downstream callers can inspect it.
    missing = requested - set(analysis.files.keys())
    if missing:
        analysis.problems.append(f"missing_requested_files:{len(missing)}")


def run_rubyparser_cli(
    *,
    repo_root: Path,
    files: Sequence[Path],
    cfg: object = None,
    work_dir: Optional[Path] = None,
) -> RubyAnalysis:
    cfg = cfg or object()
    repo_root = Path(repo_root).resolve()

    cli = find_rubyparser_cli(getattr(cfg, "rubyparser_cli_path", None))
    timeout_s = _timeout_from_cfg(cfg)

    request = build_ruby_analysis_request(
        repo_root=repo_root,
        files=files,
        cfg=cfg,
    )

    if not request.files:
        return RubyAnalysis(
            schema_version="ruby_analysis@v1",
            meta={
                "parser": "pviz-ruby-extract",
                "files_in": 0,
                "files_parsed": 0,
                "empty_request": True,
            },
            files={},
            indexes={},
            problems=[],
            raw={},
        )

    tmp_owner = None
    if work_dir is None:
        tmp_owner = tempfile.TemporaryDirectory(prefix="pviz_ruby_")
        work = Path(tmp_owner.name)
    else:
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)

    request_path = work / "ruby_analysis_request.json"
    result_path = work / "ruby_analysis_result.json"

    _write_json(request_path, request.to_json_obj())

    cmd = [
        str(cli),
        "--repo-root",
        str(repo_root),
        "--files-json",
        str(request_path),
        "--out",
        str(result_path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        raise RubyParserUnavailable(
            f"pviz-ruby-extract timed out after {timeout_s}s for {repo_root}"
        ) from e
    except FileNotFoundError as e:
        raise RubyParserUnavailable(
            f"pviz-ruby-extract not executable: {cli}"
        ) from e
    finally:
        # tmp_owner cleanup deferred until after JSON read below.
        pass

    stderr = proc.stderr or ""
    stdout = proc.stdout or ""

    if proc.returncode != 0:
        if tmp_owner is not None:
            tmp_owner.cleanup()
        raise RubyParserUnavailable(
            f"pviz-ruby-extract failed rc={proc.returncode} "
            f"stderr={stderr[:800]} stdout={stdout[:400]}"
        )

    if not result_path.exists():
        if tmp_owner is not None:
            tmp_owner.cleanup()
        raise RubyParserUnavailable(
            f"pviz-ruby-extract did not write result JSON: {result_path}; "
            f"stderr={stderr[:800]}"
        )

    try:
        data = _load_json(result_path)
        if not isinstance(data, dict):
            raise RubyParserError("Ruby parser result JSON was not an object")

        analysis = ruby_analysis_from_json(data)
        _validate_analysis(analysis, requested_files=request.files)
        return analysis

    except RubyParserError:
        raise
    except Exception as e:
        raise RubyParserError(
            f"Failed to parse ruby_analysis result: {type(e).__name__}: {e}"
        ) from e

    finally:
        if tmp_owner is not None:
            tmp_owner.cleanup()


def parse_ruby_analysis(
    *,
    repo_root: Path,
    files: Sequence[Path],
    cfg: object = None,
    work_dir: Optional[Path] = None,
) -> RubyAnalysis:
    return run_rubyparser_cli(
        repo_root=repo_root,
        files=files,
        cfg=cfg,
        work_dir=work_dir,
    )