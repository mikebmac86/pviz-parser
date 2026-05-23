from __future__ import annotations

"""
Rust parsed file models.

These dataclasses represent the output of rustparser_cli (or any Rust parser backend).

Design goals:
- Backward compatible with previous minimal schema (keeps old fields like is_pub, fields: List[str])
- Additive richness without requiring a compiler (syn-based parsing, token strings, path heuristics)
- Structured visibility, attributes, docs, cfg-gating, and richer symbol shapes
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Shared helpers / types
# ---------------------------------------------------------------------------

VisibilityStr = str
"""
VisibilityStr examples:
- "private"
- "pub"
- "pub(crate)"
- "pub(super)"
- "pub(in path::to::mod)"
"""


@dataclass(frozen=True)
class RustSpan:
    """
    Optional source location info (best-effort).

    Keep this flexible: some parsers provide line/col; others provide byte offsets;
    some provide nothing. Leave None when unknown.
    """
    file: Optional[str] = None  # repo-relative or posix file id
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    col_start: Optional[int] = None
    col_end: Optional[int] = None


@dataclass
class RustAttribute:
    """
    A normalized representation of an attribute.

    name:
      - "derive"
      - "cfg"
      - "cfg_attr"
      - "tokio::main"
      - "serde::Serialize" (if you choose to store paths)
    tokens:
      - raw-ish token string contents, e.g. 'test', 'feature = "foo"', 'Serialize, Deserialize'
    """
    name: str
    tokens: Optional[str] = None


def vis_to_is_pub(vis: Optional[str]) -> bool:
    """Compatibility helper: treat any non-private visibility as "pub" if needed."""
    if not vis:
        return False
    v = vis.strip()
    return v != "private"


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

@dataclass
class RustUseStatement:
    """Single use statement."""
    path: str  # e.g., "std::collections::HashMap"
    alias: Optional[str] = None  # e.g., "use foo as bar"
    is_glob: bool = False  # e.g., "use foo::*"

    # Compatibility + richer visibility
    is_pub: bool = False  # legacy (pub use)
    visibility: VisibilityStr = "private"  # preferred

    attrs: List[RustAttribute] = field(default_factory=list)
    span: Optional[RustSpan] = None


@dataclass
class RustModDeclaration:
    """Module declaration (mod foo;)."""
    name: str

    # Compatibility + richer visibility
    is_pub: bool = False
    visibility: VisibilityStr = "private"

    is_inline: bool = False  # mod foo { } vs mod foo;

    attrs: List[RustAttribute] = field(default_factory=list)
    cfg: List[str] = field(default_factory=list)  # extracted from #[cfg(...)]
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

@dataclass
class RustParam:
    """
    A function parameter. Without a compiler, we store best-effort strings.
    Examples:
      name="path", ty="&Path", raw="path: &Path"
      name=None, ty=None, raw="self"
    """
    raw: str
    name: Optional[str] = None
    ty: Optional[str] = None


@dataclass
class RustField:
    """Struct/tuple fields (best-effort)."""
    # name is None for tuple fields (unnamed)
    name: Optional[str] = None
    ty: Optional[str] = None

    # Compatibility + richer visibility
    is_pub: bool = False
    visibility: VisibilityStr = "private"

    attrs: List[RustAttribute] = field(default_factory=list)
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


@dataclass
class RustVariant:
    """Enum variant (best-effort)."""
    name: str
    fields: List[RustField] = field(default_factory=list)
    attrs: List[RustAttribute] = field(default_factory=list)
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


@dataclass
class RustFunction:
    """Function definition."""
    name: str

    # Compatibility + richer visibility
    is_pub: bool = False
    visibility: VisibilityStr = "private"

    is_async: bool = False
    is_unsafe: bool = False
    is_const: bool = False
    is_extern: bool = False  # extern "C" fn ...
    abi: Optional[str] = None  # "C", "system", etc.

    # Backward compatible fields
    params: List[str] = field(default_factory=list)  # legacy: raw token strings
    return_type: Optional[str] = None

    # Preferred rich params (optional)
    params_rich: List[RustParam] = field(default_factory=list)

    generics: Optional[str] = None
    where_clause: Optional[str] = None

    attrs: List[RustAttribute] = field(default_factory=list)
    cfg: List[str] = field(default_factory=list)
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


@dataclass
class RustStruct:
    """Struct definition."""
    name: str

    # Compatibility + richer visibility
    is_pub: bool = False
    visibility: VisibilityStr = "private"

    # Backward compatible field list (names only)
    fields: List[str] = field(default_factory=list)

    # Preferred rich field list
    fields_rich: List[RustField] = field(default_factory=list)

    derives: List[str] = field(default_factory=list)
    generics: Optional[str] = None
    where_clause: Optional[str] = None

    attrs: List[RustAttribute] = field(default_factory=list)
    cfg: List[str] = field(default_factory=list)
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


@dataclass
class RustEnum:
    """Enum definition."""
    name: str

    # Compatibility + richer visibility
    is_pub: bool = False
    visibility: VisibilityStr = "private"

    # Backward compatible variants (names only)
    variants: List[str] = field(default_factory=list)

    # Preferred rich variants
    variants_rich: List[RustVariant] = field(default_factory=list)

    derives: List[str] = field(default_factory=list)
    generics: Optional[str] = None
    where_clause: Optional[str] = None

    attrs: List[RustAttribute] = field(default_factory=list)
    cfg: List[str] = field(default_factory=list)
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


@dataclass
class RustTrait:
    """Trait definition."""
    name: str

    # Compatibility + richer visibility
    is_pub: bool = False
    visibility: VisibilityStr = "private"

    is_unsafe: bool = False

    # Backward compatible methods (names only)
    methods: List[str] = field(default_factory=list)

    # Preferred rich methods
    methods_rich: List[RustFunction] = field(default_factory=list)

    supertraits: List[str] = field(default_factory=list)
    generics: Optional[str] = None
    where_clause: Optional[str] = None

    attrs: List[RustAttribute] = field(default_factory=list)
    cfg: List[str] = field(default_factory=list)
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


@dataclass
class RustImpl:
    """Impl block."""
    target: str  # struct/enum name or type token string
    trait_name: Optional[str] = None  # None for inherent impl
    is_unsafe: bool = False

    # Backward compatible methods (names only)
    methods: List[str] = field(default_factory=list)

    # Preferred rich methods
    methods_rich: List[RustFunction] = field(default_factory=list)

    generics: Optional[str] = None
    where_clause: Optional[str] = None

    attrs: List[RustAttribute] = field(default_factory=list)
    cfg: List[str] = field(default_factory=list)
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


@dataclass
class RustTypeAlias:
    """Type alias definition (type Foo = Bar;)."""
    name: str

    # Compatibility + richer visibility
    is_pub: bool = False
    visibility: VisibilityStr = "private"

    target: str = ""  # token string for RHS
    generics: Optional[str] = None
    where_clause: Optional[str] = None

    attrs: List[RustAttribute] = field(default_factory=list)
    cfg: List[str] = field(default_factory=list)
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


@dataclass
class RustConst:
    """Const definition (const NAME: Ty = ...)."""
    name: str

    # Compatibility + richer visibility
    is_pub: bool = False
    visibility: VisibilityStr = "private"

    ty: Optional[str] = None
    value: Optional[str] = None  # token string (optional)

    attrs: List[RustAttribute] = field(default_factory=list)
    cfg: List[str] = field(default_factory=list)
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


@dataclass
class RustStatic:
    """Static definition (static NAME: Ty = ...)."""
    name: str

    # Compatibility + richer visibility
    is_pub: bool = False
    visibility: VisibilityStr = "private"

    is_mut: bool = False
    ty: Optional[str] = None
    value: Optional[str] = None  # token string (optional)

    attrs: List[RustAttribute] = field(default_factory=list)
    cfg: List[str] = field(default_factory=list)
    doc: Optional[str] = None
    span: Optional[RustSpan] = None


# ---------------------------------------------------------------------------
# Parsed file
# ---------------------------------------------------------------------------

@dataclass
class RustParsedFile:
    """
    Parsed Rust file result.

    Notes:
    - `module_path` is intended to be set by either:
        (a) the parser backend (ideal), or
        (b) path-based inference in Python (good fallback)
    - `attributes_raw` is optional: store raw attribute tokens if you want
      LLM-friendly "what knobs exist" context without a compiler.
    """
    file_path: Path
    ok: bool
    parse_status: str  # "ok", "error", "no_cli", etc.

    # Module identity
    module_path: Optional[str] = None  # e.g., "crate::foo::bar"
    crate_name: Optional[str] = None   # optional (if you infer from Cargo layout)
    edition: Optional[str] = None      # optional (if you parse Cargo.toml; not required)

    # Dependencies
    use_statements: List[RustUseStatement] = field(default_factory=list)
    mod_declarations: List[RustModDeclaration] = field(default_factory=list)

    # Symbols
    functions: List[RustFunction] = field(default_factory=list)
    structs: List[RustStruct] = field(default_factory=list)
    enums: List[RustEnum] = field(default_factory=list)
    traits: List[RustTrait] = field(default_factory=list)
    impls: List[RustImpl] = field(default_factory=list)
    type_aliases: List[RustTypeAlias] = field(default_factory=list)
    consts: List[RustConst] = field(default_factory=list)
    statics: List[RustStatic] = field(default_factory=list)

    # File-level metadata & knobs
    has_macro_use: bool = False  # #[macro_use]
    is_lib: bool = False  # lib.rs
    is_main: bool = False  # main.rs
    is_mod: bool = False  # mod.rs

    # Attributes (file-level)
    attrs: List[RustAttribute] = field(default_factory=list)
    cfg: List[str] = field(default_factory=list)
    doc: Optional[str] = None
    attributes_raw: List[str] = field(default_factory=list)  # optional raw strings

    # Error info
    error: Optional[str] = None

    # Optional “quality” / summary hints (no compiler required)
    problems: List[str] = field(default_factory=list)

    loc_code: Optional[int] = None
    sloc_code: Optional[int] = None
    comment_lines: Optional[int] = None
    blank_lines: Optional[int] = None
    comment_pct: Optional[float] = None

    hash: Optional[int] = None
    span: Optional[RustSpan] = None


# ---------------------------------------------------------------------------
# Normalization utilities (optional, safe to use from Python)
# ---------------------------------------------------------------------------

def normalize_visibility(vis: Optional[str]) -> VisibilityStr:
    """
    Normalize visibility strings into a small stable set.
    Keep it permissive — don’t throw.
    """
    if not vis:
        return "private"
    v = vis.strip()
    if not v:
        return "private"
    if v == "pub":
        return "pub"
    if v.startswith("pub(") and v.endswith(")"):
        return v  # keep exact
    if v.startswith("pub "):  # weird formatting
        return "pub"
    if v == "private":
        return "private"
    # fallback: store as-is (still better than losing it)
    return v


def backfill_is_pub_from_visibility() -> None:
    """
    Placeholder for any future migration scripts.
    Keep as no-op for now.
    """
    return
