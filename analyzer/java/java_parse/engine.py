#saas_analyzer/analyzer/java/parse_java/engine.py

from __future__ import annotations

import os
from pathlib import Path

from .models import JavaParsedFile
from . import regex_engine
from . import javaparser_engine


def _engine_choice() -> str:
    v = os.environ.get("PVIZ_JAVA_PARSER_ENGINE", "auto").strip().lower()
    if v not in {"auto", "regex", "javaparser"}:
        return "auto"
    return v


def parse_java_file(path: Path) -> JavaParsedFile:
    """
    Public parse entry: chooses engine.
    """
    choice = _engine_choice()

    if choice == "regex":
        return regex_engine.parse_java_file(path)

    if choice == "javaparser":
        jf = javaparser_engine.parse_java_file(path)
        return jf

    # auto: try javaparser first; fall back to regex if jar/java unavailable or parse error
    jf = javaparser_engine.parse_java_file(path)
    if jf.parse_status == "ok":
        return jf

    # If JavaParser fails, regex fallback prevents pipeline breakage.
    return regex_engine.parse_java_file(path)


# Text-based extractors:
# For now we keep these regex-backed because they’re used by FolderIndex
# and JavaParser CLI is file-based (no stdin contract assumed).
# If you later add a `--stdin` mode to the jar, we can swap these too.
extract_package = regex_engine.extract_package
extract_declared_types = regex_engine.extract_declared_types
extract_imports = regex_engine.extract_imports
