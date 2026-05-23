# analyzer/python/extract_crosstalk.py
from __future__ import annotations

"""
Python Crosstalk Candidate Extraction (v1)

Designed to be "injected" into the existing Python analyzer without modifying
the current parser logic, similar to the TS approach.

- Conservative: only emits candidates from established syntax + literal strings
- Non-fatal: callers should wrap in try/except; this module itself avoids raising
  for normal parse variance.
- Output: List[CandidateV1] suitable for:
    nodes[node_id]["facts"]["crosstalk_candidates_py_v1"] = candidates

v1 coverage (high ROI):
  - env_ref: os.getenv("X"), os.environ.get("X"), os.environ["X"]
  - http_route: FastAPI decorators: @app.get("/x"), @router.post("/x")
                Flask: @app.route("/x", methods=[...]), @bp.route(...)
"""

import ast
from typing import List, Optional, Tuple, Any

from core.build_pipeline.P_JS_Crosstalk import (
    CandidateV1,
    make_candidate,
    join_env,
    join_http,
    norm_path,
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _safe_parse(source: str) -> Optional[ast.AST]:
    try:
        return ast.parse(source)
    except Exception:
        return None


def _loc(n: ast.AST) -> Optional[Tuple[int, int]]:
    line = getattr(n, "lineno", None)
    col = getattr(n, "col_offset", None)
    if isinstance(line, int) and isinstance(col, int):
        # tree-sitter was 0-based; python ast lineno is 1-based.
        # Keep Python-native (1-based) for now; resolver doesn't care.
        return (line, col)
    return None


def _const_str(node: ast.AST) -> Optional[str]:
    # py3.8+: ast.Constant
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # legacy: ast.Str
    if hasattr(ast, "Str") and isinstance(node, ast.Str):  # type: ignore[attr-defined]
        return node.s  # type: ignore[attr-defined]
    return None


def _name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    return None


def _attr_chain(node: ast.AST) -> Optional[List[str]]:
    """
    Convert an AST node representing an attribute chain into tokens.

    Examples:
      os.getenv              -> ["os", "getenv"]
      os.environ.get         -> ["os", "environ", "get"]
      app.get                -> ["app", "get"]
      router.post            -> ["router", "post"]
      blueprint.route        -> ["blueprint", "route"]
    """
    parts: List[str] = []
    cur = node
    while True:
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
            continue
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            break
        # could be Call/Subscript/etc; stop
        return None
    return list(reversed(parts))


def _call_func_chain(call: ast.Call) -> Optional[List[str]]:
    return _attr_chain(call.func)


def _decorator_chain(dec: ast.AST) -> Optional[List[str]]:
    """
    Decorators can be:
      - ast.Call (e.g., @app.get("/x"))
      - ast.Attribute (rare; @bp.route)
      - ast.Name
    We primarily care about ast.Call.
    """
    if isinstance(dec, ast.Call):
        return _attr_chain(dec.func)
    return _attr_chain(dec) or (_name(dec) and [_name(dec)])


def _snippet(source: str, n: ast.AST, limit: int = 180) -> str:
    """
    Best-effort snippet. If ast.get_source_segment is available, use it.
    Otherwise return empty string.
    """
    try:
        seg = ast.get_source_segment(source, n)  # py3.8+
        if isinstance(seg, str):
            seg = seg.strip().replace("\n", " ")
            return seg[:limit]
    except Exception:
        pass
    return ""


# -----------------------------------------------------------------------------
# Extractors
# -----------------------------------------------------------------------------

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _emit_env_ref(*, source: str, call: ast.Call) -> Optional[CandidateV1]:
    """
    Detect:
      os.getenv("VAR")
      os.environ.get("VAR")
    """
    chain = _call_func_chain(call)
    if not chain:
        return None

    # os.getenv("X")
    if chain == ["os", "getenv"]:
        if not call.args:
            return None
        var = _const_str(call.args[0])
        if not var:
            return None
        rc = _loc(call)
        where = {"line": rc[0], "col": rc[1]} if rc else {}
        return make_candidate(
            kind="env_ref",
            join=join_env(var),
            raw=_snippet(source, call) or "os.getenv(...)",
            meta={"var": var, "system": "os"},
            where=where,
            syntax="os.getenv",
            text=_snippet(source, call),
            confidence=1.0,
        )

    # os.environ.get("X")
    if chain == ["os", "environ", "get"]:
        if not call.args:
            return None
        var = _const_str(call.args[0])
        if not var:
            return None
        rc = _loc(call)
        where = {"line": rc[0], "col": rc[1]} if rc else {}
        return make_candidate(
            kind="env_ref",
            join=join_env(var),
            raw=_snippet(source, call) or "os.environ.get(...)",
            meta={"var": var, "system": "os"},
            where=where,
            syntax="os.environ.get",
            text=_snippet(source, call),
            confidence=1.0,
        )

    return None


def _emit_env_subscript(*, source: str, sub: ast.Subscript) -> Optional[CandidateV1]:
    """
    Detect:
      os.environ["VAR"]
    """
    # Expect value to be os.environ
    chain = _attr_chain(sub.value)
    if chain != ["os", "environ"]:
        return None

    # slice differs across py versions: Constant/Index
    sl = sub.slice
    # py<3.9: ast.Index(value=...)
    if hasattr(ast, "Index") and isinstance(sl, ast.Index):  # type: ignore[attr-defined]
        sl = sl.value  # type: ignore[attr-defined]

    var = _const_str(sl)
    if not var:
        return None

    rc = _loc(sub)
    where = {"line": rc[0], "col": rc[1]} if rc else {}
    return make_candidate(
        kind="env_ref",
        join=join_env(var),
        raw=_snippet(source, sub) or 'os.environ["..."]',
        meta={"var": var, "system": "os"},
        where=where,
        syntax="os.environ[]",
        text=_snippet(source, sub),
        confidence=1.0,
    )


def _decorator_is_fastapi_method(chain: List[str]) -> Optional[str]:
    """
    Heuristic:
      @app.get("/x") or @router.post("/x")
    We don't require app/router naming; we just detect tail method.
    """
    if not chain:
        return None
    tail = chain[-1].lower()
    if tail in _HTTP_METHODS:
        return tail.upper()
    return None


def _emit_fastapi_route(*, source: str, dec_call: ast.Call, method: str) -> Optional[CandidateV1]:
    """
    @app.get("/path")
    @router.post("/path")
    """
    if not dec_call.args:
        return None
    path = _const_str(dec_call.args[0])
    if not path:
        return None

    join = join_http(method, path, canon_params=True)
    rc = _loc(dec_call)
    where = {"line": rc[0], "col": rc[1]} if rc else {}

    return make_candidate(
        kind="http_route",
        join=join,
        raw=_snippet(source, dec_call) or f"@*.{method.lower()}(...)",
        meta={"method": method, "path": norm_path(path), "framework": "fastapi"},
        where=where,
        syntax="decorator",
        text=_snippet(source, dec_call),
        confidence=1.0,
    )


def _emit_flask_route(*, source: str, dec_call: ast.Call) -> List[CandidateV1]:
    """
    @app.route("/path", methods=["GET","POST"])
    If methods not provided, Flask defaults to GET.
    """
    out: List[CandidateV1] = []

    if not dec_call.args:
        return out
    path = _const_str(dec_call.args[0])
    if not path:
        return out

    # Find "methods" kw arg if present
    methods: List[str] = []
    for kw in dec_call.keywords or []:
        if kw.arg != "methods":
            continue
        val = kw.value
        # methods=["GET","POST"] or ("GET","POST")
        if isinstance(val, (ast.List, ast.Tuple)):
            for elt in val.elts:
                s = _const_str(elt)
                if s:
                    methods.append(s.upper())
        # methods="GET" (rare)
        else:
            s = _const_str(val)
            if s:
                methods.append(s.upper())

    if not methods:
        methods = ["GET"]

    rc = _loc(dec_call)
    where = {"line": rc[0], "col": rc[1]} if rc else {}

    for m in methods:
        join = join_http(m, path, canon_params=True)
        out.append(
            make_candidate(
                kind="http_route",
                join=join,
                raw=_snippet(source, dec_call) or "@*.route(...)",
                meta={"method": m, "path": norm_path(path), "framework": "flask"},
                where=where,
                syntax="decorator",
                text=_snippet(source, dec_call),
                confidence=1.0,
            )
        )
    return out


def _decorator_is_flask_route(chain: List[str]) -> bool:
    """
    Heuristic:
      @app.route(...)
      @bp.route(...)
      @blueprint.route(...)
    """
    return bool(chain) and chain[-1] == "route"


# -----------------------------------------------------------------------------
# Public entrypoint
# -----------------------------------------------------------------------------

def extract_crosstalk_candidates_py_v1(
    *,
    source: str,
    rel_path: str = "",
) -> List[CandidateV1]:
    """
    Extract Python crosstalk candidates (v1) from source text.

    Designed to be injected: caller provides source text, and this returns candidates.
    The caller is responsible for attaching them into nodefacts facts.
    """
    tree = _safe_parse(source)
    if tree is None:
        return []

    out: List[CandidateV1] = []

    # Walk the AST
    class V(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> Any:
            # env_ref
            c = _emit_env_ref(source=source, call=node)
            if c:
                out.append(c)
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> Any:
            c = _emit_env_subscript(source=source, sub=node)
            if c:
                out.append(c)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            self._handle_decorators(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
            self._handle_decorators(node)
            self.generic_visit(node)

        def _handle_decorators(self, fn: ast.AST) -> None:
            decs = getattr(fn, "decorator_list", None) or []
            for dec in decs:
                if not isinstance(dec, ast.Call):
                    continue

                chain = _decorator_chain(dec) or []
                if not chain:
                    continue

                # FastAPI style: @*.get("/x"), @*.post("/x"), etc.
                meth = _decorator_is_fastapi_method(chain)
                if meth:
                    c2 = _emit_fastapi_route(source=source, dec_call=dec, method=meth)
                    if c2:
                        out.append(c2)
                    continue

                # Flask style: @*.route("/x", methods=[...])
                if _decorator_is_flask_route(chain):
                    out.extend(_emit_flask_route(source=source, dec_call=dec))

    V().visit(tree)

    # Deduplicate by (kind, join, line, col)
    seen = set()
    deduped: List[CandidateV1] = []
    for c in out:
        w = c.get("where") or {}
        k = (c.get("kind"), c.get("join"), w.get("line"), w.get("col"))
        if k in seen:
            continue
        seen.add(k)
        deduped.append(c)

    # Optionally annotate file in where (resolver-friendly)
    if rel_path:
        for c in deduped:
            c.setdefault("where", {})
            if isinstance(c["where"], dict) and "file" not in c["where"]:
                c["where"]["file"] = rel_path

    return deduped
