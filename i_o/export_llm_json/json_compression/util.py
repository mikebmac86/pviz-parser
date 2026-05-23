# i_o/json_compression/util.py
from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from .types import KNOWN_FIELD_ABBRS


def encode_enum(value: Any, enum_list: List[str]) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return enum_list.index(value)
    except ValueError:
        return value


def decode_enum(value: Any, enum_list: List[str]) -> Any:
    if isinstance(value, int) and 0 <= value < len(enum_list):
        return enum_list[value]
    return value


def schema_fields(section: Dict[str, Any]) -> List[str]:
    schema = section.get("schema", [])
    legend = section.get("legend", {})
    if not isinstance(schema, list) or not isinstance(legend, dict):
        return []
    return [legend.get(abbr, abbr) for abbr in schema]


def find_field_indices(fields: List[str], names: Set[str]) -> Set[int]:
    out: Set[int] = set()
    for i, field_name in enumerate(fields):
        if field_name in names:
            out.add(i)
    return out


def make_schema(fields: List[str]) -> Tuple[List[str], Dict[str, str]]:
    schema: List[str] = []
    legend: Dict[str, str] = {}

    used = set()
    counter = 0

    for f in fields:
        abbr = KNOWN_FIELD_ABBRS.get(f)
        if abbr is None or abbr in used:
            while True:
                token = f"x{counter}"
                counter += 1
                if token not in used:
                    abbr = token
                    break
        schema.append(abbr)
        legend[abbr] = f
        used.add(abbr)

    return schema, legend
