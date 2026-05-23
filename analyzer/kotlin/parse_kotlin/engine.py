from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from .models import KotlinParsedFile, kotlin_parsed_file_from_json


class KotlinParserUnavailable(RuntimeError):
    pass


def _repo_tool_candidate() -> Optional[Path]:
    here = Path(__file__).resolve()
    for parent in here.parents:
        tools = parent / "tools" / "kotlinparser_cli" / "build" / "install" / "kotlinparser_cli" / "bin"
        p = tools / ("kotlinparser_cli.bat" if os.name == "nt" else "kotlinparser_cli")
        if p.exists():
            return p
    return None


def find_kotlinparser_cli(explicit: Optional[Path] = None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise KotlinParserUnavailable(f"kotlinparser_cli not found at explicit path: {p}")

    env = os.environ.get("PVIZ_KOTLINPARSER_BIN", "").strip()
    if env:
        p = Path(env)
        if p.exists():
            return p
        raise KotlinParserUnavailable(f"PVIZ_KOTLINPARSER_BIN not found: {p}")

    found = shutil.which("kotlinparser_cli.bat" if os.name == "nt" else "kotlinparser_cli")
    if found:
        return Path(found)

    cand = _repo_tool_candidate()
    if cand:
        return cand

    raise KotlinParserUnavailable("kotlinparser_cli not found via explicit path, env, PATH, or repo tools fallback")


def _timeout_from_cfg(cfg: object, default: int = 120) -> int:
    try:
        return int(getattr(cfg, "kotlinparser_timeout_s", default) or default)
    except Exception:
        return default


def run_kotlinparser_cli(path: Path, *, cfg: object = None) -> str:
    cli = find_kotlinparser_cli(getattr(cfg, "kotlinparser_cli_path", None) if cfg else None)
    timeout_s = _timeout_from_cfg(cfg)

    try:
        p = subprocess.run(
            [str(cli), str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        raise KotlinParserUnavailable(f"kotlinparser_cli timed out after {timeout_s}s for {path}") from e
    except FileNotFoundError as e:
        raise KotlinParserUnavailable(f"kotlinparser_cli not executable: {cli}") from e

    out = p.stdout or ""
    err = p.stderr or ""
    if not out.strip():
        raise KotlinParserUnavailable(f"kotlinparser_cli produced no stdout rc={p.returncode} stderr={err[:500]}")
    return out


def parse_kotlin_path(path: Path, *, cfg: object = None) -> List[KotlinParsedFile]:
    raw = run_kotlinparser_cli(Path(path), cfg=cfg)
    out: List[KotlinParsedFile] = []

    for lineno, line in enumerate(raw.splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                out.append(kotlin_parsed_file_from_json(data))
        except Exception as e:
            out.append(KotlinParsedFile(ok=False, parse_status="error", file_path=Path(str(path)), error=f"jsonl_decode_error:line={lineno}:{type(e).__name__}:{str(e)[:200]}"))
    return out


def parse_kotlin_file(path: Path, *, cfg: object = None) -> KotlinParsedFile:
    parsed = parse_kotlin_path(Path(path), cfg=cfg)
    if parsed:
        return parsed[0]
    return KotlinParsedFile(ok=False, parse_status="error", file_path=Path(path), error="kotlinparser_cli returned no parse records")
