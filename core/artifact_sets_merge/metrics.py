from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .config import _CODE_EXTS, EXT_TO_LANG
from .io import safe_int


def compute_code_metrics_summary_from_folder_index(fi: Dict[str, Any]) -> Dict[str, Any]:
    files = fi.get("files")
    if not isinstance(files, dict):
        return {
            "loc_code": 0,
            "sloc_code": 0,
            "comment_lines_code": 0,
            "blank_lines_code": 0,
            "comment_pct_code": None,
            "metrics_code_files": 0,
            "metrics_missing_files": 0,
            "loc_by_lang": {},
            "sloc_by_lang": {},
            "comment_lines_by_lang": {},
            "blank_lines_by_lang": {},
            "files_by_lang": {},
        }

    loc_total = 0
    sloc_total = 0
    comment_total = 0
    blank_total = 0
    files_count = 0
    missing_count = 0

    loc_by_lang: Dict[str, int] = {}
    sloc_by_lang: Dict[str, int] = {}
    comment_by_lang: Dict[str, int] = {}
    blank_by_lang: Dict[str, int] = {}
    files_by_lang: Dict[str, int] = {}

    for f in files.values():
        if not isinstance(f, dict):
            continue
        if f.get("eligible") is False:
            continue

        ps = f.get("parse_status")
        if isinstance(ps, str) and ps.strip().lower() == "skipped":
            continue

        path = f.get("file") or f.get("rel_posix") or f.get("path")
        if not isinstance(path, str):
            continue

        ext = Path(path).suffix.lower()
        if ext not in _CODE_EXTS:
            continue

        try:
            loc = int(f.get("loc")) if f.get("loc") is not None else None
            sloc = int(f.get("sloc")) if f.get("sloc") is not None else None
            comments = int(f.get("comment_lines")) if f.get("comment_lines") is not None else None
            blanks = int(f.get("blank_lines")) if f.get("blank_lines") is not None else None
        except (TypeError, ValueError):
            missing_count += 1
            continue

        if loc is None or sloc is None:
            missing_count += 1
            continue

        loc = max(loc, 0)
        sloc = max(sloc, 0)
        comments = max(comments if comments is not None else 0, 0)
        blanks = max(blanks if blanks is not None else 0, 0)
        lang = EXT_TO_LANG.get(ext, ext.lstrip(".") or "unknown")

        loc_total += loc
        sloc_total += sloc
        comment_total += comments
        blank_total += blanks
        files_count += 1

        loc_by_lang[lang] = loc_by_lang.get(lang, 0) + loc
        sloc_by_lang[lang] = sloc_by_lang.get(lang, 0) + sloc
        comment_by_lang[lang] = comment_by_lang.get(lang, 0) + comments
        blank_by_lang[lang] = blank_by_lang.get(lang, 0) + blanks
        files_by_lang[lang] = files_by_lang.get(lang, 0) + 1

    return {
        "loc_code": loc_total,
        "sloc_code": sloc_total,
        "comment_lines_code": comment_total,
        "blank_lines_code": blank_total,
        "comment_pct_code": (comment_total / loc_total) if loc_total else None,
        "metrics_code_files": files_count,
        "metrics_missing_files": missing_count,
        "loc_by_lang": loc_by_lang,
        "sloc_by_lang": sloc_by_lang,
        "comment_lines_by_lang": comment_by_lang,
        "blank_lines_by_lang": blank_by_lang,
        "files_by_lang": files_by_lang,
    }


def metrics_int(metrics_summary: Optional[Mapping[str, Any]], key: str, default: int = 0) -> int:
    if not metrics_summary:
        return default
    return safe_int(metrics_summary.get(key), default)


def metrics_val(metrics_summary: Optional[Mapping[str, Any]], key: str, default: Any = None) -> Any:
    if not metrics_summary:
        return default
    return metrics_summary.get(key, default)