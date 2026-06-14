from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping

from .utils import node_files_from_nodes, node_ids_from_nodes, normalize_posix


_CODE_EXTS = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".java", ".kt", ".kts", ".rs",
    ".rb", ".rake", ".gemspec", ".ru",
)

def build_discovery(discovery_data: Mapping[str, Any], *, nodes: Mapping[str, Any]) -> Dict[str, Any]:
    meta = discovery_data.get("meta", {}) if isinstance(discovery_data.get("meta"), Mapping) else {}
    files = discovery_data.get("files", [])
    file_items: List[Mapping[str, Any]] = [x for x in files if isinstance(x, Mapping)]

    manifest_paths: List[str] = []
    langs_counter: Counter = Counter()

    for it in file_items:
        rel: str | None = None
        for k in ("rel_posix", "relpath", "rel_path", "path", "file", "rel"):
            v = it.get(k)
            if isinstance(v, str) and v.strip():
                rel = v.strip()
                break
        if rel:
            manifest_paths.append(normalize_posix(rel))

        lang = it.get("lang") or it.get("language") or it.get("kind")
        if isinstance(lang, str) and lang.strip():
            langs_counter[lang.strip().lower()] += 1
        else:
            if isinstance(rel, str):
                low = rel.lower()
                if low.endswith(".py"):
                    langs_counter["python"] += 1
                elif low.endswith((".ts", ".tsx")):
                    langs_counter["ts"] += 1
                elif low.endswith((".js", ".jsx", ".cjs", ".mjs")):
                    langs_counter["js"] += 1
                elif low.endswith(".css"):
                    langs_counter["css"] += 1
                elif low.endswith(".go"):
                    langs_counter["go"] += 1    
                elif low.endswith(".java"):
                    langs_counter["java"] += 1
                elif low.endswith(".rs"):
                    langs_counter["rust"] += 1
                elif low.endswith((".kt", ".kts")):
                    langs_counter["kotlin"] += 1
                elif low.endswith((".rb", ".rake", ".gemspec", ".ru")):
                    langs_counter["ruby"] += 1
                else:
                    langs_counter["other"] += 1

    manifest_set = set(manifest_paths)

    node_ids = node_ids_from_nodes(nodes)
    node_files = node_files_from_nodes(nodes)

    # --- Bundled language breakdown (from nodes) ---
    bundled_langs: Counter = Counter()

    for _nid, n in (nodes or {}).items():
        if not isinstance(n, Mapping):
            continue
        lang = n.get("lang")
        if isinstance(lang, str) and lang.strip():
            bundled_langs[lang.strip().lower()] += 1
        else:
            # fallback: infer from file path if present
            f = n.get("file") or n.get("node_id") or _nid
            if isinstance(f, str):
                low = f.lower()
                if low.endswith(".py"):
                    bundled_langs["python"] += 1
                elif low.endswith(".css"):
                    bundled_langs["css"] += 1
                elif low.endswith((".js", ".jsx")):
                    bundled_langs["js"] += 1
                elif low.endswith((".ts", ".tsx")):
                    bundled_langs["ts"] += 1
                elif low.endswith(".go"):
                    bundled_langs["go"] += 1    
                elif low.endswith(".java"):
                    bundled_langs["java"] += 1
                elif low.endswith(".rs"):
                    bundled_langs["rust"] += 1
                elif low.endswith((".kt", ".kts")):
                    bundled_langs["kotlin"] += 1
                elif low.endswith((".rb", ".rake", ".gemspec", ".ru")):
                    bundled_langs["ruby"] += 1
                else:
                    bundled_langs["other"] += 1

    bundled_languages = sorted(bundled_langs.keys())

    missing_by_node_id = sorted(manifest_set - node_ids)
    missing_by_file = sorted(manifest_set - node_files)

    missing = missing_by_file if len(missing_by_file) <= len(missing_by_node_id) else missing_by_node_id
    discovered_total = meta.get("total_files") if isinstance(meta.get("total_files"), int) else len(manifest_set)

    return {
        "meta": {
            "created_utc": meta.get("created_utc") or meta.get("created_at"),
            "scan_root": meta.get("scan_root"),
            "total_files": meta.get("total_files"),
        },
        "counts": {
            "discovered_total_files": discovered_total,
            "discovered_by_lang": dict(langs_counter),
            "bundled_nodes": len(node_ids),
            "bundled_by_lang": dict(bundled_langs),
            "bundled_languages": bundled_languages,
            "missing_files_count": len(missing),
            "missing_by_node_id_count": len(missing_by_node_id),
            "missing_by_file_count": len(missing_by_file),
        },
    }