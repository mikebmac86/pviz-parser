from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple

from analyzer.ruby.parse_ruby.models import RubyParsedFile

# Extensions that the Ruby parser handles.
_RUBY_EXTENSIONS = frozenset({".rb", ".rake", ".gemspec", ".ru"})
_RUBY_EXTENSIONLESS = frozenset({"Gemfile", "Rakefile", "Capfile", "Guardfile",
                                  "Vagrantfile", "config.ru"})


def _is_ruby_file(path: Path) -> bool:
    return (
        path.suffix.lower() in _RUBY_EXTENSIONS
        or path.name in _RUBY_EXTENSIONLESS
    )


def parse_ruby_any_file(
    path: Path,
    parsed_cache: Optional[dict] = None,
    cfg: Any = None,
) -> Tuple[Optional[RubyParsedFile], List[str]]:
    """
    Return (RubyParsedFile | None, warnings).

    Unlike the Kotlin dispatcher which invokes a live parser per file, the
    Ruby parser runs as a batch subprocess over all files at once
    (pviz-ruby-extract).  By the time this dispatcher is called, the analysis
    result has already been obtained and cached on cfg.ruby_parse_cache or
    passed in as parsed_cache.

    Resolution order:
      1. parsed_cache dict (keyed by rel_path string)
      2. cfg.ruby_parse_cache
      3. Return (None, [warning]) — file was not in the batch result
    """
    p = Path(path)

    if not _is_ruby_file(p):
        return None, [f"ruby_dispatch_unsupported_suffix:{p.suffix.lower() or p.name}"]

    # Determine the rel_path key used by the Ruby parser (POSIX string).
    rel_key = str(p).replace("\\", "/")

    cache = parsed_cache
    if cache is None and cfg is not None:
        cache = getattr(cfg, "ruby_parse_cache", None)

    if isinstance(cache, dict):
        pf = cache.get(rel_key)
        if pf is not None:
            if isinstance(pf, RubyParsedFile):
                warns = list(pf.problems or [])
                if pf.error:
                    warns.append(pf.error)
                return pf, warns
            # Cache contains raw dicts (deserialized JSON, not yet hydrated)
            try:
                from analyzer.ruby.parse_ruby.models import RubyParsedFile as _RPF
                hydrated = _RPF.from_json(rel_key, pf)
                warns = list(hydrated.problems or [])
                if hydrated.error:
                    warns.append(hydrated.error)
                return hydrated, warns
            except Exception as e:
                return None, [f"ruby_dispatch_hydration_error:{type(e).__name__}:{str(e)[:200]}"]

    return None, [f"ruby_dispatch_not_in_cache:{rel_key}"]