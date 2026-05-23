from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .edges import dedupe_edges


@dataclass(frozen=True)
class CrosstalkRule:
    src_lang: str
    dst_lang: str
    src_candidate_field: str
    dst_candidate_field: str
    src_label: str
    dst_label: str


CROSSTALK_RULES: Tuple[CrosstalkRule, ...] = (
    CrosstalkRule(
        src_lang="python",
        dst_lang="ts",
        src_candidate_field="crosstalk_candidates_py_v1",
        dst_candidate_field="crosstalk_candidates_ts_v1",
        src_label="python",
        dst_label="typescript",
    ),
)


def index_crosstalk_candidates(
    nodes: Mapping[str, Dict[str, Any]],
    candidate_field: str,
) -> Tuple[Dict[str, List[Tuple[str, Dict[str, Any]]]], int, int]:
    index: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    total_candidates = 0
    files_with_candidates = 0

    for node_id, node in nodes.items():
        candidates = node.get(candidate_field, [])
        if not isinstance(candidates, list) or not candidates:
            continue

        files_with_candidates += 1
        total_candidates += len(candidates)

        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            join = cand.get("join")
            if join:
                index.setdefault(str(join), []).append((node_id, cand))

    return index, total_candidates, files_with_candidates


def should_create_edge(src_cand: Dict[str, Any], dst_cand: Dict[str, Any]) -> bool:
    src_conf = src_cand.get("evidence", {}).get("confidence", 1.0)
    dst_conf = dst_cand.get("evidence", {}).get("confidence", 1.0)
    return src_conf >= 0.5 and dst_conf >= 0.5


def create_crosstalk_edge(
    *,
    src_node_id: str,
    dst_node_id: str,
    src_cand: Dict[str, Any],
    dst_cand: Dict[str, Any],
    join: str,
    src_lang_label: str,
    dst_lang_label: str,
) -> Dict[str, Any]:
    cand_kind = src_cand.get("kind", dst_cand.get("kind", "unknown"))
    src_evidence = src_cand.get("evidence", {})
    dst_evidence = dst_cand.get("evidence", {})
    src_conf = src_evidence.get("confidence", 1.0)
    dst_conf = dst_evidence.get("confidence", 1.0)

    edge = {
        "src": src_node_id,
        "dst": dst_node_id,
        "kind": "crosstalk",
        "crosstalk": {
            "join": join,
            "candidate_kind": cand_kind,
            "src_lang": src_lang_label,
            "dst_lang": dst_lang_label,
            "confidence": min(src_conf, dst_conf),
        },
    }

    if src_evidence.get("syntax"):
        edge["crosstalk"]["src_syntax"] = src_evidence["syntax"]
    if dst_evidence.get("syntax"):
        edge["crosstalk"]["dst_syntax"] = dst_evidence["syntax"]

    src_where = src_cand.get("where", {})
    dst_where = dst_cand.get("where", {})

    if src_where:
        edge["crosstalk"]["src_location"] = {
            k: v for k, v in src_where.items() if k in ("line", "col", "file")
        }
    if dst_where:
        edge["crosstalk"]["dst_location"] = {
            k: v for k, v in dst_where.items() if k in ("line", "col", "file")
        }

    src_meta = src_cand.get("meta", {})
    dst_meta = dst_cand.get("meta", {})
    if src_meta or dst_meta:
        edge["crosstalk"]["meta"] = {}
        if src_meta:
            edge["crosstalk"]["meta"]["src"] = src_meta
        if dst_meta:
            edge["crosstalk"]["meta"]["dst"] = dst_meta

    return edge


def generate_crosstalk_edges_for_rule(
    *,
    nodes_by_lang: Mapping[str, Dict[str, Dict[str, Any]]],
    rule: CrosstalkRule,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []

    src_nodes = nodes_by_lang.get(rule.src_lang, {})
    dst_nodes = nodes_by_lang.get(rule.dst_lang, {})

    src_index, src_total, src_files = index_crosstalk_candidates(src_nodes, rule.src_candidate_field)
    dst_index, dst_total, dst_files = index_crosstalk_candidates(dst_nodes, rule.dst_candidate_field)

    stats: Dict[str, Any] = {
        "src_lang": rule.src_lang,
        "dst_lang": rule.dst_lang,
        "src_candidates_total": src_total,
        "dst_candidates_total": dst_total,
        "src_files_with_candidates": src_files,
        "dst_files_with_candidates": dst_files,
        "unique_src_joins": len(src_index),
        "unique_dst_joins": len(dst_index),
        "matched_joins": 0,
        "edges_created": 0,
        "by_kind": {},
    }

    matched_joins = set(src_index.keys()) & set(dst_index.keys())
    stats["matched_joins"] = len(matched_joins)

    for join in matched_joins:
        src_items = src_index[join]
        dst_items = dst_index[join]

        kind = "unknown"
        if src_items:
            kind = src_items[0][1].get("kind", "unknown")
        elif dst_items:
            kind = dst_items[0][1].get("kind", "unknown")

        stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + (len(src_items) * len(dst_items))

        for src_node_id, src_cand in src_items:
            for dst_node_id, dst_cand in dst_items:
                if not should_create_edge(src_cand, dst_cand):
                    continue
                edges.append(
                    create_crosstalk_edge(
                        src_node_id=src_node_id,
                        dst_node_id=dst_node_id,
                        src_cand=src_cand,
                        dst_cand=dst_cand,
                        join=join,
                        src_lang_label=rule.src_label,
                        dst_lang_label=rule.dst_label,
                    )
                )
                stats["edges_created"] += 1

    return edges, stats


def generate_crosstalk_edges(
    *,
    nodes_by_lang: Mapping[str, Dict[str, Dict[str, Any]]],
    rules: Sequence[CrosstalkRule] = CROSSTALK_RULES,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    all_edges: List[Dict[str, Any]] = []
    rule_stats: List[Dict[str, Any]] = []

    for rule in rules:
        edges, stats = generate_crosstalk_edges_for_rule(nodes_by_lang=nodes_by_lang, rule=rule)
        all_edges.extend(edges)
        rule_stats.append(stats)

    legacy = {
        "py_candidates_total": 0,
        "ts_candidates_total": 0,
        "py_files_with_candidates": 0,
        "ts_files_with_candidates": 0,
        "unique_py_joins": 0,
        "unique_ts_joins": 0,
        "matched_joins": 0,
        "edges_created": len(all_edges),
        "by_kind": {},
        "rules": rule_stats,
    }

    for stats in rule_stats:
        if stats.get("src_lang") == "python" and stats.get("dst_lang") == "ts":
            legacy.update(
                {
                    "py_candidates_total": stats.get("src_candidates_total", 0),
                    "ts_candidates_total": stats.get("dst_candidates_total", 0),
                    "py_files_with_candidates": stats.get("src_files_with_candidates", 0),
                    "ts_files_with_candidates": stats.get("dst_files_with_candidates", 0),
                    "unique_py_joins": stats.get("unique_src_joins", 0),
                    "unique_ts_joins": stats.get("unique_dst_joins", 0),
                    "matched_joins": stats.get("matched_joins", 0),
                    "by_kind": stats.get("by_kind", {}),
                }
            )
            break

    return dedupe_edges(all_edges), legacy