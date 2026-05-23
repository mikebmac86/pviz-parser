# analyzer/ts/extract_crosstalk.py
from __future__ import annotations
import re
from typing import List, Optional, Tuple

# Import your schema helpers (from the combined template we made)
# Adjust the import path to wherever you placed it.
from core.build_pipeline.JS_P_Crosstalk import (
    CandidateV1,
    make_candidate,
    join_http,
    join_env,
    join_rpc,
    norm_path,
)

# If you don't want a hard dependency yet, you can inline join builders here.


def _node_text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _string_literal_value(source: bytes, node) -> Optional[str]:
    txt = _node_text(source, node).strip()
    if len(txt) >= 2 and ((txt[0] == txt[-1] == '"') or (txt[0] == txt[-1] == "'")):
        return txt[1:-1]
    return None


def _loc(node) -> Optional[Tuple[int, int]]:
    try:
        return (node.start_point[0], node.start_point[1])
    except Exception:
        return None


def _first_string_descendant(node):
    stack = list(getattr(node, "children", []) or [])
    while stack:
        cur = stack.pop()
        if cur.type == "string":
            return cur
        stack.extend(getattr(cur, "children", []) or [])
    return None


def _first_identifier_descendant(node):
    """
    Best-effort to find an identifier node under a subtree.
    Useful for process.env.VAR, import.meta.env.VAR style.
    """
    stack = list(getattr(node, "children", []) or [])
    while stack:
        cur = stack.pop()
        if cur.type in ("identifier", "property_identifier"):
            return cur
        stack.extend(getattr(cur, "children", []) or [])
    return None


def _callee_text(source: bytes, call_expr) -> str:
    callee = call_expr.child_by_field_name("function")
    if callee is None:
        # some grammars use 'callee'
        callee = call_expr.child_by_field_name("callee")
    return _node_text(source, callee).strip() if callee is not None else ""


def _get_arguments_node(call_expr):
    args = call_expr.child_by_field_name("arguments")
    if args is None:
        # some grammars use 'arguments' but nested differently; keep it simple
        return None
    return args


def _first_arg_string_literal(source: bytes, call_expr) -> Optional[str]:
    args = _get_arguments_node(call_expr)
    if args is None:
        return None
    s = _first_string_descendant(args)
    return _string_literal_value(source, s) if s is not None else None


def _extract_axios_method_from_callee(callee_txt: str) -> Optional[str]:
    """
    Accept:
      axios.get(...)
      axios.post(...)
      axios.put(...)
      axios.patch(...)
      axios.delete(...)
    """
    callee_txt = callee_txt.strip()
    # quick path: axios.get
    if callee_txt.startswith("axios."):
        meth = callee_txt.split(".", 1)[1].split("(", 1)[0].strip()
        m = meth.upper()
        if m in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            return m
    return None


def _extract_fetch_candidate(source: bytes, n) -> Optional[CandidateV1]:
    callee_txt = _callee_text(source, n)
    if callee_txt != "fetch":
        return None

    url = _first_arg_string_literal(source, n)
    if not url:
        return None

    # fetch() defaults to GET unless options.method present; v1 doesn’t attempt to read options object
    join = join_http("GET", url, canon_params=True)
    rowcol = _loc(n)
    where = {"line": rowcol[0], "col": rowcol[1]} if rowcol else {}
    return make_candidate(
        kind="http_call",
        join=join,
        raw=f"fetch({url!r})",
        meta={"method": "GET", "path": norm_path(url)},
        where=where,
        syntax="fetch",
        text=_node_text(source, n)[:200],
        confidence=1.0,
    )


def _extract_axios_direct_candidate(source: bytes, n) -> Optional[CandidateV1]:
    callee_txt = _callee_text(source, n)
    meth = _extract_axios_method_from_callee(callee_txt)
    if not meth:
        return None

    url = _first_arg_string_literal(source, n)
    if not url:
        return None

    join = join_http(meth, url, canon_params=True)
    rowcol = _loc(n)
    where = {"line": rowcol[0], "col": rowcol[1]} if rowcol else {}
    return make_candidate(
        kind="http_call",
        join=join,
        raw=f"{callee_txt}({url!r})",
        meta={"method": meth, "path": norm_path(url)},
        where=where,
        syntax="axios",
        text=_node_text(source, n)[:200],
        confidence=1.0,
    )


def _extract_axios_config_candidate(source: bytes, n) -> Optional[CandidateV1]:
    """
    axios({ url: "...", method: "post" })
    v1: only handles literal url + literal method.
    """
    callee_txt = _callee_text(source, n)
    if callee_txt != "axios":
        return None

    args = _get_arguments_node(n)
    if args is None:
        return None

    # Find object literal under arguments
    obj = None
    for ch in getattr(args, "children", []) or []:
        if ch.type in ("object", "object_literal", "object_expression"):
            obj = ch
            break
    if obj is None:
        return None

    url: Optional[str] = None
    method: Optional[str] = None

    # This is grammar-dependent; we do best-effort by scanning for "pair" nodes
    stack = [obj]
    while stack:
        cur = stack.pop()
        if cur.type in ("pair", "property", "object_pair"):
            # try to read key + value as text
            txt = _node_text(source, cur)
            # very light heuristic (safe for v1): look for url: "..." and method: "..."
            if url is None and re.search(r"\burl\s*:", txt):
                s = _first_string_descendant(cur)
                url = _string_literal_value(source, s) if s is not None else url
            if method is None and re.search(r"\bmethod\s*:", txt):
                s = _first_string_descendant(cur)
                m = _string_literal_value(source, s) if s is not None else None
                if m:
                    method = m.upper()
        stack.extend(getattr(cur, "children", []) or [])

    if not url:
        return None

    join = join_http(method or "GET", url, canon_params=True)
    rowcol = _loc(n)
    where = {"line": rowcol[0], "col": rowcol[1]} if rowcol else {}
    return make_candidate(
        kind="http_call",
        join=join,
        raw="axios({...})",
        meta={"method": (method or "GET"), "path": norm_path(url)},
        where=where,
        syntax="axios",
        text=_node_text(source, n)[:200],
        confidence=0.9 if method else 0.75,
    )


def _extract_env_candidate_from_member(source: bytes, n) -> Optional[CandidateV1]:
    """
    Detect:
      process.env.API_BASE_URL
      import.meta.env.VITE_API_URL
    This is grammar-dependent; we use callee/member text heuristics v1.
    """
    # Candidate is not a call_expression; it’s a member_expression-ish node.
    txt = _node_text(source, n).strip()

    # Fast path: look for process.env.X or import.meta.env.X with trailing identifier
    if txt.startswith("process.env."):
        var = txt[len("process.env.") :].split()[0].strip()
        if var:
            join = join_env(var)
            rowcol = _loc(n)
            where = {"line": rowcol[0], "col": rowcol[1]} if rowcol else {}
            return make_candidate(
                kind="env_ref",
                join=join,
                raw=txt,
                meta={"var": var, "system": "node"},
                where=where,
                syntax="process.env",
                text=txt[:200],
                confidence=1.0,
            )

    if txt.startswith("import.meta.env."):
        var = txt[len("import.meta.env.") :].split()[0].strip()
        if var:
            join = join_env(var)
            rowcol = _loc(n)
            where = {"line": rowcol[0], "col": rowcol[1]} if rowcol else {}
            return make_candidate(
                kind="env_ref",
                join=join,
                raw=txt,
                meta={"var": var, "system": "vite"},
                where=where,
                syntax="import.meta.env",
                text=txt[:200],
                confidence=1.0,
            )

    return None


def _extract_trpc_candidate(source: bytes, n) -> Optional[CandidateV1]:
    """
    Detect common tRPC pattern by callee text:
      trpc.<router>.<proc>.useQuery(...)
      trpc.<router>.<proc>.useMutation(...)
    v1: match only when callee text is present and stable.
    """
    callee_txt = _callee_text(source, n)
    if not callee_txt.startswith("trpc."):
        return None
    if not (callee_txt.endswith(".useQuery") or callee_txt.endswith(".useMutation")):
        return None

    # Strip suffix
    base = callee_txt.rsplit(".", 1)[0]  # trpc.user.getById
    # Turn "trpc.user.getById" into "trpc:user.getById"
    qname = "trpc:" + base[len("trpc.") :]
    join = join_rpc(qname)

    rowcol = _loc(n)
    where = {"line": rowcol[0], "col": rowcol[1]} if rowcol else {}
    return make_candidate(
        kind="rpc_call",
        join=join,
        raw=_node_text(source, n)[:200],
        meta={"name": qname, "system": "trpc"},
        where=where,
        syntax="trpc",
        text=_node_text(source, n)[:200],
        confidence=0.95,
    )


def extract_crosstalk_candidates_ts_v1(*, tree, source: bytes) -> List[CandidateV1]:
    """
    Walk the tree and extract TS/JS crosstalk candidates (v1).

    v1 rules:
      - emit only when grounded in established syntax AND stable tokens
      - only accepts plain string literals for URLs (no template literal expansion yet)
      - does not attempt deep object parsing except light axios({...}) heuristic
    """
    out: List[CandidateV1] = []
    root = tree.root_node

    def walk(n):
        # --- call-based patterns ---
        if n.type == "call_expression":
            c = (
                _extract_fetch_candidate(source, n)
                or _extract_axios_direct_candidate(source, n)
                or _extract_axios_config_candidate(source, n)
                or _extract_trpc_candidate(source, n)
            )
            if c:
                out.append(c)

        # --- member-expression-ish patterns (env) ---
        # Different grammars use different node types; include a small set.
        if n.type in ("member_expression", "subscript_expression", "identifier", "property_identifier"):
            c2 = _extract_env_candidate_from_member(source, n)
            if c2:
                out.append(c2)

        for ch in n.children:
            walk(ch)

    walk(root)

    # Dedupe by (kind, join, where.line, where.col) to keep noise down
    seen = set()
    deduped: List[CandidateV1] = []
    for c in out:
        w = c.get("where") or {}
        k = (c.get("kind"), c.get("join"), w.get("line"), w.get("col"))
        if k in seen:
            continue
        seen.add(k)
        deduped.append(c)

    return deduped
