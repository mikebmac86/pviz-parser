"""
TS/JS Crosstalk Candidates v1
============================

This module is BOTH:
  (A) user-facing context (category descriptions + syntax "residents")
  (B) program-facing contract (strict schema + join-key formats + normalization)

Goal (Level 1):
  - TS/JS analyzer emits *annotations* (facts), not cross-language edges.
  - Each candidate includes a single canonical JOIN KEY ("join") that Level 2 can match
    against Python-side detections (routes/env/rpc/etc.).

Storage convention (recommended):
  nodes[<node_id>].facts["crosstalk_candidates_ts_v1"] = List[CandidateV1]

Non-goals (v1):
  - No speculative cross-language edges
  - No dynamic evaluation of non-literal values
  - No heavy parsing logic here (this file defines contract + helpers)

Known Limitations (v1):
  - Single GraphQL operation per document
  - No template literal interpolation analysis (confidence scoring guides dynamic cases)
  - Conservative hex ID detection (may miss some patterns, prefer precision over recall)
  - No Socket.IO namespace support
  - No env var destructuring detection
  - Path traversal patterns not explicitly blocked (analyzer should sanitize)
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
    "http_call",
    "route_ref",
    "openapi_ref",
    "rpc_call",
    "env_ref",
    "graphql_op",
    "socket_event",
    "queue_event",
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
      syntax: The syntactic construct detected (e.g., "fetch", "axios.get", "process.env")
      text: Short source code snippet (<200 chars recommended)
      confidence: Float 0.0-1.0 indicating detection certainty (see confidence guide below)
      ast_node_type: Optional AST node type (e.g., "CallExpression", "MemberExpression")
      dynamic_parts: List of dynamic/interpolated segments in template literals
    """
    syntax: str
    text: str
    confidence: float
    ast_node_type: str
    dynamic_parts: List[str]


class CandidateV1(TypedDict, total=False):
    """
    Minimal machine-joinable candidate record.

    Required for matching:
      - kind
      - join

    Recommended:
      - raw
      - meta (method/path/var/event/etc)
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
# Canonical JOIN formats (single source of truth for Level-2 matching)
# ---------------------------------------------------------------------------
#
# http_call:
#   join = "http:<METHOD>:/normalized/path"
#   meta: {"method": "...", "path": "...", "host": "...?"}
#   example: "http:GET:/api/users"
#
# route_ref:
#   join = "route:/normalized/path"
#   meta: {"path": "..."}
#   example: "route:/users"
#
# env_ref:
#   join = "env:<VAR_NAME>"
#   meta: {"var": "...", "system": "node|vite|deno|bun|other"}
#   example: "env:API_BASE_URL"
#
# rpc_call:
#   join = "rpc:<qualified_name>"
#   meta: {"name": "...", "system": "trpc|grpc|custom|other"}
#   example: "rpc:trpc:user.getById"
#
# openapi_ref:
#   join = "openapi:<type>:<id>"
#   meta: {"type": "schema|client|component", "id": "..."}
#   examples:
#     - "openapi:schema:/openapi.json" (schema document)
#     - "openapi:client:UsersApi" (generated client class)
#     - "openapi:component:#/components/schemas/User" (schema component reference)
#
# socket_event:
#   join = "socket:event:<name>"
#   meta: {"event": "...", "dir": "emit|on"}
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
# Confidence Scoring Guide
# ============================================================================
#
# Use these guidelines when setting evidence["confidence"]:
#
# 1.0 - Literal string, no interpolation:
#       fetch('/api/users')
#       process.env.API_KEY
#       socket.emit('user.created')
#
# 0.9 - Template literal with known static prefix/suffix + param-like dynamic segment:
#       fetch(`/api/users/${id}`)  # dynamic segment looks like a URL parameter
#       axios.get(`/posts/${postId}/comments`)
#
# 0.8 - Template literal with const/enum interpolation:
#       fetch(`${API_BASE}api/users`)  # if API_BASE is a const/imported constant
#       process.env[`${STAGE}_API_KEY`]  # if STAGE is const/enum
#
# 0.7 - Template with multiple static segments, some dynamic parts:
#       fetch(`${baseUrl}/api/users/${id}`)  # multiple interpolations
#
# 0.6 - Property access on known object with limited possibilities:
#       ROUTES[routeKey]  # where ROUTES is a known const object
#
# 0.5 - Computed property with small known set:
#       process.env[envVarName]  # where envVarName ∈ {small known set}
#
# 0.4 - Call to known URL/path builder with literal args:
#       buildApiUrl('users', id)  # known function, first arg is literal
#
# 0.3 - Highly dynamic but pattern-detectable:
#       fetch(buildUrl('users', params))  # call chain, some static hints
#
# <0.3 - Too dynamic to emit reliably (SKIP in v1 for quality):
#        fetch(url)  # unknown variable with no static analysis path
#        process.env[dynamicKey]  # runtime-only determinable
#        axios(config)  # fully dynamic config object
#
# ============================================================================


# ============================================================================
# User-facing registry (context + what qualifies)
# ============================================================================

CANDIDATE_REGISTRY_TS_V1: Dict[CandidateKind, Dict[str, object]] = {
    "http_call": {
        "description": "Outbound HTTP request from TS/JS code",
        "confidence_typical": 1.0,
        "residents": [
            "fetch(url)",
            "axios.get/post/put/patch/delete(url)",
            "axios({ url, method, ... })",
            "ky(url) / ky.get(url) ...",
            "new Request(url)",
        ],
        "join_rule": "http:<METHOD>:/normalized/path",
        "examples": [
            {"join": "http:GET:/api/users", "raw": "fetch('/api/users')"},
            {"join": "http:POST:/users/:id", "raw": "axios.post(`/users/${id}`)"},
        ],
    },
    "route_ref": {
        "description": "String literal that references an application route (not necessarily an HTTP call)",
        "confidence_typical": 1.0,
        "residents": [
            'const USERS = "/users"',
            "router.push('/users')",
            "navigate('/login')",
        ],
        "join_rule": "route:/normalized/path",
        "note": "canon_params defaults to False for routes (literal matching preferred)",
        "examples": [
            {"join": "route:/users", "raw": 'router.push("/users")'},
        ],
    },
    "openapi_ref": {
        "description": "Explicit OpenAPI / Swagger linkage in TS/JS (generated clients, schema refs, or component references)",
        "confidence_typical": 1.0,
        "residents": [
            "Generated OpenAPI client imports (DefaultApi, UsersApi, Configuration, ...)",
            "openapi.json / swagger.json references",
            "new Configuration({ basePath })",
            "Schema component $ref (if detectable in code)",
        ],
        "join_rule": "openapi:<type>:<id>",
        "examples": [
            {"join": "openapi:schema:/openapi.json", "raw": 'fetch("/openapi.json")'},
            {"join": "openapi:client:UsersApi", "raw": "new UsersApi(config)"},
            {"join": "openapi:component:#/components/schemas/User", "raw": "import type { User } from './api'"},
        ],
    },
    "rpc_call": {
        "description": "Named RPC-style invocation (tRPC/custom RPC/grpc client wrappers)",
        "confidence_typical": 0.9,
        "residents": [
            "trpc.user.getById.useQuery()",
            "trpc.post.create.useMutation()",
            "rpc.call('UserService.GetUser')",
        ],
        "join_rule": "rpc:<qualified_name>",
        "examples": [
            {"join": "rpc:trpc:user.getById", "raw": "trpc.user.getById.useQuery(...)"},
            {"join": "rpc:UserService.GetUser", "raw": "rpc.call('UserService.GetUser', ...)"},
        ],
    },
    "env_ref": {
        "description": "Environment variable reference (often cross-language configuration join)",
        "confidence_typical": 1.0,
        "residents": [
            "process.env.API_BASE_URL",
            "import.meta.env.VITE_API_URL",
            "Deno.env.get('API_URL')",
            "Bun.env.API_URL",
        ],
        "join_rule": "env:<VAR_NAME>",
        "note": "v1 detects property access; v1.1+ may add destructuring support",
        "examples": [
            {"join": "env:API_BASE_URL", "raw": "process.env.API_BASE_URL"},
        ],
    },
    "graphql_op": {
        "description": "GraphQL query/mutation/subscription operation",
        "confidence_typical": 1.0,
        "residents": [
            "gql`query GetUser { ... }`",
            "useQuery(gql`...`)",
            "graphql-request(query, variables)",
        ],
        "join_rule": "graphql:<op_type>:<name_or_hash>",
        "note": "v1 parses single operation per document; uses hash fallback for anonymous ops",
        "examples": [
            {"join": "graphql:query:GetUser", "raw": "gql`query GetUser { ... }`"},
            {"join": "graphql:query:sha1:abcd1234", "raw": "gql`query { ... }`"},
        ],
    },
    "socket_event": {
        "description": "Realtime event usage (WebSocket/Socket.IO)",
        "confidence_typical": 1.0,
        "residents": [
            "socket.emit('user.created')",
            "socket.on('user.created')",
        ],
        "join_rule": "socket:event:<name>",
        "note": "v1 does not include Socket.IO namespaces; may add in v1.1+",
        "examples": [
            {"join": "socket:event:user.created", "raw": "socket.emit('user.created', ...)"},
        ],
    },
    "queue_event": {
        "description": "Message queue / pubsub event usage (more common in Node services than browser)",
        "confidence_typical": 1.0,
        "residents": [
            "publish('user.created')",
            "kafka.producer().send({ topic: 'user.created', ... })",
            "amqpChannel.publish(..., 'user.created')",
        ],
        "join_rule": "queue:event:<name>",
        "examples": [
            {"join": "queue:event:user.created", "raw": "publish('user.created', payload)"},
        ],
    },
}


# ============================================================================
# Path normalization helpers (usable by the parser)
# ============================================================================

# --- Common path cleanup ---
_MULTI_SLASH = re.compile(r"/{2,}")
_TRAILING_SLASH = re.compile(r"(.+)/+$")
_QUERY_HASH = re.compile(r"[?#].*$")


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

    # Extract path from full URL (handles http(s)://host:port/path)
    # Improved regex to handle more URL formats
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+(/.*)$", p)
    if m:
        p = m.group(1)
    else:
        # Check for URL without path (e.g., "https://api.example.com")
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


# --- Parameter canonicalization heuristics ---
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
_INT_RE = re.compile(r"^\d+$")
_HEX_RE = re.compile(r"^(0x)?[0-9a-fA-F]+$")


def canon_path_params(path: str, *, hex_threshold: int = 16) -> str:
    """
    Normalize dynamic path segments into a join-friendly canonical form.

    - Express-style params (:userId) -> :id
    - FastAPI style {id} -> :id
    - Flask style <int:id> / <id> -> :id
    - ints, UUIDs, long hex -> :id

    This is intentionally aggressive for HTTP joins to maximize cross-language matches.
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

        s = seg.strip()

        # --- Named param syntaxes -> :id ---
        # Express: :userId
        if s.startswith(":"):
            out.append(":id")
            continue

        # FastAPI: {user_id}
        if s.startswith("{") and s.endswith("}") and len(s) >= 3:
            out.append(":id")
            continue

        # Flask: <int:user_id> or <user_id>
        if s.startswith("<") and s.endswith(">") and len(s) >= 3:
            out.append(":id")
            continue

        # --- Literal-ish IDs -> :id ---
        if _INT_RE.match(s):
            out.append(":id")
            continue
        if _UUID_RE.match(s):
            out.append(":id")
            continue
        if len(s) >= hex_threshold and _HEX_RE.match(s):
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
#   - http_call: defaults to True (HTTP paths often have IDs: /users/123 -> /users/:id)
#   - route_ref: defaults to False (route literals are typically exact: "/users" should match "/users")
#
# Rationale: HTTP calls frequently contain dynamic IDs in URLs that should be normalized
# for matching, while route references are usually string constants meant to match exactly.
# You can override these defaults by explicitly passing canon_params in your analyzer.
# ============================================================================

def join_http(method: Optional[str], path: str, *, canon_params: bool = True) -> str:
    """
    Build join key for HTTP calls.
    
    Args:
        method: HTTP method (GET, POST, etc.)
        path: URL path
        canon_params: If True, normalize ID-like segments to :id (default: True for HTTP)
    
    Returns:
        Join key in format "http:<METHOD>:/normalized/path"
    """
    m = norm_http_method(method)
    p = canon_path_params(path) if canon_params else norm_path(path)
    return f"http:{m}:{p}"


def join_route(path: str, *, canon_params: bool = False) -> str:
    """
    Build join key for route references.
    
    Args:
        path: Route path
        canon_params: If True, normalize ID-like segments (default: False for routes)
    
    Returns:
        Join key in format "route:/normalized/path"
    """
    p = canon_path_params(path) if canon_params else norm_path(path)
    return f"route:{p}"


def join_env(var: str) -> str:
    """Build join key for environment variable."""
    v = (var or "").strip()
    return f"env:{v}"


def join_rpc(name: str) -> str:
    """Build join key for RPC call."""
    n = (name or "").strip()
    return f"rpc:{n}"


def join_openapi(type_: str, id_: str) -> str:
    """
    Build join key for OpenAPI reference.
    
    Args:
        type_: Reference type (schema|client|component)
        id_: Identifier (path to schema, client class name, component ref)
    
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
# GraphQL helpers
# ============================================================================

_GQL_OP_RE = re.compile(r"\b(query|mutation|subscription)\b", re.IGNORECASE)
_GQL_NAME_RE = re.compile(r"\b(query|mutation|subscription)\s+([_A-Za-z][_0-9A-Za-z]*)", re.IGNORECASE)


def graphql_op_name_or_hash(gql_text: str) -> Dict[str, str]:
    """
    Extract operation metadata from GraphQL document.

    Returns:
      - op_type: query|mutation|subscription (defaults to "query" if not specified)
      - name: operation name if present, empty string otherwise
      - hash: sha1 of normalized text (always available)
      - join_name_or_hash: either <Name> or sha1:<8hex>

    Normalization: collapse whitespace for hashing.
    
    Note: v1 handles single operation per document. For documents with multiple
    operations, only the first is detected. This may be enhanced in v1.1+.
    """
    t = (gql_text or "").strip()
    
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
# Convenience constructors
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
) -> CandidateV1:
    """
    Build a CandidateV1 in a consistent shape.
    
    Args:
        kind: Candidate type
        join: Canonical join key
        raw: Original source expression
        meta: Additional metadata (method, path, etc.)
        where: Source location
        syntax: Detected syntax pattern (e.g., "fetch", "axios.get")
        text: Short code snippet
        confidence: Detection confidence (0.0-1.0, see confidence guide)
        ast_node_type: Optional AST node type
        dynamic_parts: List of dynamic/interpolated parts in templates
    
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
    
    c["evidence"] = ev
    
    return c


# ============================================================================
# Category "resident" lists as machine-readable match targets (optional)
# ============================================================================
# These are NOT strict parsers; they are "labels" you can use for reporting,
# configuration, or lightweight heuristics.
# ============================================================================

HTTP_CALL_SYNTAX = ("fetch", "axios", "ky", "Request")
ENV_SYNTAX = ("process.env", "import.meta.env", "Deno.env", "Bun.env")
SOCKET_SYNTAX = ("socket.emit", "socket.on")
OPENAPI_HINTS = ("openapi.json", "swagger.json", "DefaultApi", "Configuration")
TRPC_HINT = "trpc."


# ============================================================================
# Test cases for analyzer implementers
# ============================================================================
#
# These serve as both documentation and validation targets. Analyzer
# implementations should produce candidates matching these expectations.
#
# Format: (description, input_code, expected_join, expected_meta, expected_confidence)
# ============================================================================

TEST_CASES = [
    # HTTP calls - literal
    (
        "fetch with literal path",
        "fetch('/api/users')",
        "http:GET:/api/users",
        {"method": "GET", "path": "/api/users"},
        1.0
    ),
    (
        "axios.post with literal",
        "axios.post('/api/users', data)",
        "http:POST:/api/users",
        {"method": "POST", "path": "/api/users"},
        1.0
    ),
    
    # HTTP calls - template with ID param
    (
        "fetch with template literal ID",
        "fetch(`/api/users/${userId}`)",
        "http:GET:/api/users/:id",
        {"method": "GET", "path": "/api/users/:id"},
        0.9
    ),
    
    # Environment variables
    (
        "process.env access",
        "process.env.API_BASE_URL",
        "env:API_BASE_URL",
        {"var": "API_BASE_URL", "system": "node"},
        1.0
    ),
    (
        "Vite env access",
        "import.meta.env.VITE_API_URL",
        "env:VITE_API_URL",
        {"var": "VITE_API_URL", "system": "vite"},
        1.0
    ),
    
    # RPC calls
    (
        "tRPC query",
        "trpc.user.getById.useQuery(id)",
        "rpc:trpc:user.getById",
        {"name": "user.getById", "system": "trpc"},
        0.9
    ),
    
    # GraphQL
    (
        "Named GraphQL query",
        "gql`query GetUser { user { id } }`",
        "graphql:query:GetUser",
        {"op_type": "query", "name": "GetUser"},
        1.0
    ),
    (
        "Anonymous GraphQL query",
        "gql`query { user { id } }`",
        "graphql:query:sha1:abcd1234",  # actual hash will vary
        {"op_type": "query", "name": ""},
        1.0
    ),
    
    # Socket events
    (
        "Socket.IO emit",
        "socket.emit('user.created', data)",
        "socket:event:user.created",
        {"event": "user.created", "dir": "emit"},
        1.0
    ),
    
    # Route refs
    (
        "Router navigation",
        "router.push('/users')",
        "route:/users",
        {"path": "/users"},
        1.0
    ),
]


# ============================================================================
# Notes for the TS/JS analyzer implementer
# ============================================================================
#
# Emit rules (recommended v1):
#   - Only emit when you can ground it in established syntax and a stable token:
#       * string literal (confidence 1.0)
#       * template literal with clear static prefix (confidence 0.8-0.9)
#       * const-based interpolation (confidence 0.8)
#   - Skip highly dynamic cases (confidence < 0.3) to avoid candidate spam.
#   - Use the confidence guide above to assign appropriate confidence scores.
#   - Always include evidence.syntax and evidence.text for auditability.
#
# Practical v1 priorities:
#   - http_call (highest value for cross-language matching)
#   - env_ref (critical for config matching)
#   - rpc_call (especially tRPC in fullstack TS apps)
#   - openapi_ref (if OpenAPI clients are in use)
#   - graphql_op (if GraphQL is in use)
#   - socket_event, queue_event (if realtime/messaging is in use)
#
# Matching (Level 2) workflow:
#   1. Collect all TS/JS candidates with join keys
#   2. Collect all Python detections with join keys
#   3. Intersect join keys between the two sets
#   4. Emit synthetic edges only for join-key matches
#   5. Preserve evidence and confidence for audit trail
#
# Error handling:
#   - Sanitize inputs (avoid path traversal, injection)
#   - Gracefully skip malformed code (AST parse errors)
#   - Log skipped candidates with reasons for debugging
#   - Don't emit candidates for commented-out code
#
# Performance considerations:
#   - Parse AST once, collect all candidates in single pass
#   - Avoid redundant normalization (cache norm_path results)
#   - Batch candidates by file for efficient storage
#
# ============================================================================