from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class KotlinImport:
    path: str
    alias: Optional[str] = None
    is_wildcard: bool = False


@dataclass(frozen=True)
class KotlinDeclaration:
    name: str
    visibility: str = "public"
    annotations: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class KotlinFunction:
    name: str
    visibility: str = "public"
    is_suspend: bool = False
    is_operator: bool = False
    is_infix: bool = False
    annotations: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class KotlinTypeAlias:
    name: str
    visibility: str = "public"
    target: Optional[str] = None
    annotations: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class KotlinProperty:
    name: str
    visibility: str = "public"
    is_var: bool = False
    annotations: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class KotlinParsedFile:
    ok: bool
    parse_status: str
    file_path: Path
    package_name: Optional[str] = None
    imports: List[KotlinImport] = field(default_factory=list)
    classes: List[KotlinDeclaration] = field(default_factory=list)
    interfaces: List[KotlinDeclaration] = field(default_factory=list)
    objects: List[KotlinDeclaration] = field(default_factory=list)
    enums: List[KotlinDeclaration] = field(default_factory=list)
    functions: List[KotlinFunction] = field(default_factory=list)
    type_aliases: List[KotlinTypeAlias] = field(default_factory=list)
    properties: List[KotlinProperty] = field(default_factory=list)
    annotations: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    loc_code: Optional[int] = None
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


def _list_of_dicts(v: Any) -> List[Dict[str, Any]]:
    return [x for x in (v or []) if isinstance(x, dict)]


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _decl_from_dict(d: Dict[str, Any]) -> KotlinDeclaration:
    return KotlinDeclaration(
        name=str(d.get("name") or "").strip(),
        visibility=str(d.get("visibility") or "public"),
        annotations=[str(x) for x in (d.get("annotations") or []) if str(x).strip()],
    )


def _func_from_dict(d: Dict[str, Any]) -> KotlinFunction:
    return KotlinFunction(
        name=str(d.get("name") or "").strip(),
        visibility=str(d.get("visibility") or "public"),
        is_suspend=bool(d.get("is_suspend", False)),
        is_operator=bool(d.get("is_operator", False)),
        is_infix=bool(d.get("is_infix", False)),
        annotations=[str(x) for x in (d.get("annotations") or []) if str(x).strip()],
    )


def _typealias_from_dict(d: Dict[str, Any]) -> KotlinTypeAlias:
    return KotlinTypeAlias(
        name=str(d.get("name") or "").strip(),
        visibility=str(d.get("visibility") or "public"),
        target=_str_or_none(d.get("target")),
        annotations=[str(x) for x in (d.get("annotations") or []) if str(x).strip()],
    )


def _property_from_dict(d: Dict[str, Any]) -> KotlinProperty:
    return KotlinProperty(
        name=str(d.get("name") or "").strip(),
        visibility=str(d.get("visibility") or "public"),
        is_var=bool(d.get("is_var", False)),
        annotations=[str(x) for x in (d.get("annotations") or []) if str(x).strip()],
    )


def kotlin_parsed_file_from_json(data: Dict[str, Any]) -> KotlinParsedFile:
    imports: List[KotlinImport] = []
    for imp in _list_of_dicts(data.get("imports")):
        p = str(imp.get("path") or "").strip()
        if not p:
            continue
        imports.append(KotlinImport(path=p, alias=_str_or_none(imp.get("alias")), is_wildcard=bool(imp.get("is_wildcard", False))))

    return KotlinParsedFile(
        ok=bool(data.get("ok", False)),
        parse_status=str(data.get("parse_status") or ("ok" if data.get("ok") else "error")),
        file_path=Path(str(data.get("file_path") or "")),
        package_name=_str_or_none(data.get("package_name")),
        imports=imports,
        classes=[_decl_from_dict(d) for d in _list_of_dicts(data.get("classes")) if str(d.get("name") or "").strip()],
        interfaces=[_decl_from_dict(d) for d in _list_of_dicts(data.get("interfaces")) if str(d.get("name") or "").strip()],
        objects=[_decl_from_dict(d) for d in _list_of_dicts(data.get("objects")) if str(d.get("name") or "").strip()],
        enums=[_decl_from_dict(d) for d in _list_of_dicts(data.get("enums")) if str(d.get("name") or "").strip()],
        functions=[_func_from_dict(d) for d in _list_of_dicts(data.get("functions")) if str(d.get("name") or "").strip()],
        type_aliases=[_typealias_from_dict(d) for d in _list_of_dicts(data.get("type_aliases")) if str(d.get("name") or "").strip()],
        properties=[_property_from_dict(d) for d in _list_of_dicts(data.get("properties")) if str(d.get("name") or "").strip()],
        annotations=[str(x) for x in (data.get("annotations") or []) if str(x).strip()],
        problems=[str(x) for x in (data.get("problems") or []) if str(x).strip()],
        loc_code=int(data["loc_code"]) if data.get("loc_code") is not None else None,
        error=_str_or_none(data.get("error")),
        raw=dict(data),
    )
