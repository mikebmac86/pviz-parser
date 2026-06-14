from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _dict(v: Any) -> Dict[str, Any]:
    return dict(v) if isinstance(v, Mapping) else {}


def _list(v: Any) -> List[Any]:
    return list(v) if isinstance(v, list) else []


def _list_of_dicts(v: Any) -> List[Dict[str, Any]]:
    return [dict(x) for x in _list(v) if isinstance(x, Mapping)]


def _str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return str(v)


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return bool(v)


def _int_or_none(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def _float_or_none(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _str_list(v: Any) -> List[str]:
    return [str(x).strip() for x in _list(v) if str(x).strip()]


# ---------------------------------------------------------------------------
# Parser request
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RubyAnalysisRequest:
    repo_root: Path
    files: List[str]
    rails_mode: str = "auto"
    include_constant_refs: bool = True
    include_method_calls: bool = True
    include_rails_dsl: bool = True
    include_bundler_index: bool = True
    include_dynamic_require_facts: bool = True
    max_bytes_per_file: int = 200_000_000

    def to_json_obj(self) -> Dict[str, Any]:
        return {
            "schema_version": "ruby_analysis_request@v1",
            "repo_root": str(self.repo_root),
            "files": list(self.files),
            "options": {
                "rails_mode": self.rails_mode,
                "include_constant_refs": bool(self.include_constant_refs),
                "include_method_calls": bool(self.include_method_calls),
                "include_rails_dsl": bool(self.include_rails_dsl),
                "include_bundler_index": bool(self.include_bundler_index),
                "include_dynamic_require_facts": bool(self.include_dynamic_require_facts),
                "max_bytes_per_file": int(self.max_bytes_per_file),
            },
        }


# ---------------------------------------------------------------------------
# Parser result models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RubyLoc:
    line: Optional[int] = None
    col: Optional[int] = None
    end_line: Optional[int] = None
    end_col: Optional[int] = None

    @staticmethod
    def from_json(data: Any) -> "RubyLoc":
        d = _dict(data)
        return RubyLoc(
            line=_int_or_none(d.get("line")),
            col=_int_or_none(d.get("col")),
            end_line=_int_or_none(d.get("end_line")),
            end_col=_int_or_none(d.get("end_col")),
        )


@dataclass(frozen=True)
class RubyRequire:
    kind: str
    spec: Optional[str] = None
    symbol: Optional[str] = None
    raw: Optional[str] = None
    dynamic: bool = False
    loc: RubyLoc = field(default_factory=RubyLoc)

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> "RubyRequire":
        return RubyRequire(
            kind=_str(data.get("kind"), "unknown").strip() or "unknown",
            spec=_str_or_none(data.get("spec")),
            symbol=_str_or_none(data.get("symbol")),
            raw=_str_or_none(data.get("raw")),
            dynamic=_bool(data.get("dynamic"), False),
            loc=RubyLoc.from_json(data.get("loc")),
        )


@dataclass(frozen=True)
class RubyDeclaration:
    kind: str
    name: str
    fq_name: Optional[str] = None
    owner: Optional[str] = None
    superclass: Optional[str] = None
    visibility: Optional[str] = None
    includes: List[str] = field(default_factory=list)
    extends: List[str] = field(default_factory=list)
    prepends: List[str] = field(default_factory=list)
    loc: RubyLoc = field(default_factory=RubyLoc)

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> "RubyDeclaration":
        return RubyDeclaration(
            kind=_str(data.get("kind"), "unknown").strip() or "unknown",
            name=_str(data.get("name")).strip(),
            fq_name=_str_or_none(data.get("fq_name")),
            owner=_str_or_none(data.get("owner")),
            superclass=_str_or_none(data.get("superclass")),
            visibility=_str_or_none(data.get("visibility")),
            includes=_str_list(data.get("includes")),
            extends=_str_list(data.get("extends")),
            prepends=_str_list(data.get("prepends")),
            loc=RubyLoc.from_json(data.get("loc")),
        )


@dataclass(frozen=True)
class RubyCall:
    method: str
    receiver: Optional[str] = None
    receiver_kind: Optional[str] = None
    call_form: Optional[str] = None
    dynamic: bool = False
    target_candidates: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    reason: Optional[str] = None
    loc: RubyLoc = field(default_factory=RubyLoc)

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> "RubyCall":
        return RubyCall(
            method=_str(data.get("method")).strip(),
            receiver=_str_or_none(data.get("receiver")),
            receiver_kind=_str_or_none(data.get("receiver_kind")),
            call_form=_str_or_none(data.get("call_form")),
            dynamic=_bool(data.get("dynamic"), False),
            target_candidates=_str_list(data.get("target_candidates")),
            confidence=_float_or_none(data.get("confidence")),
            reason=_str_or_none(data.get("reason")),
            loc=RubyLoc.from_json(data.get("loc")),
        )


@dataclass(frozen=True)
class RubyMethod:
    name: str
    fq_name: Optional[str] = None
    owner: Optional[str] = None
    kind: str = "instance_method"
    visibility: str = "public"
    calls: List[RubyCall] = field(default_factory=list)
    loc: RubyLoc = field(default_factory=RubyLoc)

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> "RubyMethod":
        return RubyMethod(
            name=_str(data.get("name")).strip(),
            fq_name=_str_or_none(data.get("fq_name")),
            owner=_str_or_none(data.get("owner")),
            kind=_str(data.get("kind"), "instance_method").strip() or "instance_method",
            visibility=_str(data.get("visibility"), "public").strip() or "public",
            calls=[
                RubyCall.from_json(d)
                for d in _list_of_dicts(data.get("calls"))
                if _str(d.get("method")).strip()
            ],
            loc=RubyLoc.from_json(data.get("loc")),
        )


@dataclass(frozen=True)
class RubyReference:
    kind: str
    name: str
    fq_name: Optional[str] = None
    context: Optional[str] = None
    loc: RubyLoc = field(default_factory=RubyLoc)

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> "RubyReference":
        return RubyReference(
            kind=_str(data.get("kind"), "constant_ref").strip() or "constant_ref",
            name=_str(data.get("name")).strip(),
            fq_name=_str_or_none(data.get("fq_name")),
            context=_str_or_none(data.get("context")),
            loc=RubyLoc.from_json(data.get("loc")),
        )


@dataclass(frozen=True)
class RubyRailsFacts:
    role: str = "unknown"
    autoload_root: Optional[str] = None
    expected_constants: List[str] = field(default_factory=list)
    path_convention: Optional[str] = None
    dsl: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_json(data: Any) -> "RubyRailsFacts":
        d = _dict(data)
        return RubyRailsFacts(
            role=_str(d.get("role"), "unknown").strip() or "unknown",
            autoload_root=_str_or_none(d.get("autoload_root")),
            expected_constants=_str_list(d.get("expected_constants")),
            path_convention=_str_or_none(d.get("path_convention")),
            dsl=_dict(d.get("dsl")),
        )


@dataclass(frozen=True)
class RubyParsedFile:
    ok: bool
    parse_status: str
    file_path: str

    loc: Optional[int] = None
    sloc: Optional[int] = None
    comment_lines: Optional[int] = None
    blank_lines: Optional[int] = None
    comment_pct: Optional[float] = None
    size_bytes: Optional[int] = None

    requires: List[RubyRequire] = field(default_factory=list)
    declarations: List[RubyDeclaration] = field(default_factory=list)
    references: List[RubyReference] = field(default_factory=list)
    methods: List[RubyMethod] = field(default_factory=list)
    rails: RubyRailsFacts = field(default_factory=RubyRailsFacts)

    problems: List[str] = field(default_factory=list)
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_json(file_path: str, data: Mapping[str, Any]) -> "RubyParsedFile":
        return RubyParsedFile(
            ok=_bool(data.get("ok"), False),
            parse_status=_str(
                data.get("parse_status"), "ok" if data.get("ok") else "error"
            ),
            file_path=_str(data.get("file_path"), file_path).strip() or file_path,

            loc=_int_or_none(data.get("loc")),
            sloc=_int_or_none(data.get("sloc")),
            comment_lines=_int_or_none(data.get("comment_lines")),
            blank_lines=_int_or_none(data.get("blank_lines")),
            comment_pct=_float_or_none(data.get("comment_pct")),
            size_bytes=_int_or_none(data.get("size_bytes")),

            requires=[RubyRequire.from_json(d) for d in _list_of_dicts(data.get("requires"))],
            declarations=[
                RubyDeclaration.from_json(d)
                for d in _list_of_dicts(data.get("declarations"))
                if _str(d.get("name")).strip()
            ],
            references=[
                RubyReference.from_json(d)
                for d in _list_of_dicts(data.get("references"))
                if _str(d.get("name")).strip()
            ],
            methods=[
                RubyMethod.from_json(d)
                for d in _list_of_dicts(data.get("methods"))
                if _str(d.get("name")).strip()
            ],
            rails=RubyRailsFacts.from_json(data.get("rails")),

            problems=_str_list(data.get("problems")),
            error=_str_or_none(data.get("error")),
            raw=dict(data),
        )


@dataclass(frozen=True)
class RubyAnalysis:
    schema_version: str
    meta: Dict[str, Any]
    files: Dict[str, RubyParsedFile]
    indexes: Dict[str, Any]
    problems: List[str]
    raw: Dict[str, Any]

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> "RubyAnalysis":
        schema = _str(data.get("schema_version")).strip()
        files_raw = _dict(data.get("files"))
        files: Dict[str, RubyParsedFile] = {}

        for rel, rec in files_raw.items():
            if not isinstance(rec, Mapping):
                continue
            rel_s = str(rel).strip()
            if not rel_s:
                continue
            files[rel_s] = RubyParsedFile.from_json(rel_s, rec)

        return RubyAnalysis(
            schema_version=schema,
            meta=_dict(data.get("meta")),
            files=files,
            indexes=_dict(data.get("indexes")),
            problems=_str_list(data.get("problems")),
            raw=dict(data),
        )


def ruby_analysis_from_json(data: Mapping[str, Any]) -> RubyAnalysis:
    return RubyAnalysis.from_json(data)