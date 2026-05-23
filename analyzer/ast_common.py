# analyzer/ast_common.py
from __future__ import annotations
from typing import List, Set, Optional, Iterable, Tuple, Dict, Any, Union
from dataclasses import dataclass, field
import ast

# ============================================================================
# ORIGINAL INFRASTRUCTURE (PRESERVED EXACTLY)
# ============================================================================

# ---------------- Scope tracking ----------------

class ScopeAwareVisitor(ast.NodeVisitor):
    """Tracks ['module', ...] and provides module-scope helpers."""
    __slots__ = ("_scope_stack",)

    def __init__(self) -> None:
        super().__init__()
        self._scope_stack: List[str] = ["module"]

    def at_module_scope(self) -> bool:
        return self._scope_stack == ["module"]

    def current_scope(self) -> str:
        return self._scope_stack[-1]

    # Optional hooks
    def _on_classdef(self, node: ast.ClassDef) -> None: ...
    def _on_functiondef(self, node: ast.FunctionDef) -> None: ...
    def _on_asyncfunctiondef(self, node: ast.AsyncFunctionDef) -> None: ...

    # Scope-aware overrides
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._on_classdef(node)
        self._scope_stack.append("class")
        try:
            self.generic_visit(node)
        finally:
            self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._on_functiondef(node)
        self._scope_stack.append("function")
        try:
            self.generic_visit(node)
        finally:
            self._scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._on_asyncfunctiondef(node)
        self._scope_stack.append("function")
        try:
            self.generic_visit(node)
        finally:
            self._scope_stack.pop()


# --------------- TYPE_CHECKING utilities ---------------

def collect_typing_aliases_and_tc_names(tree: ast.AST) -> tuple[Set[str], Set[str]]:
    """
    Returns (typing_aliases, tc_names). tc_names includes bare 'TYPE_CHECKING'.
    """
    typing_aliases: Set[str] = set()
    tc_names: Set[str] = {"TYPE_CHECKING"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing":
                    typing_aliases.add(alias.asname or "typing")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "typing":
                for alias in node.names:
                    if alias.name == "TYPE_CHECKING":
                        tc_names.add(alias.asname or "TYPE_CHECKING")

    typing_aliases.add("typing")
    return typing_aliases, tc_names


def is_tc_name(n: ast.AST, tc_names: Set[str]) -> bool:
    return isinstance(n, ast.Name) and n.id in tc_names


def is_tc_attr(n: ast.AST, typing_aliases: Set[str]) -> bool:
    return (
        isinstance(n, ast.Attribute)
        and n.attr == "TYPE_CHECKING"
        and isinstance(n.value, ast.Name)
        and n.value.id in typing_aliases
    )


def contains_tc(n: ast.AST, typing_aliases: Set[str], tc_names: Set[str]) -> bool:
    found = False
    class _V(ast.NodeVisitor):
        def visit_Name(self, x: ast.Name) -> None:
            nonlocal found
            if x.id in tc_names:
                found = True
        def visit_Attribute(self, x: ast.Attribute) -> None:
            nonlocal found
            if is_tc_attr(x, typing_aliases):
                found = True
            self.generic_visit(x)
    _V().visit(n)
    return found


def classify_type_checking_if(
    test: ast.AST,
    typing_aliases: Set[str],
    tc_names: Set[str],
) -> Optional[str]:
    """
    Returns 'body' if TYPE_CHECKING evaluates to True path,
            'orelse' if False path,
            None if unrelated/unknown.
    Handles: not TYPE_CHECKING, typing.TYPE_CHECKING, bool ops, compares to True/False.
    """
    # not <tc>
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = test.operand
        if is_tc_name(inner, tc_names) or is_tc_attr(inner, typing_aliases):
            return "orelse"

    # <tc> or typing.<tc>
    if is_tc_name(test, tc_names) or is_tc_attr(test, typing_aliases):
        return "body"

    # boolean ops
    if isinstance(test, ast.BoolOp):
        for val in test.values:
            cls = classify_type_checking_if(val, typing_aliases, tc_names)
            if cls in ("body", "orelse"):
                return cls

    # comparisons vs True/False
    if isinstance(test, ast.Compare):
        def _is_true_const(x: ast.AST) -> bool:
            return isinstance(x, ast.Constant) and x.value is True
        def _is_false_const(x: ast.AST) -> bool:
            return isinstance(x, ast.Constant) and x.value is False

        nodes = [test.left] + list(test.comparators)
        if any(is_tc_name(n, tc_names) or is_tc_attr(n, typing_aliases) for n in nodes):
            if any(_is_true_const(n) for n in nodes):
                return "body"
            if any(_is_false_const(n) for n in nodes):
                return "orelse"

    if contains_tc(test, typing_aliases, tc_names):
        return "body"

    return None


# --------------- Scope + TYPE_CHECKING base visitor ---------------

class ScopeAndTCVisitor(ScopeAwareVisitor):
    """
    Adds TYPE_CHECKING propagation and dispatch hooks:
      - on_import(node, under_tc)
      - on_from(node, under_tc)
      - on_call(node, under_tc)
    Subclasses may override those hooks; base traversal handles scope/ifs.
    """

    # Hooks (override as needed)
    def on_import(self, node: ast.Import, *, under_tc: bool) -> None: ...
    def on_from(self, node: ast.ImportFrom, *, under_tc: bool) -> None: ...
    def on_call(self, node: ast.Call, *, under_tc: bool) -> None: ...

    def __init__(self, typing_aliases: Set[str], tc_names: Set[str]) -> None:
        super().__init__()
        self._typing_aliases = typing_aliases
        self._tc_names = tc_names

    # If-handling that propagates under_tc down the correct branch
    def visit_If(self, n: ast.If) -> None:
        try:
            cls = classify_type_checking_if(n.test, self._typing_aliases, self._tc_names)
        except Exception:
            cls = "body" if contains_tc(n.test, self._typing_aliases, self._tc_names) else None

        if cls == "body":
            self._visit_block(n.body, under_tc=True)
            self._visit_block(n.orelse, under_tc=False)
        elif cls == "orelse":
            self._visit_block(n.body, under_tc=False)
            self._visit_block(n.orelse, under_tc=True)
        else:
            self._visit_block(n.body, under_tc=False)
            self._visit_block(n.orelse, under_tc=False)

    # Generic visitor keeps looking for imports/calls and delegates to hooks
    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            self.on_import(node, under_tc=False)
        elif isinstance(node, ast.ImportFrom):
            self.on_from(node, under_tc=False)
        super().generic_visit(node)

    # Internal helpers
    def _visit_block(self, nodes: Iterable[ast.stmt], *, under_tc: bool) -> None:
        for ch in nodes:
            self._dispatch(ch, under_tc)

    def _dispatch(self, node: ast.AST, under_tc: bool) -> None:
        if isinstance(node, ast.If):
            # Recurse with TC-aware logic (parent under_tc is handled in visit_If above)
            self.visit(node)
            return

        if isinstance(node, ast.Import):
            self.on_import(node, under_tc=under_tc)
            return
        if isinstance(node, ast.ImportFrom):
            self.on_from(node, under_tc=under_tc)
            return

        # Calls are interesting for dynamic import detection
        call: Optional[ast.Call] = None
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.value, ast.Call):
            call = node.value

        if call is not None:
            self.on_call(call, under_tc=under_tc)

        super().visit(node)

# --------------- small helpers for literal name lists ---------------

def _literal_str_sequence(value: ast.AST) -> list[str]:
    """
    Extract a flat list of literal strings from list/tuple/set nodes.
    Supports both ast.Constant(str) and ast.Str (py<3.8).
    """
    out: list[str] = []
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        for el in getattr(value, "elts", []) or []:
            if isinstance(el, ast.Constant) and isinstance(getattr(el, "value", None), str):
                out.append(el.value)  # type: ignore[union-attr]
            elif isinstance(el, ast.Str):
                out.append(el.s)
    return out

# --- Detect direct class usage (instantiation, subclassing, isinstance/issubclass) ---

def collect_class_usages(tree: ast.AST) -> set[str]:
    """
    Return a set of class names that appear in:
      - constructor calls (MyClass(...))
      - subclass bases (class Foo(MyClass): ...)
      - isinstance/issubclass calls
    Used by dead_code analysis to mark classes as referenced.
    """
    used: set[str] = set()

    class _CUV(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Name):
                used.add(func.id)
            elif isinstance(func, ast.Attribute):
                chain = flatten_attr_chain(func)
                if chain:
                    used.add(chain[-1])
            self.generic_visit(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            for b in node.bases:
                if isinstance(b, ast.Name):
                    used.add(b.id)
                elif isinstance(b, ast.Attribute):
                    chain = flatten_attr_chain(b)
                    if chain:
                        used.add(chain[-1])
            self.generic_visit(node)

        def visit_If(self, node: ast.If) -> None:
            # isinstance(x, MyClass) / issubclass(X, MyClass)
            if isinstance(node.test, ast.Call) and isinstance(node.test.func, ast.Name):
                if node.test.func.id in ("isinstance", "issubclass"):
                    for arg in node.test.args[1:]:
                        if isinstance(arg, ast.Name):
                            used.add(arg.id)
                        elif isinstance(arg, ast.Attribute):
                            chain = flatten_attr_chain(arg)
                            if chain:
                                used.add(chain[-1])
            self.generic_visit(node)

    _CUV().visit(tree)
    return used

# --------------- Module symbol collector (simple index) ---------------

class ModuleSymbolCollector(ScopeAwareVisitor):
    """Collects module-scope classes/functions/globals and literal __all__."""
    def __init__(self) -> None:
        super().__init__()
        self.classes: list[str] = []
        self.functions: list[str] = []
        self.globals: list[str] = []
        self.all_exports: list[str] = []
        self.warnings: list[str] = []

    # hooks: record only at module scope
    def _on_classdef(self, node: ast.ClassDef) -> None:
        if self.at_module_scope():
            self.classes.append(node.name)

    def _on_functiondef(self, node: ast.FunctionDef) -> None:
        if self.at_module_scope():
            self.functions.append(node.name)

    def _on_asyncfunctiondef(self, node: ast.AsyncFunctionDef) -> None:
        if self.at_module_scope():
            self.functions.append(node.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self.at_module_scope():
            try:
                # __all__ = [...]
                if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                    names = _literal_str_sequence(node.value)
                    if names:
                        self.all_exports = names
                else:
                    # normal module-level globals
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            self.globals.append(t.id)
            except Exception:
                self.warnings.append("__all__ could not be parsed")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self.at_module_scope():
            try:
                if isinstance(node.target, ast.Name):
                    if node.target.id == "__all__":  # annotated __all__
                        names = _literal_str_sequence(node.value)
                        if names:
                            self.all_exports = names
                    else:
                        self.globals.append(node.target.id)
            except Exception:
                # best-effort
                pass
        self.generic_visit(node)


# --------------- Top-level import collection ---------------

def collect_top_level_imports_and_tc_blocks(
    tree: ast.AST,
    typing_aliases: set[str],
    tc_names: set[str],
) -> tuple[list[ast.stmt], list[list[ast.stmt]]]:
    """
    Returns (top_level_imports, type_checking_blocks_of_imports)
    where each item is ast.Import/ast.ImportFrom collected at module scope.

    NOTE: TYPE_CHECKING imports are grouped per top-level 'if' block,
    matching earlier behavior used by parse.py.
    """
    top_level: list[ast.stmt] = []
    tc_blocks: list[list[ast.stmt]] = []

    body = getattr(tree, "body", []) or []

    # 1) collect plain top-level imports
    for n in body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            top_level.append(n)

    # 2) collect TYPE_CHECKING guarded blocks (group imports per block)
    for n in body:
        if isinstance(n, ast.If):
            which = classify_type_checking_if(n.test, typing_aliases, tc_names)
            if which == "body":
                block = [s for s in n.body if isinstance(s, (ast.Import, ast.ImportFrom))]
                if block:
                    tc_blocks.append(block)
            elif which == "orelse":
                block = [s for s in n.orelse if isinstance(s, (ast.Import, ast.ImportFrom))]
                if block:
                    tc_blocks.append(block)

    return top_level, tc_blocks


# --------------- Docstring spans ---------------

def docstring_spans(tree: ast.AST) -> list[tuple[int, int]]:
    """Return (start_line, end_line) for module/class/func docstrings via AST."""
    spans: list[tuple[int, int]] = []

    def _first_expr_doc(body: list[ast.stmt]):
        if not body: return None
        n0 = body[0]
        if isinstance(n0, ast.Expr):
            val = getattr(n0, "value", None)
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                s = getattr(n0, "lineno", None); e = getattr(n0, "end_lineno", None)
                if s is not None and e is not None:
                    return (s, e)
        return None

    if isinstance(tree, ast.Module):
        m = _first_expr_doc(tree.body)
        if m: spans.append(m)

    for n in ast.walk(tree):
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = _first_expr_doc(n.body)
            if d: spans.append(d)

    return spans


# --------------- Attribute chain helpers (AST-only) ---------------

def flatten_attr_chain(expr: ast.AST) -> Optional[List[str]]:
    """
    Flatten an attribute chain into dotted parts.
    Example: a.b.c  -> ["a", "b", "c"]
    Returns None if the base is not a Name.
    """
    parts: List[str] = []
    node = expr
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        parts.reverse()
        return parts
    return None

def attr_root_name_and_leaf(a: ast.Attribute) -> Tuple[Optional[str], str]:
    """
    For a possibly nested attribute, return (root_name, leaf_attr).
      - root_name: leftmost Name in the chain (or None if not present)
      - leaf_attr: the terminal attribute name (a.attr)
    Example: (self.view.widget).update  -> ("self", "update")
             (obj.sub.attr)             -> ("obj", "attr")
    """
    base = a.value
    while isinstance(base, ast.Attribute):
        base = base.value
    root = base.id if isinstance(base, ast.Name) else None
    return root, a.attr


# --------------- Additional pure-AST helpers ---------------

def has_dunder_getattr(source: str, tree: Optional[ast.AST] = None) -> bool:
    """
    Detect module-level `def __getattr__(name): ...` (PEP 562 dynamic facades).
    """
    try:
        t = tree or ast.parse(source)
    except Exception:
        return False

    # Only count module-scope definitions (body of Module)
    for n in getattr(t, "body", []) or []:
        if isinstance(n, ast.FunctionDef) and n.name == "__getattr__" and isinstance(getattr(n, "args", None), ast.arguments):
            return True
    return False


def node_source_segment(text: str, node: ast.AST) -> str:
    """
    Best-effort source slice for a node using lineno/end_lineno.
    Falls back to empty string if unavailable.
    """
    try:
        # Python's built-in helper when available
        seg = ast.get_source_segment(text, node)  # type: ignore[attr-defined]
        if isinstance(seg, str):
            return seg.rstrip("\n")
    except Exception:
        pass

    try:
        ln = getattr(node, "lineno", None)
        eln = getattr(node, "end_lineno", None) or ln
        if ln is None or eln is None:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[ln - 1 : eln]).rstrip()
    except Exception:
        return ""


def match_dynamic_import_literal(call: ast.Call) -> Optional[str]:
    """
    If `call` is a dynamic import of the forms:
      - __import__("pkg.sub")
      - importlib.import_module("pkg.sub")
    and the first argument is a literal string, return that string; else None.
    """
    try:
        # __import__("pkg.sub")
        if isinstance(call.func, ast.Name) and call.func.id == "__import__":
            if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                mod = call.args[0].value.strip()
                return mod or None

        # importlib.import_module("pkg.sub")
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "import_module"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "importlib"
        ):
            if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                mod = call.args[0].value.strip()
                return mod or None
    except Exception:
        return None

    return None


# --------------- Module indexing (defs/imports/refs/__all__) ---------------

class ModuleIndexCollector(ScopeAwareVisitor):
    """
    Collect module-scope:
      - definitions: top-level functions/classes, simple top-level assignments
      - imports: names introduced in module scope (import x as a; from m import y as b)
      - outgoing references: Name loads + base name of Attribute chains
      - __all__ = [...] literal exports
    Mirrors the behavior used by dead-code analysis.
    """
    def __init__(self) -> None:
        super().__init__()
        self.def_funcs: Dict[str, Tuple[int, int]] = {}
        self.def_classes: Dict[str, Tuple[int, int]] = {}
        self.def_vars: Dict[str, Tuple[int, int]] = {}
        self.imports: Dict[str, Tuple[int, int]] = {}  # local_name -> (lineno, col)
        self.refs: Set[str] = set()                    # simple names referenced in the module
        self.exports: Set[str] = set()                 # names listed in __all__

    # defs (module-scope only)
    def _on_functiondef(self, node: ast.FunctionDef) -> None:
        if self.at_module_scope():
            self.def_funcs[node.name] = (node.lineno, node.col_offset)

    def _on_asyncfunctiondef(self, node: ast.AsyncFunctionDef) -> None:
        if self.at_module_scope():
            self.def_funcs[node.name] = (node.lineno, node.col_offset)

    def _on_classdef(self, node: ast.ClassDef) -> None:
        if self.at_module_scope():
            self.def_classes[node.name] = (node.lineno, node.col_offset)

    # assignments (defs + __all__)
    def visit_Assign(self, node: ast.Assign) -> None:
        if self.at_module_scope():
            # simple top-level assignments
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.def_vars[t.id] = (t.lineno, t.col_offset)

            # literal __all__ exports
            try:
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__":
                        self.exports |= set(_literal_str_sequence(node.value))
            except Exception:
                pass

        self.generic_visit(node)

    # walrus: traverse only
    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.generic_visit(node)

    # imports (module scope only)
    def visit_Import(self, node: ast.Import) -> None:
        if self.at_module_scope():
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                self.imports[local] = (node.lineno, node.col_offset)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self.at_module_scope():
            for alias in node.names:
                local = alias.asname or alias.name
                self.imports[local] = (node.lineno, node.col_offset)

    # refs
    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.refs.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Count the base name of base.attr as a reference (conservative).
        root, _ = attr_root_name_and_leaf(node)
        if root is not None:
            self.refs.add(root)
        self.generic_visit(node)

# --------------- Data classes for detailed metadata ---------------

@dataclass
class ParameterInfo:
    """Function/method parameter with type hints."""
    name: str
    type_hint: Optional[str] = None
    default: Optional[str] = None
    kind: str = "positional_or_keyword"  # positional_only, keyword_only, var_positional, var_keyword


@dataclass
class DecoratorInfo:
    """Decorator with arguments."""
    name: str
    args: List[str] = field(default_factory=list)
    kwargs: Dict[str, str] = field(default_factory=dict)
    raw: str = ""
    lineno: int = 0


@dataclass
class MethodInfo:
    """Method metadata with signature."""
    name: str
    parameters: List[ParameterInfo] = field(default_factory=list)
    return_type: Optional[str] = None
    decorators: List[DecoratorInfo] = field(default_factory=list)
    is_async: bool = False
    is_static: bool = False
    is_classmethod: bool = False
    is_property: bool = False
    docstring: Optional[str] = None
    lineno: int = 0


@dataclass
class AttributeInfo:
    """Class attribute with type hint."""
    name: str
    type_hint: Optional[str] = None
    default: Optional[str] = None
    lineno: int = 0


@dataclass
class ClassInfo:
    """Detailed class metadata."""
    name: str
    bases: List[str] = field(default_factory=list)
    decorators: List[DecoratorInfo] = field(default_factory=list)
    methods: List[MethodInfo] = field(default_factory=list)
    attributes: List[AttributeInfo] = field(default_factory=list)
    docstring: Optional[str] = None
    is_dataclass: bool = False
    is_enum: bool = False
    lineno: int = 0


@dataclass
class FunctionInfo:
    """Detailed function metadata."""
    name: str
    parameters: List[ParameterInfo] = field(default_factory=list)
    return_type: Optional[str] = None
    decorators: List[DecoratorInfo] = field(default_factory=list)
    is_async: bool = False
    is_generator: bool = False
    docstring: Optional[str] = None
    lineno: int = 0


@dataclass
class GlobalInfo:
    """Global variable with type hint."""
    name: str
    type_hint: Optional[str] = None
    value: Optional[str] = None
    lineno: int = 0


# --------------- Extraction helper functions ---------------

def extract_type_hint(annotation: Optional[ast.expr]) -> Optional[str]:
    """Extract type hint as string from annotation node."""
    if annotation is None:
        return None
    try:
        return ast.unparse(annotation)
    except Exception:
        return None


def extract_decorators(decorator_list: List[ast.expr]) -> List[DecoratorInfo]:
    """Extract decorator information from decorator_list."""
    decorators = []
    for dec in decorator_list:
        try:
            raw = ast.unparse(dec)
            name = raw
            args: List[str] = []
            kwargs: Dict[str, str] = {}
            
            # Parse decorator structure
            if isinstance(dec, ast.Call):
                # Decorator with arguments: @decorator(arg1, key=value)
                if isinstance(dec.func, ast.Name):
                    name = dec.func.id
                elif isinstance(dec.func, ast.Attribute):
                    name = ast.unparse(dec.func)
                else:
                    name = ast.unparse(dec.func)
                
                # Extract positional args
                for arg in dec.args:
                    try:
                        args.append(ast.unparse(arg))
                    except Exception:
                        pass
                
                # Extract keyword args
                for kw in dec.keywords:
                    try:
                        key = kw.arg or ""
                        if key:  # Skip **kwargs
                            kwargs[key] = ast.unparse(kw.value)
                    except Exception:
                        pass
            
            elif isinstance(dec, ast.Name):
                # Simple decorator: @decorator
                name = dec.id
            elif isinstance(dec, ast.Attribute):
                # Attribute decorator: @module.decorator
                name = ast.unparse(dec)
            
            decorators.append(DecoratorInfo(
                name=name,
                args=args,
                kwargs=kwargs,
                raw=raw,
                lineno=getattr(dec, 'lineno', 0)
            ))
        except Exception:
            # Skip problematic decorators rather than fail
            pass
    
    return decorators


def extract_parameters(args: ast.arguments) -> List[ParameterInfo]:
    """Extract parameter information from function arguments."""
    params = []
    
    # Regular positional/keyword args
    num_args = len(args.args)
    num_defaults = len(args.defaults)
    # Pad with None for args without defaults
    defaults = [None] * (num_args - num_defaults) + list(args.defaults)
    
    for arg, default in zip(args.args, defaults):
        type_hint = extract_type_hint(arg.annotation)
        default_str = None
        if default:
            try:
                default_str = ast.unparse(default)
            except Exception:
                pass
        
        params.append(ParameterInfo(
            name=arg.arg,
            type_hint=type_hint,
            default=default_str,
            kind="positional_or_keyword"
        ))
    
    # *args
    if args.vararg:
        params.append(ParameterInfo(
            name=args.vararg.arg,
            type_hint=extract_type_hint(args.vararg.annotation),
            kind="var_positional"
        ))
    
    # Keyword-only args
    num_kwonly = len(args.kwonlyargs)
    num_kw_defaults = len(args.kw_defaults)
    # Pad with None
    kw_defaults_padded = [None] * (num_kwonly - num_kw_defaults) + list(args.kw_defaults)
    
    for arg, default in zip(args.kwonlyargs, kw_defaults_padded):
        type_hint = extract_type_hint(arg.annotation)
        default_str = None
        if default:
            try:
                default_str = ast.unparse(default)
            except Exception:
                pass
        
        params.append(ParameterInfo(
            name=arg.arg,
            type_hint=type_hint,
            default=default_str,
            kind="keyword_only"
        ))
    
    # **kwargs
    if args.kwarg:
        params.append(ParameterInfo(
            name=args.kwarg.arg,
            type_hint=extract_type_hint(args.kwarg.annotation),
            kind="var_keyword"
        ))
    
    return params


def parse_class_detailed(node: ast.ClassDef) -> ClassInfo:
    """Extract detailed class information from ClassDef node."""
    # Base classes
    bases = []
    for base in node.bases:
        try:
            bases.append(ast.unparse(base))
        except Exception:
            pass
    
    # Decorators
    decorators = extract_decorators(node.decorator_list)
    
    # Check special class types
    is_dataclass = any(
        "dataclass" in d.name.lower()
        for d in decorators
    )
    is_enum = any("Enum" in b for b in bases)
    
    # Extract methods and attributes
    methods = []
    attributes = []
    
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Method
            params = extract_parameters(item.args)
            return_type = extract_type_hint(item.returns)
            method_decorators = extract_decorators(item.decorator_list)
            
            # Check decorator-based method types
            is_static = any(d.name == "staticmethod" for d in method_decorators)
            is_classmethod = any(d.name == "classmethod" for d in method_decorators)
            is_property = any(d.name == "property" for d in method_decorators)
            
            # Check if generator
            is_generator = any(
                isinstance(n, (ast.Yield, ast.YieldFrom))
                for n in ast.walk(item)
            )
            
            methods.append(MethodInfo(
                name=item.name,
                parameters=params,
                return_type=return_type,
                decorators=method_decorators,
                is_async=isinstance(item, ast.AsyncFunctionDef),
                is_static=is_static,
                is_classmethod=is_classmethod,
                is_property=is_property,
                docstring=ast.get_docstring(item),
                lineno=item.lineno
            ))
        
        elif isinstance(item, ast.AnnAssign):
            # Annotated attribute: name: Type = value
            if isinstance(item.target, ast.Name):
                type_hint = extract_type_hint(item.annotation)
                default_str = None
                if item.value:
                    try:
                        default_str = ast.unparse(item.value)
                    except Exception:
                        pass
                
                attributes.append(AttributeInfo(
                    name=item.target.id,
                    type_hint=type_hint,
                    default=default_str,
                    lineno=item.lineno
                ))
        
        elif isinstance(item, ast.Assign):
            # Simple assignment: name = value
            for target in item.targets:
                if isinstance(target, ast.Name):
                    default_str = None
                    try:
                        default_str = ast.unparse(item.value)
                    except Exception:
                        pass
                    
                    attributes.append(AttributeInfo(
                        name=target.id,
                        type_hint=None,
                        default=default_str,
                        lineno=item.lineno
                    ))
    
    return ClassInfo(
        name=node.name,
        bases=bases,
        decorators=decorators,
        methods=methods,
        attributes=attributes,
        docstring=ast.get_docstring(node),
        is_dataclass=is_dataclass,
        is_enum=is_enum,
        lineno=node.lineno
    )


def parse_function_detailed(node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> FunctionInfo:
    """Extract detailed function information from FunctionDef node."""
    params = extract_parameters(node.args)
    return_type = extract_type_hint(node.returns)
    decorators = extract_decorators(node.decorator_list)
    
    # Check if generator
    is_generator = any(
        isinstance(n, (ast.Yield, ast.YieldFrom))
        for n in ast.walk(node)
    )
    
    return FunctionInfo(
        name=node.name,
        parameters=params,
        return_type=return_type,
        decorators=decorators,
        is_async=isinstance(node, ast.AsyncFunctionDef),
        is_generator=is_generator,
        docstring=ast.get_docstring(node),
        lineno=node.lineno
    )


# --------------- Enhanced collector ---------------

class EnhancedModuleSymbolCollector(ScopeAwareVisitor):
    """
    Enhanced collector that extracts detailed metadata.
    Maintains 100% backward compatibility with ModuleSymbolCollector.
    """
    def __init__(self, *, detailed: bool = True) -> None:
        super().__init__()
        # Backward-compatible simple lists (always populated)
        self.classes: list[str] = []
        self.functions: list[str] = []
        self.globals: list[str] = []
        self.all_exports: list[str] = []
        self.warnings: list[str] = []
        self.detailed = detailed
        self.classes_detailed: List[ClassInfo] = []
        self.functions_detailed: List[FunctionInfo] = []
        self.globals_detailed: List[GlobalInfo] = []
    
    def _on_classdef(self, node: ast.ClassDef) -> None:
        if self.at_module_scope():
            # Always populate simple list (backward compat)
            self.classes.append(node.name)
            
            # Optionally extract detailed metadata
            if self.detailed:
                try:
                    class_info = parse_class_detailed(node)
                    self.classes_detailed.append(class_info)
                except Exception:
                    # Fallback: add minimal class info
                    self.classes_detailed.append(ClassInfo(
                        name=node.name,
                        lineno=node.lineno
                    ))
    
    def _on_functiondef(self, node: ast.FunctionDef) -> None:
        if self.at_module_scope():
            # Always populate simple list (backward compat)
            self.functions.append(node.name)
            
            # Optionally extract detailed metadata
            if self.detailed:
                try:
                    func_info = parse_function_detailed(node)
                    self.functions_detailed.append(func_info)
                except Exception:
                    # Fallback: add minimal function info
                    self.functions_detailed.append(FunctionInfo(
                        name=node.name,
                        lineno=node.lineno
                    ))
    
    def _on_asyncfunctiondef(self, node: ast.AsyncFunctionDef) -> None:
        if self.at_module_scope():
            # Always populate simple list (backward compat)
            self.functions.append(node.name)
            
            # Optionally extract detailed metadata
            if self.detailed:
                try:
                    func_info = parse_function_detailed(node)
                    self.functions_detailed.append(func_info)
                except Exception:
                    # Fallback
                    self.functions_detailed.append(FunctionInfo(
                        name=node.name,
                        is_async=True,
                        lineno=node.lineno
                    ))
    
    def visit_Assign(self, node: ast.Assign) -> None:
        if self.at_module_scope():
            try:
                # __all__ = [...] (existing logic)
                if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                    names = _literal_str_sequence(node.value)
                    if names:
                        self.all_exports = names
                else:
                    # Regular module-level globals
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            # Always populate simple list (backward compat)
                            self.globals.append(t.id)
                            
                            # Optionally extract detailed metadata
                            if self.detailed:
                                value_str = None
                                try:
                                    value_str = ast.unparse(node.value)
                                except Exception:
                                    pass
                                
                                self.globals_detailed.append(GlobalInfo(
                                    name=t.id,
                                    type_hint=None,
                                    value=value_str,
                                    lineno=t.lineno
                                ))
            except Exception:
                self.warnings.append("__all__ could not be parsed")
        
        self.generic_visit(node)
    
    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self.at_module_scope():
            try:
                if isinstance(node.target, ast.Name):
                    if node.target.id == "__all__":
                        # Annotated __all__
                        names = _literal_str_sequence(node.value)
                        if names:
                            self.all_exports = names
                    else:
                        # Annotated global
                        # Always populate simple list (backward compat)
                        self.globals.append(node.target.id)
                        
                        # Optionally extract detailed metadata
                        if self.detailed:
                            type_hint = extract_type_hint(node.annotation)
                            value_str = None
                            if node.value:
                                try:
                                    value_str = ast.unparse(node.value)
                                except Exception:
                                    pass
                            
                            self.globals_detailed.append(GlobalInfo(
                                name=node.target.id,
                                type_hint=type_hint,
                                value=value_str,
                                lineno=node.target.lineno
                            ))
            except Exception:
                pass
        
        self.generic_visit(node)