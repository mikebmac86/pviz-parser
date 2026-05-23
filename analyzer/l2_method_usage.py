# analyzer/dead_code/l2_method_usage.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple, Iterable
import ast
import os
from analyzer.ast_common import (
    ScopeAwareVisitor,
    flatten_attr_chain,
    attr_root_name_and_leaf,
)
from adapters.canonical import moduleish_for_path
@dataclass(frozen=True)
class BoundMethodKey:
    mod_id: str
    class_name: str
    method_name: str

class _Symtab:
    def __init__(self, mod_id: str):
        self.mod_id = mod_id
        self.names: Dict[str, Tuple[str, str]] = {}              # name -> ("class", class_name)
        self.attr_to_class: Dict[Tuple[str, str], str] = {}      # ("self","view") -> "GraphView"
        self.local_classes: Set[str] = set()
        self.imports: Dict[str, str] = {}                        # alias -> origin (class or module)

    @staticmethod
    def _mod_id_for_path(path: str) -> str:
        return moduleish_for_path(path)

    def note_local_class(self, class_name: str) -> None:
        self.local_classes.add(class_name)
        self.names[class_name] = ("class", class_name)

    def note_import_as(self, asname: str, origin: str) -> None:
        self.imports[asname] = origin

    def bind_name_to_class(self, name: str, class_name: str) -> None:
        self.names[name] = ("class", class_name)

    def bind_attr_to_class(self, base: str, attr: str, class_name: str) -> None:
        self.attr_to_class[(base, attr)] = class_name

    def resolve_name_class(self, name: str) -> Optional[str]:
        kv = self.names.get(name)
        if kv and kv[0] == "class":
            return kv[1]
        return None

    def resolve_attr_chain_class(self, chain: Iterable[str]) -> Optional[str]:
        parts = list(chain)
        if len(parts) == 2:
            return self.attr_to_class.get((parts[0], parts[1]))
        return None


class _L2Visitor(ScopeAwareVisitor):
    def __init__(self, mod_id: str, file_path: str):
        super().__init__()
        self.file_path = file_path
        self.mod_id = mod_id
        self.sym = _Symtab(mod_id)
        self.used: Set[BoundMethodKey] = set()

    # -------- Phase A: defs & imports (use ScopeAwareVisitor hook) --------
    def _on_classdef(self, node: ast.ClassDef) -> None:
        # record local class names (needed to resolve constructor bindings)
        self.sym.note_local_class(node.name)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.sym.note_import_as(alias.asname or alias.name, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        origin = (node.module or "").strip(".")
        for alias in node.names:
            self.sym.note_import_as(alias.asname or alias.name, alias.name or origin)
        self.generic_visit(node)

    # ---------------- Phase B: constructor bindings ----------------
    def visit_Assign(self, node: ast.Assign) -> None:
        try:
            rhs = node.value
            if isinstance(rhs, ast.Call):
                cls = self._class_name_from_call(rhs.func)
                if cls:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            # name = Class(...)
                            self.sym.bind_name_to_class(t.id, cls)
                        elif isinstance(t, ast.Attribute):
                            # base.attr = Class(...)
                            root, leaf = attr_root_name_and_leaf(t)
                            if root and leaf:
                                self.sym.bind_attr_to_class(root, leaf, cls)
            elif isinstance(rhs, ast.Name):
                # aliasing: base.attr = name   (where name already bound to a class)
                rhs_cls = self.sym.resolve_name_class(rhs.id)
                if rhs_cls:
                    for t in node.targets:
                        if isinstance(t, ast.Attribute):
                            root, leaf = attr_root_name_and_leaf(t)
                            if root and leaf:
                                self.sym.bind_attr_to_class(root, leaf, rhs_cls)
                        elif isinstance(t, ast.Name):
                            self.sym.bind_name_to_class(t.id, rhs_cls)
        finally:
            self.generic_visit(node)

    # ---------------- Phase C: bound method uses -------------------
    def visit_Call(self, node: ast.Call) -> None:
        # direct call: obj.attr.method(...)
        bm = self._resolve_bound_method_from_expr(node.func)
        if bm:
            self.used.add(bm)

        # callbacks: foo(obj.attr.method) or kw= obj.attr.method
        for arg in node.args:
            bm = self._resolve_bound_method_from_expr(arg)
            if bm:
                self.used.add(bm)
        for kw in (node.keywords or []):
            val = kw.value
            bm = self._resolve_bound_method_from_expr(val)
            if bm:
                self.used.add(bm)

        self.generic_visit(node)

    # ---------------- Utilities -------------------
    def _class_name_from_call(self, func: ast.AST) -> Optional[str]:
        if isinstance(func, ast.Name):
            name = func.id
            if name in self.sym.local_classes:
                return name
            imp = self.sym.imports.get(name)
            if imp and imp[:1].isupper():
                return imp
            return name if name[:1].isupper() else None
        if isinstance(func, ast.Attribute):
            chain = flatten_attr_chain(func)
            if chain:
                last = chain[-1]
                return last if last[:1].isupper() else None
        return None

    def _resolve_bound_method_from_expr(self, expr: ast.AST) -> Optional[BoundMethodKey]:
        if isinstance(expr, ast.Attribute):
            chain = flatten_attr_chain(expr)
            if chain and len(chain) >= 2:
                method = chain[-1]
                base_chain = chain[:-1]
                base_cls = self.sym.resolve_attr_chain_class(base_chain)
                if base_cls:
                    return BoundMethodKey(self.mod_id, base_cls, method)
        return None


def find_bound_method_usages(file_path: str, mod_id: Optional[str] = None) -> Set[BoundMethodKey]:
    if not file_path or not os.path.exists(file_path):
        return set()
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
        tree = ast.parse(code, filename=file_path)
    except Exception:
        return set()
    if not mod_id:
        mod_id = _Symtab._mod_id_for_path(file_path)
    v = _L2Visitor(mod_id, file_path)
    v.visit(tree)
    return v.used

def attach_l2_used_methods(report) -> None:
    """
    Populate report._l2_used_method_names_by_module = { module_id: {method_name, ...}, ... }
    Does NOT mutate any unused_* lists.
    """
    mod_to_path: Dict[str, str] = {}
    for mid, modrep in (getattr(report, "modules", {}) or {}).items():
        path = getattr(modrep, "path", None)
        if not path and hasattr(modrep, "meta"):
            path = (modrep.meta or {}).get("path")
        if isinstance(path, str):
            mod_to_path[mid] = path

    by_mod: Dict[str, Set[str]] = {}
    for mid, path in mod_to_path.items():
        used = find_bound_method_usages(path, mod_id=mid)
        if not used:
            continue
        for bm in used:
            by_mod.setdefault(bm.mod_id, set()).add(bm.method_name)

    setattr(report, "_l2_used_method_names_by_module", by_mod)
