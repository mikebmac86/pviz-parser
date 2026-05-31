"""
Python Crosstalk Candidates v1
=============================

This module mirrors the TS/JS crosstalk contract:
  - emit *annotations* (facts), not edges
  - each candidate has a single canonical JOIN KEY ("join")
  - join formats are intentionally identical across languages, so Level 2 can match
    by simple intersection of join keys.

Storage convention (recommended):
  nodes[<node_id>].facts["crosstalk_candidates_py_v1"] = List[CandidateV1]

Non-goals (v1):
  - No speculative cross-language edges
  - No dynamic evaluation of values
  - Keep extraction conservative and auditable

Known Limitations (v1):
  - No Django urlpatterns parsing (v1.1+)
  - No Flask Blueprint mount point tracking (affects final path resolution)
  - No pydantic Settings model field extraction
  - Limited f-string interpolation analysis (confidence scoring handles this)
  - No GraphQL schema AST parsing (resolver names only)
  - No automatic framework version detection
  - Path parameters: :id vs :userId treated as equivalent (unified to :id)
  - No detection of dynamically registered routes
"""

from __future__ import annotations

SCHEMA_VERSION = "1.0.0"

from hashlib import sha1
import re
from typing import Dict, List, Literal, Optional, TypedDict


# ============================================================================
# Program-facing schema (matchable)
# ============================================================================

CandidateKind = Literal[
    "http_route",     # inbound route definition in Python web framework
    "env_ref",        # os.getenv / environ / settings
    "rpc_decl",       # server-side RPC endpoint name (tRPC analogs vary; keep generic)
    "openapi_decl",   # openapi.json / docs endpoints / schema exposure
    "socket_event",   # socket.io / websockets events (server side)
    "queue_event",    # kafka/rabbit/sns event names
    "graphql_op",     # GraphQL schema/resolver operation names (if detectable)
]

HTTPMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "*"]


class SrcLoc(TypedDict, total=False):
    """Source location information."""
    file: str
    line: int
    col: int


class Evidence(TypedDict, total=False):
    """
    Evidence supporting the candidate detection.
    
    Fields:
      syntax: The syntactic construct detected (e.g., "@app.get", "os.getenv")
      text: Short source code snippet (<200 chars recommended)
      confidence: Float 0.0-1.0 indicating detection certainty (see confidence guide below)
      ast_node_type: Optional AST node type (e.g., "FunctionDef", "Decorator", "Call")
      dynamic_parts: List of dynamic/interpolated segments (e.g., f-string parts)
      framework_version: Optional framework version if detectable
    """
    syntax: str
    text: str
    confidence: float
    ast_node_type: str
    dynamic_parts: List[str]
    framework_version: str


class CandidateV1(TypedDict, total=False):
    """
    Minimal machine-joinable candidate record.

    Required for matching:
      - kind
      - join

    Recommended:
      - raw
      - meta (method/path/var/event/framework/etc)
      - where (file/line/col)
      - evidence (syntax/snippet/confidence) for auditability
    
    Optional:
      - schema_version: For forward compatibility tracking
    """
    kind: CandidateKind
    join: str
    raw: str
    meta: Dict[str, str]
    where: SrcLoc
    evidence: Evidence
    schema_version: str


# ---------------------------------------------------------------------------
# Canonical JOIN formats (MUST match TS/JS side)
# ---------------------------------------------------------------------------
#
# http_route:
#   join = "http:<METHOD>:/normalized/path"
#   meta: {"method": "...", "path": "...", "framework": "fastapi|flask|django|starlette|other"}
#   example: "http:GET:/api/users"
#
# env_ref:
#   join = "env:<VAR_NAME>"
#   meta: {"var": "...", "system": "os|dotenv|pydantic|django|other"}
#   example: "env:API_BASE_URL"
#
# rpc_decl:
#   join = "rpc:<qualified_name>"
#   meta: {"name": "...", "system": "grpc|jsonrpc|custom|other"}
#   example: "rpc:UserService.GetUser"
#
# openapi_decl:
#   join = "openapi:<type>:<id>"
#   meta: {"type": "schema|docs|component", "id": "...", "framework": "..."}
#   examples:
#     - "openapi:schema:/openapi.json" (schema document)
#     - "openapi:docs:/docs" (documentation UI endpoint)
#     - "openapi:component:#/components/schemas/User" (schema component reference)
#
# socket_event:
#   join = "socket:event:<name>"
#   meta: {"event": "...", "dir": "emit|on", "system": "socketio|ws|other"}
#   example: "socket:event:user.created"
#
# queue_event:
#   join = "queue:event:<name>"
#   meta: {"event": "...", "system": "kafka|amqp|sns|custom|other"}
#   example: "queue:event:user.created"
#
# graphql_op:
#   join = "graphql:<op_type>:<name_or_hash>"
#   meta: {"op_type": "query|mutation|subscription", "name": "...?", "hash": "..."}
#   examples:
#     - "graphql:query:GetUser" (named operation)
#     - "graphql:query:sha1:abcd1234" (anonymous operation, hash fallback)
# ---------------------------------------------------------------------------


# ============================================================================
# Confidence Scoring Guide (Python)
# ============================================================================
#
# Use these guidelines when setting evidence["confidence"]:
#
# 1.0 - Literal decorator or string constant:
#       @app.get('/api/users')
#       os.getenv('API_KEY')
#       @socketio.on('user.created')
#       @app.route('/users', methods=['GET'])
#
# 0.9 - Decorator with f-string containing clear param pattern:
#       @app.get(f'/api/users/{user_id}')  # param-like interpolation
#       @app.post(f'/posts/{post_id}/comments')
#
# 0.8 - Decorator with const/settings interpolation:
#       @app.get(f'{BASE_PATH}/users')  # if BASE_PATH is const/imported constant
#       @app.get(settings.USERS_PATH)  # if settings is known config object
#
# 0.7 - Path from const concatenation:
#       path = BASE + '/api/users'
#       @app.get(path)  # where BASE is traceable constant
#
# 0.6 - Path from settings/config object property:
#       @app.get(config.routes.users)  # if config is known configuration
#
# 0.5 - Urlpatterns entry with literal path:
#       urlpatterns = [path('users/', view)]  # Django (v1.1+)
#
# 0.4 - Dynamic route registration with some static parts:
#       app.add_route('/api/' + endpoint, handler)  # partial static content
#
# 0.3 - Highly dynamic but pattern-detectable:
#       @app.get(build_route('users'))  # call to known route builder
#
# <0.3 - Too dynamic to emit reliably (SKIP in v1 for quality):
#        @app.get(route_var)  # unknown variable with no static analysis path
#        os.getenv(dynamic_key)  # runtime-only determinable
#        app.add_route(dynamic_path, handler)  # fully dynamic
#
# Framework-specific notes:
# - FastAPI decorators: usually 1.0 (literal paths, framework encourages literals)
# - Flask decorators: 0.9-1.0 (may have variable mounting, blueprint prefixes)
# - Django urlpatterns: 0.5-1.0 (depends on path() vs re_path(), defer to v1.1)
# - Starlette: similar to FastAPI (0.9-1.0)
#
# Path parameter styles (all normalize to :id):
# - FastAPI: {user_id} -> :id (confidence 1.0)
# - Flask: <int:user_id> or <user_id> -> :id (confidence 1.0)
# - Django: <int:pk> -> :id (confidence 0.5-1.0, v1.1+)
#
# ============================================================================


# ============================================================================
# Filled normalization helpers (must mirror TS behavior for joinability)
# ============================================================================

_MULTI_SLASH = re.compile(r"/{2,}")
_TRAILING_SLASH = re.compile(r"(.+)/+$")
_QUERY_HASH = re.compile(r"[?#].*$")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
_INT_RE = re.compile(r"^\d+$")
_HEX_RE = re.compile(r"^(0x)?[0-9a-fA-F]+$")


def norm_path(path: str, *, decode_percent: bool = False, strict: bool = False) -> str:
    """
    Normalize a URL/path-ish string into a stable joinable form.

    Steps:
      - strip querystring/hash
      - extract path from full URL (best-effort, handles http(s)://)
      - ensure leading slash
      - collapse multiple slashes
      - remove trailing slash (except root "/")
      - optionally decode percent-encoding

    Args:
        path: Raw path or URL string
        decode_percent: If True, decode %XX sequences before normalizing
        strict: If True, raise ValueError on suspicious patterns (path traversal, etc.)

    Returns:
        Normalized path starting with '/', or '/' if empty/invalid

    Raises:
        ValueError: If strict=True and path contains suspicious patterns
    """
    p = (path or "").strip()
    
    # Handle empty/whitespace
    if not p:
        return "/"
    
    # Strict mode checks
    if strict:
        if ".." in p:
            raise ValueError(f"Path traversal pattern detected: {p}")
    
    # Strip querystring and hash
    p = _QUERY_HASH.sub("", p)

    # Extract path from full URL (improved regex to handle more cases)
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+(/.*)$", p)
    if m:
        p = m.group(1)
    else:
        # Check for URL without path (e.g., "http://api.example.com")
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+$", p):
            p = "/"

    # Ensure leading slash
    if not p.startswith("/"):
        p = "/" + p

    # Optionally decode percent-encoding
    if decode_percent:
        try:
            from urllib.parse import unquote
            p = unquote(p)
        except ImportError:
            pass  # Python 2 fallback, skip decoding

    # Collapse multiple slashes
    p = _MULTI_SLASH.sub("/", p)

    # Remove trailing slash (except for root)
    if p != "/":
        p2 = _TRAILING_SLASH.sub(r"\1", p)
        p = p2 if p2 else p

    return p


def canon_path_params(path: str, *, hex_threshold: int = 12) -> str:
    """
    Normalize obvious ID segments and unify parameter patterns.
    
    Transformations:
      - Pure integers -> ':id'
      - UUIDs -> ':id'
      - Long hex tokens (>= hex_threshold) -> ':id'
      - Named params (various formats) -> ':id' (unified)
    
    This unification ensures cross-language matching works even when
    parameter names differ (e.g., FastAPI {user_id} matches TS :userId).
    
    Supported parameter formats:
      - Express/generic: :userId -> :id
      - FastAPI: {user_id} -> :id
      - Flask: <int:user_id> or <user_id> -> :id
      - Django: <int:pk> -> :id (v1.1+)

    Args:
        path: Normalized path
        hex_threshold: Minimum length for hex strings to be considered IDs (default 12)

    Returns:
        Path with ID-like segments replaced with ':id'
    
    Note:
        This means '/users/:id' and '/users/:userId' will both become '/users/:id'
        and thus match each other. This is intentional for cross-language joining.
    """
    p = norm_path(path)
    if p in ("/", ""):
        return "/"

    parts = p.split("/")
    out: List[str] = []
    for seg in parts:
        if seg == "":
            out.append("")
            continue
        
        # Unify all param-style segments to :id
        # Express/generic style: :userId
        if seg.startswith(":"):
            out.append(":id")
            continue
        
        # FastAPI style: {user_id}
        if seg.startswith("{") and seg.endswith("}"):
            out.append(":id")
            continue
        
        # Flask style: <int:user_id> or <user_id> or <path:filepath>
        if seg.startswith("<") and seg.endswith(">"):
            out.append(":id")
            continue
        
        # Replace pure integers
        if _INT_RE.match(seg):
            out.append(":id")
            continue
        
        # Replace UUIDs
        if _UUID_RE.match(seg):
            out.append(":id")
            continue
        
        if len(seg) >= hex_threshold and _HEX_RE.match(seg):
            out.append(":id")
            continue
        
        out.append(seg)
    
    return "/".join(out) or "/"


def norm_http_method(method: Optional[str]) -> str:
    """
    Normalize HTTP method to uppercase.

    Returns:
        Uppercase method name, or "*" if empty/invalid
    
    Allows standard methods + any uppercase token for custom methods.
    """
    m = (method or "").strip().upper()
    if not m:
        return "*"
    
    # Allow any uppercase alphabetic token (RFC 9110 compliant)
    if re.match(r'^[A-Z]+$', m):
        return m
    
    return "*"  # Invalid method format


# ============================================================================
# Join builders (strict contract)
# ============================================================================
#
# Note on canon_params defaults:
#   - http_route: defaults to True (HTTP paths often have IDs: /users/123 -> /users/:id)
#
# Rationale: HTTP route definitions frequently contain path parameters that should be
# normalized for matching across languages. FastAPI {user_id}, Flask <user_id>, and
# TS :userId should all match as :id.
# ============================================================================

def join_http(method: Optional[str], path: str, *, canon_params: bool = True) -> str:
    """
    Build join key for HTTP routes.
    
    Args:
        method: HTTP method (GET, POST, etc.)
        path: URL path
        canon_params: If True, normalize ID-like segments to :id (default: True)
    
    Returns:
        Join key in format "http:<METHOD>:/normalized/path"
    """
    m = norm_http_method(method)
    p = canon_path_params(path) if canon_params else norm_path(path)
    return f"http:{m}:{p}"


def join_env(var: str) -> str:
    """Build join key for environment variable."""
    v = (var or "").strip()
    return f"env:{v}"


def join_rpc(name: str) -> str:
    """Build join key for RPC declaration."""
    n = (name or "").strip()
    return f"rpc:{n}"


def join_openapi(type_: str, id_: str) -> str:
    """
    Build join key for OpenAPI declaration.
    
    Args:
        type_: Declaration type (schema|docs|component)
        id_: Identifier (path to schema, docs endpoint, component ref)
    
    Returns:
        Join key in format "openapi:<type>:<id>"
    """
    t = (type_ or "").strip()
    i = (id_ or "").strip()
    return f"openapi:{t}:{i}"


def join_socket(event: str) -> str:
    """Build join key for socket event."""
    e = (event or "").strip()
    return f"socket:event:{e}"


def join_queue(event: str) -> str:
    """Build join key for queue/message event."""
    e = (event or "").strip()
    return f"queue:event:{e}"


def join_graphql(op_type: str, name_or_hash: str) -> str:
    """Build join key for GraphQL operation."""
    ot = (op_type or "").strip()
    noh = (name_or_hash or "").strip()
    return f"graphql:{ot}:{noh}"


# ============================================================================
# Evidence + convenience constructor
# ============================================================================

def make_candidate(
    *,
    kind: CandidateKind,
    join: str,
    raw: str = "",
    meta: Optional[Dict[str, str]] = None,
    where: Optional[SrcLoc] = None,
    syntax: str = "",
    text: str = "",
    confidence: float = 1.0,
    ast_node_type: str = "",
    dynamic_parts: Optional[List[str]] = None,
    framework_version: str = "",
) -> CandidateV1:
    """
    Build a CandidateV1 in a consistent shape.
    
    Args:
        kind: Candidate type
        join: Canonical join key
        raw: Original source expression
        meta: Additional metadata (method, path, framework, etc.)
        where: Source location
        syntax: Detected syntax pattern (e.g., "@app.get", "os.getenv")
        text: Short code snippet
        confidence: Detection confidence (0.0-1.0, see confidence guide)
        ast_node_type: Optional AST node type (e.g., "FunctionDef", "Decorator")
        dynamic_parts: List of dynamic/interpolated parts in f-strings
        framework_version: Optional framework version if detectable
    
    Returns:
        Fully formed CandidateV1 dict
    """
    c: CandidateV1 = {
        "kind": kind,
        "join": join,
        "schema_version": SCHEMA_VERSION,
    }
    
    if raw:
        c["raw"] = raw
    if meta:
        c["meta"] = dict(meta)
    if where:
        c["where"] = dict(where)
    
    # Build evidence dict
    ev: Evidence = {"confidence": float(confidence)}
    if syntax:
        ev["syntax"] = syntax
    if text:
        ev["text"] = text
    if ast_node_type:
        ev["ast_node_type"] = ast_node_type
    if dynamic_parts:
        ev["dynamic_parts"] = list(dynamic_parts)
    if framework_version:
        ev["framework_version"] = framework_version
    
    c["evidence"] = ev
    
    return c


# ============================================================================
# User-facing registry (context + what qualifies)
# ============================================================================

CANDIDATE_REGISTRY_PY_V1: Dict[CandidateKind, Dict[str, object]] = {
    "http_route": {
        "description": "Inbound HTTP route definition in Python framework",
        "confidence_typical": 1.0,
        "residents": [
            "@app.get('/path') / @router.post('/path') (FastAPI)",
            "@app.route('/path', methods=['GET']) (Flask)",
            "Blueprint.route('/path') (Flask)",
            "urlpatterns = [ path('x/', view) ] (Django)  [v1.1+]",
        ],
        "join_rule": "http:<METHOD>:/normalized/path",
        "note": "FastAPI {param}, Flask <param> styles normalized to :id for cross-language matching",
        "examples": [
            {"join": "http:GET:/api/users", "raw": "@app.get('/api/users')"},
            {"join": "http:POST:/users/:id", "raw": "@app.post('/users/{id}')"},
            {"join": "http:GET:/users/:id", "raw": "@app.route('/users/<int:user_id>')"},
        ],
    },
    "env_ref": {
        "description": "Environment variable reference",
        "confidence_typical": 1.0,
        "residents": [
            "os.getenv('API_BASE_URL')",
            "os.environ['API_BASE_URL'] / os.environ.get('API_BASE_URL')",
            "dotenv load patterns (v1.1+)",
            "pydantic Settings fields (v1.1+)",
        ],
        "join_rule": "env:<VAR_NAME>",
        "note": "v1 detects literal access; v1.1+ may add Settings model support",
        "examples": [
            {"join": "env:API_BASE_URL", "raw": "os.getenv('API_BASE_URL')"},
            {"join": "env:API_KEY", "raw": "os.environ['API_KEY']"},
        ],
    },
    "openapi_decl": {
        "description": "OpenAPI / Swagger schema exposure endpoints",
        "confidence_typical": 1.0,
        "residents": [
            "FastAPI: openapi_url='/openapi.json'",
            "Swagger UI docs endpoints (framework-specific)",
            "Serving openapi.json/swagger.json as static/route",
        ],
        "join_rule": "openapi:<type>:<id>",
        "examples": [
            {"join": "openapi:schema:/openapi.json", "raw": "FastAPI(openapi_url='/openapi.json')"},
            {"join": "openapi:docs:/docs", "raw": "FastAPI(docs_url='/docs')"},
        ],
    },
    "rpc_decl": {
        "description": "Named RPC endpoint or service method name",
        "confidence_typical": 0.9,
        "residents": [
            "gRPC service/method names",
            "jsonrpc method strings",
            "custom rpc dispatch table keys",
        ],
        "join_rule": "rpc:<qualified_name>",
        "examples": [
            {"join": "rpc:UserService.GetUser", "raw": "def GetUser(...)"},
            {"join": "rpc:jsonrpc:getUser", "raw": "@jsonrpc.method('getUser')"},
        ],
    },
    "socket_event": {
        "description": "Socket event name on server",
        "confidence_typical": 1.0,
        "residents": [
            "@socketio.on('user.created')",
            "sio.on('user.created') / sio.emit('user.created')",
        ],
        "join_rule": "socket:event:<name>",
        "note": "v1 does not include Socket.IO namespaces; may add in v1.1+",
        "examples": [
            {"join": "socket:event:user.created", "raw": "@socketio.on('user.created')"},
            {"join": "socket:event:message", "raw": "sio.emit('message', data)"},
        ],
    },
    "queue_event": {
        "description": "Queue/pubsub event topic name on server",
        "confidence_typical": 1.0,
        "residents": [
            "kafka topic strings",
            "amqp routing key strings",
            "sns topic strings",
        ],
        "join_rule": "queue:event:<name>",
        "examples": [
            {"join": "queue:event:user.created", "raw": "producer.send('user.created', ...)"},
            {"join": "queue:event:order.placed", "raw": "channel.basic_publish(routing_key='order.placed', ...)"},
        ],
    },
    "graphql_op": {
        "description": "GraphQL operation names/types (server-side schema or resolvers)",
        "confidence_typical": 1.0,
        "residents": [
            "Graphene schema/resolver names",
            "Ariadne type_defs operations",
            "Strawberry @strawberry.type query fields",
        ],
        "join_rule": "graphql:<op_type>:<name_or_hash>",
        "note": "v1 detects resolver function names; full schema parsing in v1.1+",
        "examples": [
            {"join": "graphql:query:GetUser", "raw": "def resolve_get_user(...)"},
            {"join": "graphql:mutation:CreateUser", "raw": "def resolve_create_user(...)"},
        ],
    },
}


# ============================================================================
# Optional helpers for Python-side extraction implementations
# ============================================================================

_GQL_OP_RE = re.compile(r"\b(query|mutation|subscription)\b", re.IGNORECASE)
_GQL_NAME_RE = re.compile(r"\b(query|mutation|subscription)\s+([_A-Za-z][_0-9A-Za-z]*)", re.IGNORECASE)


def graphql_op_name_or_hash(text: str) -> Dict[str, str]:
    """
    Extract operation metadata from GraphQL document.
    
    Shared helper if you extract GraphQL documents in Python (rare).
    For server-side GraphQL, you may instead emit operation names from schema/resolvers.

    Returns:
      - op_type: query|mutation|subscription (defaults to "query" if not specified)
      - name: operation name if present, empty string otherwise
      - hash: sha1 of normalized text (always available)
      - join_name_or_hash: either <name> or sha1:<8hex>

    Normalization: collapse whitespace for hashing.
    
    Note: v1 handles single operation per document. For documents with multiple
    operations, only the first is detected. This may be enhanced in v1.1+.
    """
    t = (text or "").strip()
    
    # Normalize whitespace for consistent hashing
    norm = re.sub(r"\s+", " ", t)
    h = sha1(norm.encode("utf-8")).hexdigest()

    # Detect operation type (default to "query" for shorthand syntax)
    op_type = "query"
    mtype = _GQL_OP_RE.search(norm)
    if mtype:
        op_type = mtype.group(1).lower()

    # Extract operation name if present
    name = ""
    mname = _GQL_NAME_RE.search(norm)
    if mname:
        name = mname.group(2)

    # Use name if available, otherwise use hash prefix
    join_part = name if name else f"sha1:{h[:8]}"
    
    return {
        "op_type": op_type,
        "name": name,
        "hash": h,
        "join_name_or_hash": join_part,
    }


# ============================================================================
# Framework detection helper (optional)
# ============================================================================

def detect_framework(code: str, imports: List[str]) -> str:
    """
    Best-effort framework detection from imports and syntax.
    
    Args:
        code: Source code snippet or full file
        imports: List of imported module names
    
    Returns:
        Framework identifier: "fastapi" | "flask" | "django" | "starlette" | "other"
    
    Note:
        This is a heuristic helper. For production use, consider more robust
        detection based on AST analysis of import statements and decorator usage.
    """
    # Normalize for case-insensitive matching
    imports_lower = [imp.lower() for imp in imports]
    code_lower = code.lower()
    
    # Check imports first (most reliable)
    if "fastapi" in imports_lower:
        return "fastapi"
    if "flask" in imports_lower:
        return "flask"
    if "django" in imports_lower:
        return "django"
    if "starlette" in imports_lower:
        return "starlette"
    
    # Fallback to syntax patterns (less reliable)
    if "@app.get" in code or "@app.post" in code or "fastapi" in code_lower:
        return "fastapi"
    if "@app.route" in code or "flask" in code_lower:
        return "flask"
    if "urlpatterns" in code or "django" in code_lower:
        return "django"
    
    return "other"


# ============================================================================
# Test cases for analyzer implementers
# ============================================================================

TEST_CASES = [
    # Format: (description, input_code, expected_join, expected_meta, confidence)
    
    # HTTP routes - FastAPI
    (
        "FastAPI GET route literal",
        "@app.get('/api/users')\ndef get_users(): ...",
        "http:GET:/api/users",
        {"method": "GET", "path": "/api/users", "framework": "fastapi"},
        1.0
    ),
    (
        "FastAPI POST route with path param",
        "@app.post('/api/users/{user_id}')\ndef create_user(user_id: int): ...",
        "http:POST:/api/users/:id",
        {"method": "POST", "path": "/api/users/:id", "framework": "fastapi"},
        1.0
    ),
    (
        "FastAPI route with multiple params",
        "@app.get('/posts/{post_id}/comments/{comment_id}')",
        "http:GET:/posts/:id/comments/:id",
        {"method": "GET", "path": "/posts/:id/comments/:id", "framework": "fastapi"},
        1.0
    ),
    
    # HTTP routes - Flask
    (
        "Flask route literal",
        "@app.route('/api/users', methods=['GET'])\ndef get_users(): ...",
        "http:GET:/api/users",
        {"method": "GET", "path": "/api/users", "framework": "flask"},
        1.0
    ),
    (
        "Flask route with typed param",
        "@app.route('/users/<int:user_id>')\ndef get_user(user_id): ...",
        "http:GET:/users/:id",
        {"method": "GET", "path": "/users/:id", "framework": "flask"},
        1.0
    ),
    (
        "Flask route with untyped param",
        "@app.route('/users/<user_id>')\ndef get_user(user_id): ...",
        "http:GET:/users/:id",
        {"method": "GET", "path": "/users/:id", "framework": "flask"},
        1.0
    ),
    (
        "Flask route with path param",
        "@app.route('/files/<path:filepath>')\ndef serve_file(filepath): ...",
        "http:GET:/files/:id",
        {"method": "GET", "path": "/files/:id", "framework": "flask"},
        1.0
    ),
    
    # HTTP routes - f-strings
    (
        "FastAPI route with f-string param",
        "@app.get(f'/api/users/{user_id}')\ndef get_user(): ...",
        "http:GET:/api/users/:id",
        {"method": "GET", "path": "/api/users/:id", "framework": "fastapi"},
        0.9
    ),
    
    # Environment variables
    (
        "os.getenv literal",
        "api_key = os.getenv('API_KEY')",
        "env:API_KEY",
        {"var": "API_KEY", "system": "os"},
        1.0
    ),
    (
        "os.environ dictionary access",
        "base_url = os.environ['API_BASE_URL']",
        "env:API_BASE_URL",
        {"var": "API_BASE_URL", "system": "os"},
        1.0
    ),
    (
        "os.environ.get with default",
        "secret = os.environ.get('SECRET_KEY', 'default')",
        "env:SECRET_KEY",
        {"var": "SECRET_KEY", "system": "os"},
        1.0
    ),
    
    # Socket events
    (
        "Flask-SocketIO on decorator",
        "@socketio.on('user.created')\ndef handle_user_created(data): ...",
        "socket:event:user.created",
        {"event": "user.created", "dir": "on", "system": "socketio"},
        1.0
    ),
    (
        "Socket.IO emit call",
        "socketio.emit('notification', data)",
        "socket:event:notification",
        {"event": "notification", "dir": "emit", "system": "socketio"},
        1.0
    ),
    
    # OpenAPI
    (
        "FastAPI OpenAPI URL",
        "app = FastAPI(openapi_url='/openapi.json')",
        "openapi:schema:/openapi.json",
        {"type": "schema", "id": "/openapi.json", "framework": "fastapi"},
        1.0
    ),
    (
        "FastAPI docs URL",
        "app = FastAPI(docs_url='/docs')",
        "openapi:docs:/docs",
        {"type": "docs", "id": "/docs", "framework": "fastapi"},
        1.0
    ),
    
    # RPC
    (
        "gRPC service method",
        "class UserService(user_pb2_grpc.UserServiceServicer):\n    def GetUser(self, request, context): ...",
        "rpc:UserService.GetUser",
        {"name": "UserService.GetUser", "system": "grpc"},
        0.9
    ),
    
    # Queue events
    (
        "Kafka producer send",
        "producer.send('user.created', value=data)",
        "queue:event:user.created",
        {"event": "user.created", "system": "kafka"},
        1.0
    ),
    
    # GraphQL
    (
        "GraphQL resolver function",
        "def resolve_get_user(root, info, user_id): ...",
        "graphql:query:GetUser",
        {"op_type": "query", "name": "GetUser"},
        1.0
    ),
]


# ============================================================================
# Notes for the Python analyzer implementer
# ============================================================================
#
# Emit rules (recommended v1):
#   - Only emit when you can ground it in established syntax and a stable token:
#       * literal string in decorator (confidence 1.0)
#       * f-string with clear static prefix (confidence 0.8-0.9)
#       * const-based concatenation (confidence 0.7-0.8)
#   - Skip highly dynamic cases (confidence < 0.3) to avoid candidate spam.
#   - Use the confidence guide above to assign appropriate confidence scores.
#   - Always include evidence fields for auditability.
#
# Framework detection priorities (v1):
#   - FastAPI: High priority (decorators are clean, paths are literal)
#     * @app.get('/path'), @app.post('/path'), @router.get('/path')
#     * Look for "fastapi" in imports
#   - Flask: High priority (similar to FastAPI, may have blueprint complexity)
#     * @app.route('/path', methods=['GET']), @bp.route('/path')
#     * Look for "flask" in imports
#   - Django: Medium priority (urlpatterns parsing more complex, defer to v1.1)
#     * urlpatterns = [path('x/', view), ...]
#     * Look for "django" in imports
#   - Starlette: Medium priority (similar to FastAPI but less common)
#     * Similar decorator patterns to FastAPI
#   - Tornado: Low priority (less common, complex routing)
#
# AST parsing strategy:
#   - Use ast.NodeVisitor for decorator detection
#   - Look for ast.FunctionDef with specific decorator patterns:
#       * Decorator.func.attr in ['get', 'post', 'put', 'patch', 'delete', 'route']
#       * Decorator.func.value.id in ['app', 'router', 'bp']
#   - Extract string arguments from decorator calls:
#       * First positional arg is usually the path
#       * Keyword arg 'path' may override
#       * Keyword arg 'methods' specifies HTTP methods (Flask)
#   - Handle both Call nodes and Name nodes (for decorator references)
#
# Path parameter normalization:
#   - FastAPI: {user_id} -> :id (always)
#   - Flask: <int:user_id> -> :id (always)
#   - Flask: <user_id> -> :id (always)
#   - Flask: <path:filepath> -> :id (always)
#   - Django: <int:pk> -> :id (v1.1+)
#
# Example AST pattern detection:
#
#   Pattern 1: Simple decorator with string literal
#     @app.get('/users')
#     AST: FunctionDef(
#            decorator_list=[Call(func=Attribute(value=Name('app'), attr='get'),
#                                args=[Constant('/users')])])
#
#   Pattern 2: Decorator with keyword arguments
#     @app.route('/users', methods=['GET'])
#     AST: Call with keyword 'methods'
#
#   Pattern 3: Decorator with f-string
#     @app.get(f'/api/{version}/users')
#     AST: JoinedStr with values containing variables
#
#   Pattern 4: Blueprint registration (Flask)
#     bp = Blueprint('api', __name__, url_prefix='/api')
#     @bp.route('/users')
#     Need to track blueprint prefix for full path
#
# Environment variable detection:
#   - os.getenv(literal_string) -> confidence 1.0
#     AST: Call(func=Attribute(value=Name('os'), attr='getenv'),
#              args=[Constant('VAR_NAME')])
#   - os.environ[literal_string] -> confidence 1.0
#     AST: Subscript(value=Attribute(value=Name('os'), attr='environ'),
#                   slice=Constant('VAR_NAME'))
#   - os.environ.get(literal_string) -> confidence 1.0
#     AST: Call(func=Attribute(...), args=[Constant('VAR_NAME')])
#   - os.getenv(variable) -> SKIP (too dynamic)
#   - Settings.FIELD access -> v1.1+ (requires pydantic inspection)
#
# Socket event detection:
#   - @socketio.on('event_name') -> confidence 1.0
#     AST: Decorator with Call to socketio.on with string arg
#   - socketio.emit('event_name', data) -> confidence 1.0
#     AST: Call to socketio.emit with string first arg
#
# Error handling:
#   - Catch SyntaxError during AST parsing, log and skip file
#   - Handle missing imports gracefully (file may not import os/fastapi)
#   - Skip decorator arguments that aren't literals or simple f-strings
#   - Don't emit for test files (check file path contains 'test' or confidence penalty)
#   - Handle incomplete AST gracefully (partial parses in interactive environments)
#
# Performance considerations:
#   - Parse AST once per file, collect all candidates in single pass
#   - Cache framework detection per-file (check imports once)
#   - Avoid redundant norm_path calls (memoize if processing many routes)
#   - Use ast.walk() for simple extraction, NodeVisitor for complex cases
#   - Consider parallel processing for large codebases
#
# Matching (Level 2) workflow:
#   1. Collect all Python candidates with join keys
#   2. Collect all TS/JS candidates with join keys
#   3. Intersect join keys: http:GET:/api/users matches across languages
#   4. Emit synthetic edges only for join-key matches
#   5. Include bidirectional evidence for audit trail:
#      - Python side: @app.get('/api/users') at file.py:42
#      - TS side: fetch('/api/users') at file.ts:15
#      - Edge confidence: min(py_confidence, ts_confidence)
#
# Flask Blueprint considerations (v1.1+):
#   - Track Blueprint instantiation: Blueprint('api', url_prefix='/api')
#   - Track Blueprint registration: app.register_blueprint(bp, url_prefix='/v1')
#   - Combine prefixes: blueprint_prefix + registration_prefix + route_path
#   - Example: bp('/users') with url_prefix='/api' and register('/v1')
#     -> final path: /v1/api/users
#
# Django urlpatterns considerations (v1.1+):
#   - Parse urlpatterns list: [path('users/', view), path('users/<int:pk>/', view)]
#   - Handle include(): path('api/', include('app.urls')) requires recursive resolution
#   - Handle re_path(): re_path(r'^users/(?P<pk>\d+)/$', view) requires regex analysis
#   - Consider app mounting: ROOT_URLCONF affects final paths
#
# Pydantic Settings considerations (v1.1+):
#   - Detect Settings classes: class Settings(BaseSettings)
#   - Extract field names: api_key: str reads from API_KEY env var
#   - Handle Field aliases: Field(env='CUSTOM_NAME')
#   - Consider .env file loading (requires file system access)
#
# ============================================================================