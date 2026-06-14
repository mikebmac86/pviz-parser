from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional

from analyzer.ruby.parse_ruby.models import RubyAnalysis


class RailsEdge(NamedTuple):
    source_file: str         # rel_path of declaring file
    target_file: Optional[str]  # rel_path of associated file, if resolved
    edge_type: str           # "belongs_to" | "has_many" | "has_one" | etc.
    association_name: str    # the DSL name ("user", "posts", etc.)
    reason: str


class RailsAnnotation(NamedTuple):
    file: str
    annotation_type: str     # "rails_role" | "has_route" | "has_callback" | etc.
    value: str


def build_rails_edges(
    *,
    analysis: RubyAnalysis,
    fq_decl_to_file: Dict[str, str],
    min_confidence: float = 0.0,   # associations are structurally reliable; include all
) -> List[RailsEdge]:
    """
    Project Rails ActiveRecord associations into cross-file edges.

    Resolution: "belongs_to :user" -> look for a "User" class declared
    somewhere in the codebase.  Uses the fq_decl_to_file index.
    """
    edges: list[RailsEdge] = []
    seen: set[tuple] = set()

    indexes = analysis.indexes or {}
    rails_index = indexes.get("rails") or {}
    associations_by_file: Dict[str, List[dict]] = rails_index.get("associations") or {}

    for source_file, assoc_list in associations_by_file.items():
        for assoc in (assoc_list or []):
            if not isinstance(assoc, dict):
                continue
            kind: str = str(assoc.get("kind") or "")
            name: str = str(assoc.get("name") or "")
            if not kind or not name:
                continue

            # Heuristic: association name "user" -> class "User",
            # "blog_posts" -> "BlogPost" (singularize + camelize).
            candidate_class = _association_name_to_class(name)
            target_file = fq_decl_to_file.get(candidate_class) if candidate_class else None

            key = (source_file, candidate_class, kind)
            if key in seen:
                continue
            seen.add(key)

            edges.append(RailsEdge(
                source_file=str(source_file),
                target_file=target_file,
                edge_type=kind,
                association_name=name,
                reason="rails_association_decl" if target_file else "rails_association_unresolved",
            ))

    return sorted(edges, key=lambda e: (e.source_file, e.edge_type, e.association_name))


def build_rails_annotations(
    *,
    analysis: RubyAnalysis,
) -> List[RailsAnnotation]:
    """
    Emit per-file Rails role and DSL presence annotations for nodefacts.
    """
    annotations: list[RailsAnnotation] = []

    for rel, pf in (analysis.files or {}).items():
        rails = pf.rails
        if not rails:
            continue

        role = rails.role or "unknown"
        if role != "unknown":
            annotations.append(RailsAnnotation(
                file=str(rel),
                annotation_type="rails_role",
                value=role,
            ))

        dsl = rails.dsl or {}
        for dsl_key in ("associations", "callbacks", "validations", "scopes", "routes", "async_invocations"):
            items = dsl.get(dsl_key)
            if items:
                annotations.append(RailsAnnotation(
                    file=str(rel),
                    annotation_type=f"has_{dsl_key}",
                    value=str(len(items)),
                ))

    return sorted(annotations, key=lambda a: (a.file, a.annotation_type))


def _association_name_to_class(name: str) -> Optional[str]:
    """
    Heuristic: convert an ActiveRecord association name to a class name.
    "user"        -> "User"
    "blog_posts"  -> "BlogPost"   (naive singularize: strip trailing 's')
    "categories"  -> "Category"   (not attempted — too complex without inflections)
    """
    if not name:
        return None
    # Strip leading colon if present (symbol form)
    s = name.lstrip(":")
    # Naive singularization for has_many
    if s.endswith("ies"):
        s = s[:-3] + "y"
    elif s.endswith("ses") or s.endswith("xes"):
        s = s[:-2]
    elif s.endswith("s") and not s.endswith("ss"):
        s = s[:-1]
    # Camelize
    return "".join(part.capitalize() for part in s.split("_") if part)