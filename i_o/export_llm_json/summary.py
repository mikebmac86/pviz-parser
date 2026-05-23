from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .utils import edge_counts_from_edges


def build_summary(
    *,
    meta: Mapping[str, Any],
    nodes: Mapping[str, Any],
    edges: List[Mapping[str, Any]],
    zones: List[Mapping[str, Any]],
    folders: Optional[Mapping[str, Any]],
    node_order: List[str],
    import_summary: Optional[Dict[str, Any]] = None,
    nodefacts_meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    node_count = len(nodes or {})
    edge_count = len(edges or [])
    zone_count = len(zones or [])
    node_order_count = len(node_order or [])

    loc_vals: List[int] = []
    sloc_vals: List[int] = []
    comment_vals: List[int] = []
    blank_vals: List[int] = []
    parse_counts: Dict[str, int] = {}
    cycle_nodes = 0
    scc_sizes: List[int] = []

    for _nid, n in (nodes or {}).items():
        if not isinstance(n, Mapping):
            continue

        vloc = n.get("loc")
        if isinstance(vloc, int):
            loc_vals.append(vloc)

        vsloc = n.get("sloc")
        if isinstance(vsloc, int):
            sloc_vals.append(vsloc)

        vc = n.get("comment_lines")
        if isinstance(vc, int):
            comment_vals.append(vc)

        vb = n.get("blank_lines")
        if isinstance(vb, int):
            blank_vals.append(vb)

        ps = n.get("parse_status")
        if isinstance(ps, str) and ps:
            parse_counts[ps] = parse_counts.get(ps, 0) + 1

        sz = n.get("scc_size")
        if isinstance(sz, int) and sz > 1:
            cycle_nodes += 1
            scc_sizes.append(sz)

    total_loc = meta.get("loc_code") if meta else (sum(loc_vals) if loc_vals else None)
    total_sloc = meta.get("sloc_code") if meta else (sum(sloc_vals) if sloc_vals else None)
    total_comment_lines = meta.get("comment_lines_code") if meta else (sum(comment_vals) if comment_vals else None)
    total_blank_lines = meta.get("blank_lines_code") if meta else (sum(blank_vals) if blank_vals else None)
    comment_pct_code = meta.get("comment_pct_code") if meta else None

    top_loc: List[Dict[str, Any]] = []
    if nodes:
        tmp: List[Tuple[int, str, Any, Any]] = []
        for nid, n in nodes.items():
            if not isinstance(n, Mapping):
                continue
            vloc = n.get("loc")
            if isinstance(vloc, int):
                tmp.append((vloc, nid, n.get("file"), n.get("name")))
        tmp.sort(reverse=True, key=lambda t: t[0])
        top_loc = [
            {"loc": v, "node_id": nid, "file": f, "name": nm}
            for (v, nid, f, nm) in tmp[:15]
        ]

    edge_kind_counts: Dict[str, int] = {}
    for e in edges or []:
        if not isinstance(e, Mapping):
            continue
        k = e.get("kind") or "import"
        if isinstance(k, str) and k:
            edge_kind_counts[k] = edge_kind_counts.get(k, 0) + 1

    out_by_src, in_by_dst = edge_counts_from_edges(edges)

    def _node_label(nid: str) -> Dict[str, Any]:
        nn = nodes.get(nid)
        if isinstance(nn, Mapping):
            return {"file": nn.get("file"), "name": nn.get("name")}
        return {"file": None, "name": None}

    top_imported = [
        {"node_id": nid, "importers": int(c), **_node_label(nid)}
        for nid, c in in_by_dst.most_common(15)
    ]
    top_dependencies = [
        {"node_id": nid, "deps": int(c), **_node_label(nid)}
        for nid, c in out_by_src.most_common(15)
    ]

    zone_sizes: List[Tuple[int, Any, Any, Any, Any]] = []
    node_to_zone: Dict[str, str] = {}

    for z in zones or []:
        if not isinstance(z, Mapping):
            continue
        zid = z.get("zone_id")
        members = z.get("member_files") or []
        if isinstance(zid, str) and zid and isinstance(members, list):
            valid_members = [m for m in members if isinstance(m, str)]
            for m in valid_members:
                node_to_zone.setdefault(m, zid)
            zone_sizes.append((len(valid_members), zid, z.get("package"), z.get("kind"), z.get("level")))

    zone_sizes.sort(reverse=True, key=lambda t: t[0])
    largest_zones = [
        {"members": n, "zone_id": zid, "package": pkg, "kind": kind, "level": lvl}
        for (n, zid, pkg, kind, lvl) in zone_sizes[:15]
        if isinstance(zid, str) and zid
    ]

    cross_zone_edges_top: List[Dict[str, Any]] = []
    if node_to_zone:
        cross = Counter()
        for e in edges or []:
            if not isinstance(e, Mapping):
                continue
            s = e.get("src")
            d = e.get("dst")
            if not (isinstance(s, str) and isinstance(d, str)):
                continue
            zs = node_to_zone.get(s)
            zd = node_to_zone.get(d)
            if zs and zd and zs != zd:
                cross[(zs, zd)] += 1
        cross_zone_edges_top = [
            {"from": a, "to": b, "count": int(c)}
            for (a, b), c in cross.most_common(15)
        ]

    total_loc_folder = 0
    total_sloc_folder = 0
    total_comment_folder = 0
    total_blank_folder = 0
    loc_seen = False
    sloc_seen = False
    comment_seen = False
    blank_seen = False
    import_style_totals: Dict[str, int] = {}
    parse_counts_folder: Dict[str, int] = {}
    file_count: Optional[int] = None

    if folders and isinstance(folders, Mapping):
        files = folders.get("files")
        if isinstance(files, Mapping):
            file_count = len(files)
            for _fid, fe in files.items():
                if not isinstance(fe, Mapping):
                    continue

                vloc = fe.get("loc")
                if isinstance(vloc, int) and vloc >= 0:
                    total_loc_folder += vloc
                    loc_seen = True

                vsloc = fe.get("sloc")
                if isinstance(vsloc, int) and vsloc >= 0:
                    total_sloc_folder += vsloc
                    sloc_seen = True

                vc = fe.get("comment_lines")
                if isinstance(vc, int) and vc >= 0:
                    total_comment_folder += vc
                    comment_seen = True

                vb = fe.get("blank_lines")
                if isinstance(vb, int) and vb >= 0:
                    total_blank_folder += vb
                    blank_seen = True

                ps = fe.get("parse_status")
                if isinstance(ps, str) and ps:
                    parse_counts_folder[ps] = parse_counts_folder.get(ps, 0) + 1

                sc = fe.get("import_style_counts")
                if isinstance(sc, Mapping):
                    for k, v in sc.items():
                        if isinstance(k, str) and isinstance(v, int):
                            import_style_totals[k] = import_style_totals.get(k, 0) + v

    parse_status_summary = dict(parse_counts)
    for k, v in parse_counts_folder.items():
        if k not in parse_status_summary:
            parse_status_summary[k] = v

    comment_summary: Dict[str, Any] = {}
    if loc_seen:
        if comment_seen:
            comment_ratio = (total_comment_folder / total_loc_folder) if total_loc_folder > 0 else 0.0
            comment_summary = {
                "folder_total_loc": total_loc_folder,
                "folder_total_sloc": total_sloc_folder if sloc_seen else None,
                "folder_comment_lines": total_comment_folder,
                "folder_blank_lines": total_blank_folder if blank_seen else None,
                "folder_comment_ratio": round(comment_ratio, 4),
            }
        else:
            comment_summary = {
                "folder_total_loc": total_loc_folder,
                "folder_total_sloc": total_sloc_folder if sloc_seen else None,
                "folder_comment_lines": None,
                "folder_blank_lines": total_blank_folder if blank_seen else None,
                "folder_comment_ratio": None,
                "note": "comment_lines missing from FolderIndex files; cannot compute true comment ratio",
            }

    crosstalk_fields = {
        "crosstalk_candidates_py_v1": "python",
        "crosstalk_candidates_ts_v1": "typescript",
        "crosstalk_candidates_js_v1": "javascript",
        "crosstalk_candidates_go_v1": "go",
        "crosstalk_candidates_java_v1": "java",
        "crosstalk_candidates_rust_v1": "rust",
    }

    crosstalk_summary: Dict[str, Any] = {}
    files_with_crosstalk: Dict[str, int] = {}
    total_references: Dict[str, int] = {}
    crosstalk_by_kind: Dict[str, Dict[str, int]] = {}

    for _nid, n in (nodes or {}).items():
        if not isinstance(n, Mapping):
            continue

        for field_name, lang in crosstalk_fields.items():
            ct_data = n.get(field_name)
            if not ct_data:
                continue

            files_with_crosstalk[lang] = files_with_crosstalk.get(lang, 0) + 1

            if isinstance(ct_data, Mapping):
                rows = ct_data.get("rows", [])
                if isinstance(rows, list):
                    total_references[lang] = total_references.get(lang, 0) + len(rows)

                    legend = ct_data.get("legend", {})
                    schema = ct_data.get("schema", [])

                    kind_idx = None
                    for i, field in enumerate(schema):
                        if legend.get(field) == "kind" or field == "k":
                            kind_idx = i
                            break

                    if kind_idx is not None:
                        for row in rows:
                            if isinstance(row, list) and len(row) > kind_idx:
                                kind = row[kind_idx]
                                if isinstance(kind, str):
                                    crosstalk_by_kind.setdefault(lang, {})
                                    crosstalk_by_kind[lang][kind] = (
                                        crosstalk_by_kind[lang].get(kind, 0) + 1
                                    )

    if files_with_crosstalk or total_references:
        crosstalk_summary = {
            "files_with_candidates": files_with_crosstalk,
            "total_references": total_references,
        }
        if crosstalk_by_kind:
            crosstalk_summary["by_kind"] = crosstalk_by_kind

    api_surface: Dict[str, Any] = {}
    total_functions = 0
    total_classes = 0
    functions_with_docstrings = 0
    total_function_params = 0
    function_count_for_avg = 0

    for _nid, n in (nodes or {}).items():
        if not isinstance(n, Mapping):
            continue

        fn_list = n.get("functions")
        if isinstance(fn_list, list):
            total_functions += len(fn_list)

        cl_list = n.get("classes")
        if isinstance(cl_list, list):
            total_classes += len(cl_list)

        fnd_list = n.get("functions_detailed")
        if isinstance(fnd_list, list):
            for func in fnd_list:
                if isinstance(func, Mapping):
                    if func.get("doc"):
                        functions_with_docstrings += 1
                    params = func.get("p")
                    if isinstance(params, list):
                        total_function_params += len(params)
                        function_count_for_avg += 1

    if total_functions > 0 or total_classes > 0:
        api_surface = {
            "total_functions": total_functions,
            "total_classes": total_classes,
        }
        if functions_with_docstrings > 0:
            api_surface["functions_with_docstrings"] = functions_with_docstrings
            if total_functions > 0:
                api_surface["docstring_coverage"] = round(functions_with_docstrings / total_functions, 3)
        if function_count_for_avg > 0:
            api_surface["avg_params_per_function"] = round(total_function_params / function_count_for_avg, 2)

    environment_summary: Dict[str, Any] = {}
    env_vars: Dict[str, List[str]] = {}
    secret_patterns = ["key", "secret", "password", "token", "credential", "private"]

    for _nid, n in (nodes or {}).items():
        if not isinstance(n, Mapping):
            continue

        for field_name in crosstalk_fields.keys():
            ct_data = n.get(field_name)
            if not ct_data or not isinstance(ct_data, Mapping):
                continue

            rows = ct_data.get("rows", [])
            legend = ct_data.get("legend", {})
            schema = ct_data.get("schema", [])

            kind_idx = None
            meta_idx = None
            for i, field in enumerate(schema):
                field_meaning = legend.get(field, field)
                if field_meaning == "kind" or field == "k":
                    kind_idx = i
                elif field_meaning == "meta" or field == "m":
                    meta_idx = i

            if kind_idx is None or meta_idx is None:
                continue

            for row in rows:
                if not isinstance(row, list) or len(row) <= max(kind_idx, meta_idx):
                    continue

                kind = row[kind_idx]
                if kind == "env_ref":
                    meta_row = row[meta_idx]
                    if isinstance(meta_row, Mapping):
                        var_name = meta_row.get("var")
                        if isinstance(var_name, str) and var_name:
                            file_path = n.get("file")
                            if isinstance(file_path, str):
                                env_vars.setdefault(var_name, []).append(file_path)

    if env_vars:
        sorted_vars = sorted(env_vars.items(), key=lambda x: len(x[1]), reverse=True)
        secrets = [
            var for var, _files in sorted_vars
            if any(pattern in var.lower() for pattern in secret_patterns)
        ]
        environment_summary = {
            "total_vars": len(env_vars),
            "total_references": sum(len(files_) for files_ in env_vars.values()),
            "top_vars": [
                {"name": var, "used_in_files": len(files_)}
                for var, files_ in sorted_vars[:15]
            ],
        }
        if secrets:
            environment_summary["secrets_detected"] = secrets[:10]

    http_contracts: Dict[str, Any] = {}
    backend_routes: List[Dict[str, Any]] = []
    frontend_calls: List[Dict[str, Any]] = []

    for _nid, n in (nodes or {}).items():
        if not isinstance(n, Mapping):
            continue

        for field_name in crosstalk_fields.keys():
            ct_data = n.get(field_name)
            if not ct_data or not isinstance(ct_data, Mapping):
                continue

            rows = ct_data.get("rows", [])
            legend = ct_data.get("legend", {})
            schema = ct_data.get("schema", [])

            kind_idx = None
            join_idx = None
            where_idx = None
            for i, field in enumerate(schema):
                field_meaning = legend.get(field, field)
                if field_meaning == "kind" or field == "k":
                    kind_idx = i
                elif field_meaning == "join" or field == "j":
                    join_idx = i
                elif field_meaning == "where" or field == "w":
                    where_idx = i

            if kind_idx is None:
                continue

            for row in rows:
                if not isinstance(row, list) or len(row) <= kind_idx:
                    continue

                kind = row[kind_idx]
                if kind == "http_route":
                    route_info: Dict[str, Any] = {"file": n.get("file")}
                    if join_idx is not None and len(row) > join_idx:
                        route_info["endpoint"] = row[join_idx]
                    if where_idx is not None and len(row) > where_idx:
                        where = row[where_idx]
                        if isinstance(where, Mapping):
                            route_info["line"] = where.get("line")
                    backend_routes.append(route_info)
                elif kind == "http_call":
                    call_info: Dict[str, Any] = {"file": n.get("file")}
                    if join_idx is not None and len(row) > join_idx:
                        call_info["endpoint"] = row[join_idx]
                    if where_idx is not None and len(row) > where_idx:
                        where = row[where_idx]
                        if isinstance(where, Mapping):
                            call_info["line"] = where.get("line")
                    frontend_calls.append(call_info)

    if backend_routes or frontend_calls:
        http_contracts = {
            "backend_routes": len(backend_routes),
            "frontend_calls": len(frontend_calls),
        }
        if backend_routes and frontend_calls:
            http_contracts["coverage_ratio"] = round(len(frontend_calls) / len(backend_routes), 3)
        if backend_routes:
            http_contracts["top_routes"] = backend_routes[:10]
        if frontend_calls:
            http_contracts["top_calls"] = frontend_calls[:10]

    testing_summary: Dict[str, Any] = {}
    test_patterns = ["test_", "_test.", ".test.", "spec.", "_spec."]
    test_files = 0
    source_files = 0

    for _nid, n in (nodes or {}).items():
        if not isinstance(n, Mapping):
            continue

        file_path = n.get("file")
        if not isinstance(file_path, str):
            continue

        file_name = file_path.split("/")[-1].lower()
        is_test = any(pattern in file_name for pattern in test_patterns)

        if is_test:
            test_files += 1
        else:
            source_files += 1

    if test_files > 0 or source_files > 0:
        testing_summary = {"test_files": test_files, "source_files": source_files}
        if source_files > 0:
            testing_summary["test_to_source_ratio"] = round(test_files / source_files, 3)

    change_risk: Dict[str, Any] = {}
    high_risk_modules: List[Dict[str, Any]] = []

    for nid, n in (nodes or {}).items():
        if not isinstance(n, Mapping):
            continue

        importers = n.get("importers_count", 0)
        loc = n.get("loc", 0)
        sloc = n.get("sloc", 0)
        comment_pct = n.get("comment_pct")
        scc_size = n.get("scc_size", 1)

        risk_score = 0.0
        risk_reasons: List[str] = []

        if isinstance(importers, int) and importers > 0:
            risk_score += importers * 3
            if importers > 10:
                risk_reasons.append(f"{importers} importers (high blast radius)")

        if isinstance(sloc, int) and sloc > 0:
            risk_score += sloc / 80
            if sloc > 500:
                risk_reasons.append(f"{sloc} SLOC (large code module)")
        elif isinstance(loc, int) and loc > 0:
            risk_score += loc / 100
            if loc > 500:
                risk_reasons.append(f"{loc} LOC (large module)")

        if isinstance(comment_pct, (int, float)):
            risk_score += (1 - float(comment_pct)) * 10
            if float(comment_pct) < 0.05 and isinstance(sloc, int) and sloc > 200:
                risk_reasons.append("low comment density")

        if isinstance(scc_size, int) and scc_size > 1:
            risk_score += scc_size * 2
            scc_size_rt = n.get("scc_size_runtime")
            if isinstance(scc_size_rt, int) and scc_size_rt > 1:
                risk_reasons.append(f"in {scc_size}-module cycle (runtime)")
            else:
                risk_reasons.append(f"in {scc_size}-module cycle (conceptual/TC)")

        if risk_score > 30 or (isinstance(importers, int) and importers > 15):
            high_risk_modules.append({
                "file": n.get("file"),
                "name": n.get("name"),
                "risk_score": round(risk_score, 1),
                "blast_radius": importers,
                "loc": loc,
                "sloc": sloc,
                "comment_pct": comment_pct,
                "reasons": risk_reasons,
            })

    high_risk_modules.sort(key=lambda x: x["risk_score"], reverse=True)

    if high_risk_modules:
        change_risk = {
            "high_risk_count": len(high_risk_modules),
            "top_risky_modules": high_risk_modules[:10],
        }

    loc_summary: Dict[str, Any] = {
        "total_loc": total_loc,
        "total_sloc": total_sloc,
        "total_comment_lines": total_comment_lines,
        "total_blank_lines": total_blank_lines,
        "comment_pct": comment_pct_code,
        "top_files_by_loc": top_loc,
    }
    if comment_summary:
        loc_summary["comment"] = comment_summary

    summary: Dict[str, Any] = {
        "counts": {
            "nodes": node_count,
            "edges": edge_count,
            "zones": zone_count,
            "node_order": node_order_count,
            "files": file_count,
        },
        "loc": loc_summary,
        "parse_status": parse_status_summary,
        "edges": {"kinds": edge_kind_counts},
        "hotspots": {
            "top_imported": top_imported,
            "top_dependencies": top_dependencies,
        },
        "zones": {"largest": largest_zones},
    }

    if cross_zone_edges_top:
        summary["zones"]["cross_zone_edges_top"] = cross_zone_edges_top

    has_v18_cycle_meta = (
        isinstance(nodefacts_meta, Mapping)
        and any(k in nodefacts_meta for k in ("scc_conceptual", "scc_runtime", "tc_inflation"))
    )

    if scc_sizes or has_v18_cycle_meta:
        cycles_entry: Dict[str, Any] = {
            "cycle_nodes": cycle_nodes,
            "largest_scc_size": max(scc_sizes) if scc_sizes else 0,
        }
        if isinstance(nodefacts_meta, Mapping):
            scc_c = nodefacts_meta.get("scc_conceptual")
            scc_r = nodefacts_meta.get("scc_runtime")
            tc_inf = nodefacts_meta.get("tc_inflation")
            if isinstance(scc_c, Mapping):
                cycles_entry["scc_conceptual"] = dict(scc_c)
            if isinstance(scc_r, Mapping):
                cycles_entry["scc_runtime"] = dict(scc_r)
            if isinstance(tc_inf, (int, float)):
                cycles_entry["tc_inflation"] = int(tc_inf)
        summary["cycles"] = cycles_entry

    if crosstalk_summary:
        summary["crosstalk"] = crosstalk_summary
    if api_surface:
        summary["api_surface"] = api_surface
    if environment_summary:
        summary["environment"] = environment_summary
    if http_contracts:
        summary["http_contracts"] = http_contracts
    if testing_summary:
        summary["testing"] = testing_summary
    if change_risk:
        summary["change_risk"] = change_risk

    if import_summary:
        totals = import_summary.get("totals", {})
        if totals:
            summary.setdefault("imports", {})
            summary["imports"]["totals"] = totals

        top_packages = import_summary.get("top_third_party_packages", [])
        if isinstance(top_packages, list) and top_packages:
            summary.setdefault("imports", {})
            summary["imports"]["top_packages"] = [
                {
                    "package": pkg.get("package"),
                    "file_count": pkg.get("file_count"),
                    "import_count": pkg.get("import_count"),
                }
                for pkg in top_packages[:10]
                if isinstance(pkg, Mapping)
            ]

    if import_style_totals:
        summary.setdefault("imports", {})
        summary["imports"]["style_totals"] = import_style_totals

    langs = sorted({
        (n.get("lang") or "").strip()
        for n in (nodes or {}).values()
        if isinstance(n, Mapping) and isinstance(n.get("lang"), str) and n.get("lang").strip()
    })

    if not langs:
        ml = meta.get("language") if meta else None
        if isinstance(ml, str) and ml.strip():
            langs = [ml.strip()]

    derived_language = "polyglot" if len(langs) > 1 else (langs[0] if langs else "unknown")

    if meta:
        sm = {k: meta.get(k) for k in ("repo_name", "mode", "generated_at") if k in meta}
        sm["language"] = derived_language
        sm["languages"] = langs
        summary["meta"] = sm

    return summary