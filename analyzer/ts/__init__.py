#analyzer/ts/__init__.py
from __future__ import annotations

from .config import TSAnalyzerCfg
from .run import analyze_repo

__all__ = ["TSAnalyzerCfg", "analyze_repo"]
