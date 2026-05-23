# i_o/json_compression/node_codecs.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .types import ENUMS
from .util import encode_enum, decode_enum


def build_type_table(nodes: Dict[str, Any]) -> Tuple[List[str], Dict[str, int]]:
    types_seen: Set[str] = set()

    def extract_types(obj: Any) -> None:
        if isinstance(obj, dict):
            if "type_hint" in obj and isinstance(obj["type_hint"], str):
                types_seen.add(obj["type_hint"])
            if "return_type" in obj and isinstance(obj["return_type"], str):
                types_seen.add(obj["return_type"])
            for v in obj.values():
                extract_types(v)
        elif isinstance(obj, list):
            for item in obj:
                extract_types(item)

    for node in nodes.values():
        if isinstance(node, dict):
            extract_types(node)

    type_list = sorted(types_seen)
    type_to_idx = {t: i for i, t in enumerate(type_list)}
    return type_list, type_to_idx


def encode_type_hint(value: Any, type_to_idx: Dict[str, int]) -> Any:
    if isinstance(value, str) and value in type_to_idx:
        return type_to_idx[value]
    return value


def decode_type_hint(value: Any, type_strings: List[str]) -> Any:
    if isinstance(value, int) and 0 <= value < len(type_strings):
        return type_strings[value]
    return value


# --- Callable codec (shared by functions and methods) ----------------------

def compress_callable(func: Dict[str, Any], enums: Dict[str, List[str]], type_to_idx: Dict[str, int]) -> Dict[str, Any]:
    cf: Dict[str, Any] = {}

    if "name" in func:
        cf["n"] = func["name"]
    if "lineno" in func:
        cf["ln"] = func["lineno"]

    # FIX: Use key existence check instead of truthiness to preserve empty lists
    if "decorators" in func:
        cf["d"] = func["decorators"]

    # FIX: Use key existence check instead of truthiness to preserve None values
    if "docstring" in func:
        cf["doc"] = func["docstring"]

    if "return_type" in func:
        cf["rt"] = encode_type_hint(func["return_type"], type_to_idx)

    if "is_async" in func:
        cf["a"] = 1 if func["is_async"] else 0
    if "is_generator" in func:
        cf["g"] = 1 if func["is_generator"] else 0
    if "is_static" in func:
        cf["s"] = 1 if func["is_static"] else 0
    if "is_classmethod" in func:
        cf["cm"] = 1 if func["is_classmethod"] else 0
    if "is_property" in func:
        cf["pr"] = 1 if func["is_property"] else 0

    # FIX: Use key existence check instead of truthiness to preserve empty lists
    if "parameters" in func:
        cf["p"] = [compress_parameter(p, enums, type_to_idx) for p in func["parameters"]]

    return cf


def decode_callable(cf: Dict[str, Any], enums: Dict[str, List[str]], type_strings: List[str]) -> Dict[str, Any]:
    func: Dict[str, Any] = {}

    if "n" in cf:
        func["name"] = cf["n"]
    if "ln" in cf:
        func["lineno"] = cf["ln"]

    if "d" in cf:
        func["decorators"] = cf["d"]
    if "doc" in cf:
        func["docstring"] = cf["doc"]
    if "rt" in cf:
        func["return_type"] = decode_type_hint(cf["rt"], type_strings)

    if "a" in cf:
        func["is_async"] = bool(cf["a"])
    if "g" in cf:
        func["is_generator"] = bool(cf["g"])
    if "s" in cf:
        func["is_static"] = bool(cf["s"])
    if "cm" in cf:
        func["is_classmethod"] = bool(cf["cm"])
    if "pr" in cf:
        func["is_property"] = bool(cf["pr"])

    if "p" in cf:
        func["parameters"] = [decode_parameter(p, enums, type_strings) for p in cf["p"]]

    return func


def compress_functions_detailed(
    functions: Optional[List[Dict]],
    enums: Dict[str, List[str]],
    type_to_idx: Dict[str, int],
) -> Any:
    if not functions:
        return None
    return [compress_callable(func, enums, type_to_idx) for func in functions]


def decode_functions_detailed(
    compressed: List[Dict],
    enums: Dict[str, List[str]],
    type_strings: List[str],
) -> List[Dict[str, Any]]:
    return [decode_callable(cf, enums, type_strings) for cf in compressed]


def compress_parameter(param: Dict, enums: Dict[str, List[str]], type_to_idx: Dict[str, int]) -> Dict[str, Any]:
    cp: Dict[str, Any] = {}

    if "name" in param:
        cp["n"] = param["name"]

    if "type_hint" in param:
        cp["t"] = encode_type_hint(param["type_hint"], type_to_idx)

    # FIX: Preserve ALL default values including None
    # Only exclude if the key doesn't exist at all
    if "default" in param:
        cp["d"] = param["default"]

    if "kind" in param:
        kind_list = enums.get("param_kind", ENUMS["param_kind"])
        cp["k"] = encode_enum(param.get("kind"), kind_list)

    return cp


def decode_parameter(compressed: Dict, enums: Dict[str, List[str]], type_strings: List[str]) -> Dict[str, Any]:
    param: Dict[str, Any] = {}

    if "n" in compressed:
        param["name"] = compressed["n"]

    if "t" in compressed:
        param["type_hint"] = decode_type_hint(compressed["t"], type_strings)

    if "d" in compressed:
        param["default"] = compressed["d"]

    if "k" in compressed:
        kind_list = enums.get("param_kind", ENUMS["param_kind"])
        param["kind"] = decode_enum(compressed["k"], kind_list)

    return param


def compress_classes_detailed(
    classes: Optional[List[Dict]],
    enums: Dict[str, List[str]],
    type_to_idx: Dict[str, int],
) -> Any:
    if not classes:
        return None

    out = []
    for cls in classes:
        cc: Dict[str, Any] = {}

        if "name" in cls:
            cc["n"] = cls["name"]
        if "lineno" in cls:
            cc["ln"] = cls["lineno"]

        # FIX: Use key existence checks instead of truthiness to preserve empty lists and None values
        if "decorators" in cls:
            cc["d"] = cls["decorators"]
        if "docstring" in cls:
            cc["doc"] = cls["docstring"]
        if "bases" in cls:
            cc["b"] = cls["bases"]
        if "attributes" in cls:
            cc["at"] = cls["attributes"]

        if "is_dataclass" in cls:
            cc["dc"] = 1 if cls["is_dataclass"] else 0
        if "is_enum" in cls:
            cc["en"] = 1 if cls["is_enum"] else 0

        # FIX: Use key existence check instead of truthiness to preserve empty lists
        if "methods" in cls:
            cc["m"] = [compress_callable(method, enums, type_to_idx) for method in cls["methods"]]

        out.append(cc)

    return out


def decode_classes_detailed(
    compressed: List[Dict],
    enums: Dict[str, List[str]],
    type_strings: List[str],
) -> List[Dict[str, Any]]:
    decoded = []
    for cc in compressed:
        cls: Dict[str, Any] = {}

        if "n" in cc:
            cls["name"] = cc["n"]
        if "ln" in cc:
            cls["lineno"] = cc["ln"]

        if "d" in cc:
            cls["decorators"] = cc["d"]
        if "doc" in cc:
            cls["docstring"] = cc["doc"]
        if "b" in cc:
            cls["bases"] = cc["b"]
        if "at" in cc:
            cls["attributes"] = cc["at"]

        if "dc" in cc:
            cls["is_dataclass"] = bool(cc["dc"])
        if "en" in cc:
            cls["is_enum"] = bool(cc["en"])

        if "m" in cc:
            cls["methods"] = [decode_callable(method, enums, type_strings) for method in cc["m"]]

        decoded.append(cls)

    return decoded


def compress_globals_detailed(globals_list: Optional[List[Dict]], type_to_idx: Dict[str, int]) -> Any:
    if not globals_list:
        return None

    return {
        "schema": ["l", "n", "t", "v"],
        "legend": {"l": "lineno", "n": "name", "t": "type_hint", "v": "value"},
        "rows": [
            [g.get("lineno"), g.get("name"), encode_type_hint(g.get("type_hint"), type_to_idx), g.get("value")]
            for g in globals_list
        ],
    }


def decode_globals_detailed(compressed: Dict[str, Any], type_strings: List[str]) -> List[Dict[str, Any]]:
    schema = compressed.get("schema", [])
    legend = compressed.get("legend", {})
    rows = compressed.get("rows", [])

    decoded: List[Dict[str, Any]] = []
    for row in rows:
        item: Dict[str, Any] = {}
        for i, field_abbr in enumerate(schema):
            field_name = legend.get(field_abbr, field_abbr)
            if i < len(row):
                value = row[i]
                if field_name == "type_hint":
                    value = decode_type_hint(value, type_strings)
                item[field_name] = value
        decoded.append(item)
    return decoded


def compress_crosstalk(crosstalk_list: Optional[List[Dict]]) -> Any:
    if not crosstalk_list:
        return None

    return {
        "schema": ["e", "j", "k", "m", "r", "s", "w"],
        "legend": {
            "e": "evidence",
            "j": "join",
            "k": "kind",
            "m": "meta",
            "r": "raw",
            "s": "schema_version",
            "w": "where",
        },
        "rows": [
            [
                item.get("evidence"),
                item.get("join"),
                item.get("kind"),
                item.get("meta"),
                item.get("raw"),
                item.get("schema_version"),
                item.get("where"),
            ]
            for item in crosstalk_list
        ],
    }


def decode_crosstalk(compressed: Dict[str, Any]) -> List[Dict[str, Any]]:
    schema = compressed.get("schema", [])
    legend = compressed.get("legend", {})
    rows = compressed.get("rows", [])

    decoded: List[Dict[str, Any]] = []
    for row in rows:
        item: Dict[str, Any] = {}
        for i, field_abbr in enumerate(schema):
            field_name = legend.get(field_abbr, field_abbr)
            if i < len(row):
                item[field_name] = row[i]
        decoded.append(item)
    return decoded