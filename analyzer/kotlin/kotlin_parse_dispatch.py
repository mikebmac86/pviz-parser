from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple

from analyzer.kotlin.parse_kotlin import KotlinParsedFile, parse_kotlin_file


def parse_kotlin_any_file(path: Path, cfg: Any = None) -> Tuple[Optional[KotlinParsedFile], List[str]]:
    p = Path(path)
    if p.suffix.lower() not in {".kt", ".kts"}:
        return None, [f"kotlin_dispatch_unsupported_suffix:{p.suffix.lower()}"]
    try:
        parsed = parse_kotlin_file(p, cfg=cfg)
        warns = list(parsed.problems or [])
        if parsed.error:
            warns.append(parsed.error)
        return parsed, warns
    except Exception as e:
        return None, [f"kotlin_parse_error:{type(e).__name__}:{str(e)[:200]}"]
