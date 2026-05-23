from __future__ import annotations

from typing import Any, Mapping, Sequence

from .config import LANGUAGE_SPECS, LanguageSpec


def merge_folder_index_default_from_list(folder_indexes: Sequence[Any]) -> Any:
    """
    Merge folder indexes in deterministic language order.

    The first valid folder index becomes the base. Later folder indexes add
    missing files. This preserves base metadata while avoiding language loss.
    """
    base = next((fi for fi in folder_indexes if isinstance(fi, dict)), None)
    if base is None:
        return next((fi for fi in reversed(folder_indexes) if fi is not None), None)

    out = dict(base)

    if "files" in out and isinstance(out["files"], dict):
        out["files"] = dict(out["files"])
    elif "files" in out and isinstance(out["files"], list):
        out["files"] = list(out["files"])

    def _merge_files_into(out_obj: dict, src_fi: Any) -> None:
        if not isinstance(src_fi, dict):
            return

        src_files = src_fi.get("files")
        if src_files is None:
            return

        dst_files = out_obj.get("files")

        if isinstance(dst_files, dict) and isinstance(src_files, dict):
            for k, v in src_files.items():
                if k not in dst_files:
                    dst_files[k] = v
            out_obj["files"] = dst_files
            return

        if isinstance(dst_files, list) and isinstance(src_files, list):
            out_obj["files"] = [*dst_files, *src_files]
            return

        if dst_files is None:
            out_obj["files"] = dict(src_files) if isinstance(src_files, dict) else list(src_files) if isinstance(src_files, list) else src_files

    for fi in folder_indexes:
        if fi is base:
            continue
        _merge_files_into(out, fi)

    return out


def _file_count(fi: Any) -> int:
    if not isinstance(fi, dict):
        return 0
    files = fi.get("files")
    if isinstance(files, dict):
        return len(files)
    if isinstance(files, list):
        return len(files)
    return 0


def _is_plausible_folder_index(fi: Any) -> bool:
    return isinstance(fi, dict) and _file_count(fi) > 0


def merge_folder_indexes(
    *,
    folder_indexes_by_lang: Mapping[str, Any],
    merge_folder_indexes_fn: Any,
    specs: Sequence[LanguageSpec] = LANGUAGE_SPECS,
) -> Any:
    folder_indexes = [folder_indexes_by_lang.get(spec.lang) for spec in specs]

    if not any(fi is not None for fi in folder_indexes):
        return None

    raw_default = merge_folder_index_default_from_list(folder_indexes)

    merged_fi = None

    if callable(merge_folder_indexes_fn):
        # Preferred: explicit keyword adapter for known bucket-style signature.
        try:
            merged_fi = merge_folder_indexes_fn(
                py_fi=folder_indexes_by_lang.get("python"),
                ts_fi=folder_indexes_by_lang.get("ts"),
                go_fi=folder_indexes_by_lang.get("go"),
                java_fi=folder_indexes_by_lang.get("java"),
                kotlin_fi=folder_indexes_by_lang.get("kotlin"),
                rust_fi=folder_indexes_by_lang.get("rust"),
            )
        except TypeError:
            merged_fi = None
        except Exception:
            merged_fi = None

        # Fallback: positional language order.
        if merged_fi is None:
            for n in range(len(folder_indexes), 1, -1):
                try:
                    merged_fi = merge_folder_indexes_fn(*folder_indexes[:n])
                    break
                except TypeError:
                    continue
                except Exception:
                    merged_fi = None
                    break

    if not _is_plausible_folder_index(merged_fi):
        return raw_default

    # Use callback result, but always backfill from raw language indexes.
    # Put raw_default first if it has more files to prevent a partial callback
    # result from becoming the effective base.
    if _file_count(raw_default) > _file_count(merged_fi):
        return merge_folder_index_default_from_list([raw_default, merged_fi, *folder_indexes])

    return merge_folder_index_default_from_list([merged_fi, raw_default, *folder_indexes])