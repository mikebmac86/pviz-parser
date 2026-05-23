#analyzer/ts/symbols_js.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set


@dataclass(frozen=True)
class JSSymbols:
    functions: List[str]
    classes: List[str]
    globals: List[str]
    exports: List[str]
    facts: Dict[str, Any]


def _node_text(source: bytes, node: Any) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _ident_text(source: bytes, node: Optional[Any]) -> Optional[str]:
    """
    Identifier text helper.

    In TS grammars, names can be emitted as:
      - identifier
      - type_identifier
      - property_identifier
      - namespace_identifier (sometimes)
    """
    if node is None:
        return None
    if node.type in ("identifier", "type_identifier", "property_identifier", "namespace_identifier"):
        txt = _node_text(source, node).strip()
        return txt or None
    return None


def _count_desc(node: Any, t: str) -> int:
    if node is None:
        return 0
    n = 1 if node.type == t else 0
    for ch in getattr(node, "children", []) or []:
        n += _count_desc(ch, t)
    return n


def _find_first_desc(node: Any, t: str) -> Optional[Any]:
    if node is None:
        return None
    if node.type == t:
        return node
    for ch in getattr(node, "children", []) or []:
        out = _find_first_desc(ch, t)
        if out is not None:
            return out
    return None


def _first_ident_in(node: Any, source: bytes) -> Optional[str]:
    """
    Best-effort identifier finder under a subtree.
    """
    if node is None:
        return None
    nm = _ident_text(source, node)
    if nm:
        return nm
    for ch in getattr(node, "children", []) or []:
        nm = _ident_text(source, ch)
        if nm:
            return nm
    for ch in getattr(node, "children", []) or []:
        nm = _first_ident_in(ch, source)
        if nm:
            return nm
    return None


def _summarize_class(cls_node: Any, source: bytes) -> Dict[str, Any]:
    """
    Grammar-tolerant class summary (no type-checking required).
    Returns additive metadata for facts["classes"].
    """
    name_node = cls_node.child_by_field_name("name") if hasattr(cls_node, "child_by_field_name") else None
    name = _ident_text(source, name_node) or "<anonymous>"

    # Best-effort extends target:
    extends_name: Optional[str] = None

    # Common TS tree-sitter fields:
    # - "superclass" exists in some grammars
    sup = cls_node.child_by_field_name("superclass") if hasattr(cls_node, "child_by_field_name") else None
    if sup is not None:
        extends_name = _first_ident_in(sup, source)

    # Some grammars put extends/implements under 'class_heritage'
    if extends_name is None:
        heritage = _find_first_desc(cls_node, "class_heritage")
        if heritage is not None:
            extends_name = _first_ident_in(heritage, source)

    # Implements: best-effort; without grammar-specific parsing we approximate
    implements_count = 0
    heritage2 = _find_first_desc(cls_node, "class_heritage")
    if heritage2 is not None:
        implements_count = _node_text(source, heritage2).count("implements")

    # Members
    methods_count = _count_desc(cls_node, "method_definition")
    # Property nodes vary by grammar; count a few common ones
    props_count = (
        _count_desc(cls_node, "public_field_definition")
        + _count_desc(cls_node, "property_definition")
        + _count_desc(cls_node, "public_field")
        + _count_desc(cls_node, "field_definition")
    )

    decorators_count = _count_desc(cls_node, "decorator")

    return {
        "name": name,
        "extends": extends_name,
        "implements_count": int(implements_count),
        "methods_count": int(methods_count),
        "props_count": int(props_count),
        "decorators_count": int(decorators_count),
    }


def extract_js_symbols(*, tree: Any, source: bytes) -> JSSymbols:
    root = tree.root_node
    top_level = list(root.children)

    functions: Set[str] = set()
    classes: Set[str] = set()
    globals_: Set[str] = set()
    exports: Set[str] = set()

    export_kinds: Dict[str, str] = {}

    # TS-only top-level decls (kept in facts)
    ts_types: Set[str] = set()
    ts_interfaces: Set[str] = set()
    ts_enums: Set[str] = set()
    ts_namespaces: Set[str] = set()

    # class richness (additive; no schema change)
    class_details: List[Dict[str, Any]] = []

    def add_export(name: str, kind: str) -> None:
        exports.add(name)
        export_kinds[name] = kind

    def _add_class_from_decl(
        decl: Any,
        *,
        is_exported: bool,
        is_default: bool,
    ) -> None:
        nm = _ident_text(source, decl.child_by_field_name("name"))
        # Record name for legacy classes list
        if nm:
            classes.add(nm)

        # Add rich facts (always; name may be <anonymous>)
        det = _summarize_class(decl, source)
        det["exported"] = bool(is_exported)
        det["is_default"] = bool(is_default)
        class_details.append(det)

        # Export kinds (only for named classes)
        if nm and is_exported:
            add_export(nm, "export_class_default" if is_default else "export_class")

    def handle_var_decl(node: Any, *, is_exported: bool = False) -> None:
        for ch in node.children:
            if ch.type != "variable_declarator":
                continue
            name_node = ch.child_by_field_name("name")
            init_node = ch.child_by_field_name("value") or ch.child_by_field_name("initializer")

            nm = _ident_text(source, name_node)
            if nm:
                globals_.add(nm)
                if is_exported:
                    add_export(nm, "export_var")

            if nm and init_node is not None:
                if init_node.type in ("arrow_function", "function", "function_expression"):
                    functions.add(nm)
                    if is_exported:
                        add_export(nm, "export_fn_assigned")

    def handle_export_statement(exp: Any) -> None:
        # Determine if this export_statement is a default export.
        is_default = any(ch.type == "default" for ch in exp.children)
        if is_default:
            add_export("default", "export_default")

        decl = exp.child_by_field_name("declaration")
        if decl is not None:
            if decl.type == "function_declaration":
                nm = _ident_text(source, decl.child_by_field_name("name"))
                if nm:
                    functions.add(nm)
                    add_export(nm, "export_fn_default" if is_default else "export_fn")

            elif decl.type == "class_declaration":
                _add_class_from_decl(decl, is_exported=True, is_default=is_default)

            elif decl.type in ("lexical_declaration", "variable_declaration"):
                handle_var_decl(decl, is_exported=True)

            elif decl.type == "interface_declaration":
                nm = _ident_text(source, decl.child_by_field_name("name"))
                if nm:
                    ts_interfaces.add(nm)
                    add_export(nm, "export_interface")

            elif decl.type == "type_alias_declaration":
                nm = _ident_text(source, decl.child_by_field_name("name"))
                if nm:
                    ts_types.add(nm)
                    add_export(nm, "export_type")

            elif decl.type == "enum_declaration":
                nm = _ident_text(source, decl.child_by_field_name("name"))
                if nm:
                    ts_enums.add(nm)
                    add_export(nm, "export_enum")

        clause = exp.child_by_field_name("clause") or exp.child_by_field_name("export_clause")
        if clause is not None:
            for ch in clause.children:
                if ch.type != "export_specifier":
                    continue
                name_node = ch.child_by_field_name("name")
                alias_node = ch.child_by_field_name("alias")
                nm = _ident_text(source, alias_node) or _ident_text(source, name_node)
                if nm:
                    add_export(nm, "export_clause")

        # export * (re-export star)
        for ch in exp.children:
            if ch.type == "*":
                add_export("*", "export_star")
                break

    # Top-level scan
    for stmt in top_level:
        t = stmt.type

        if t == "function_declaration":
            nm = _ident_text(source, stmt.child_by_field_name("name"))
            if nm:
                functions.add(nm)

        elif t == "class_declaration":
            # Non-exported top-level class
            nm = _ident_text(source, stmt.child_by_field_name("name"))
            if nm:
                classes.add(nm)
            # Add richness for non-exported classes too
            det = _summarize_class(stmt, source)
            det["exported"] = False
            det["is_default"] = False
            class_details.append(det)

        elif t in ("lexical_declaration", "variable_declaration"):
            handle_var_decl(stmt, is_exported=False)

        elif t == "export_statement":
            handle_export_statement(stmt)

        elif t == "interface_declaration":
            nm = _ident_text(source, stmt.child_by_field_name("name"))
            if nm:
                ts_interfaces.add(nm)

        elif t == "type_alias_declaration":
            nm = _ident_text(source, stmt.child_by_field_name("name"))
            if nm:
                ts_types.add(nm)

        elif t == "enum_declaration":
            nm = _ident_text(source, stmt.child_by_field_name("name"))
            if nm:
                ts_enums.add(nm)

        # Some grammars call these "module_declaration" or "namespace_declaration"
        elif t in ("namespace_declaration", "module_declaration"):
            nm = _ident_text(source, stmt.child_by_field_name("name"))
            if nm:
                ts_namespaces.add(nm)

    facts: Dict[str, Any] = {}
    if export_kinds:
        facts["export_kinds"] = export_kinds
    if ts_types:
        facts["types"] = sorted(ts_types)
    if ts_interfaces:
        facts["interfaces"] = sorted(ts_interfaces)
    if ts_enums:
        facts["enums"] = sorted(ts_enums)
    if ts_namespaces:
        facts["namespaces"] = sorted(ts_namespaces)
    if class_details:
        # stable order: by name then exported/default, but keep duplicates out
        # (we keep details list as-is, but you can sort if you prefer determinism)
        facts["classes"] = class_details

    return JSSymbols(
        functions=sorted(functions),
        classes=sorted(classes),
        globals=sorted(globals_),
        exports=sorted(exports),
        facts=facts,
    )
