from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set


@dataclass(frozen=True)
class ResolveResult:
    kind: str          # "internal" | "external" | "dynamic"
    spec: str          # normalized spec as given
    resolved: Optional[str]  # rel_path of the target file, or None
    reason: str = ""


# ---------------------------------------------------------------------------
# Ruby stdlib / gem prefixes
#
# Rule: a require spec whose first path segment matches one of these is
# treated as external.  The prefixes end with "/" so that "json" matches
# "json" and "json/add/core" but NOT a user gem named "jsonify".
# ---------------------------------------------------------------------------
RUBY_STDLIB_PREFIXES = (
    # Core stdlib (Ruby 3.x)
    "abbrev", "base64", "benchmark", "bigdecimal", "bundler",
    "cgi", "coverage", "csv", "date", "dbm", "debug",
    "delegate", "digest", "drb", "English", "erb", "etc",
    "expect", "fcntl", "fiddle", "fileutils", "find", "forwardable",
    "gdbm", "getoptlong", "io/", "ipaddr", "irb", "json",
    "logger", "matrix", "monitor", "mutex_m", "net/", "nkf",
    "objspace", "observer", "open-uri", "open3", "openssl", "optparse",
    "ostruct", "pathname", "pp", "prettyprint", "prime", "pstore",
    "psych", "pty", "rake", "rbconfig", "rdoc", "readline",
    "resolv", "rexml/", "rinda/", "ripper", "rss/", "rubygems",
    "securerandom", "set", "shellwords", "singleton", "socket",
    "stringio", "strscan", "sync", "syslog", "tempfile", "time",
    "timeout", "tmpdir", "tracer", "tsort", "un", "unicode_normalize",
    "uri", "weakref", "webrick", "win32ole", "yaml", "zlib",
    # Common gems that are never internal
    "rails", "active_record", "active_support", "active_model",
    "action_controller", "action_view", "action_mailer", "action_cable",
    "action_dispatch", "sprockets", "turbo", "stimulus",
    "sidekiq", "resque", "delayed_job",
    "devise", "pundit", "cancancan",
    "carrierwave", "shrine", "active_storage",
    "httparty", "faraday", "rest-client", "typhoeus",
    "nokogiri", "mechanize",
    "rspec", "minitest", "factory_bot", "faker", "capybara", "webmock",
    "pry", "byebug", "amazing_print",
    "dotenv", "figaro",
    "redis", "hiredis", "connection_pool",
    "pg", "mysql2", "sqlite3",
    "bootsnap", "listen", "spring",
    "thor", "gli", "highline",
    "jwt", "bcrypt", "rack",
)

# Fast-reject for bare names that are unambiguously external without
# needing the prefix walk (avoids iterating the full prefix list).
_STDLIB_BARE: frozenset[str] = frozenset({
    "json", "yaml", "csv", "set", "date", "time", "logger",
    "pathname", "fileutils", "tempfile", "open-uri",
    "rubygems", "bundler", "rake", "rails",
    "active_record", "active_support",
    "rspec", "minitest",
})


def normalize_ruby_require_spec(spec: str) -> str:
    """Strip leading ./ and trailing .rb from a require spec."""
    s = (spec or "").strip()
    if not s:
        return ""
    if s.startswith("./"):
        s = s[2:]
    if s.endswith(".rb"):
        s = s[:-3]
    return s


def is_stdlib_or_gem_require(spec: str) -> bool:
    """
    Return True if spec is unambiguously stdlib or a well-known gem, and
    should never resolve to an internal file.

    Uses prefix matching on the first path segment to avoid false-positives
    on user code that shares a name with a stdlib module.
    """
    s = normalize_ruby_require_spec(spec)
    if not s:
        return False
    # Fast path
    first_segment = s.split("/")[0]
    if first_segment in _STDLIB_BARE:
        return True
    # Prefix walk: match against known stdlib/gem names (exact first segment
    # or path prefix like "net/http")
    return any(
        s == prefix or s.startswith(prefix + "/") or s == prefix.rstrip("/")
        for prefix in RUBY_STDLIB_PREFIXES
    )


def fq_decl(module_name: Optional[str], simple: str) -> Optional[str]:
    """Build a fully-qualified Ruby constant name: Module::Simple."""
    mod = (module_name or "").strip()
    name = (simple or "").strip()
    if not name:
        return None
    return f"{mod}::{name}" if mod else name


def resolve_ruby_require(
    spec: str,
    *,
    kind: str = "require",
    require_spec_to_files: Dict[str, List[str]],
    fq_decl_to_file: Dict[str, str],
) -> List[ResolveResult]:
    """
    Resolve a Ruby require spec to zero or more ResolveResults.

    Resolution order:
      1. Empty / dynamic spec    -> []
      2. stdlib / gem prefix     -> external (reason="stdlib_or_gem")
      3. require_relative        -> already a canonical rel_path; look up directly
      4. Bare require spec:
           a. in require_spec_to_files index  -> internal (reason="require_index")
           b. normalized spec as fq_decl key  -> internal (reason="fq_decl_match")
           c. no match                        -> external (reason="spec_not_found")
    """
    if not spec or spec.strip() == "":
        return []

    # Dynamic requires (spec is None or contains interpolation markers) are
    # flagged upstream; we receive an empty spec for them.
    norm = normalize_ruby_require_spec(spec)
    if not norm:
        return []

    # Stdlib / well-known gem: always external
    if is_stdlib_or_gem_require(norm):
        return [ResolveResult(kind="external", spec=norm, resolved=None, reason="stdlib_or_gem")]

    # require_relative: the Ruby RequireIndex already resolved this to a
    # canonical rel_path (with .rb extension).  Look it up directly.
    if kind == "require_relative":
        # Try both with and without .rb suffix since the index keys vary
        for key in (norm, norm + ".rb", spec.strip()):
            files = require_spec_to_files.get(key)
            if files:
                return [
                    ResolveResult(kind="internal", spec=norm, resolved=f, reason="require_relative_resolved")
                    for f in sorted(files)
                ]
        # Resolved path not in index — file may exist but wasn't in the scan
        return [ResolveResult(kind="external", spec=norm, resolved=None, reason="require_relative_not_indexed")]

    # Bare require: consult require_spec_to_files index first
    files = require_spec_to_files.get(norm)
    if files:
        return [
            ResolveResult(kind="internal", spec=norm, resolved=f, reason="require_index")
            for f in sorted(files)
        ]

    # Fallback: treat spec as a constant path (e.g. "my_gem/my_class" ->
    # "MyGem::MyClass") and look up in the declaration index.
    camelled = _camelize_path(norm)
    if camelled:
        f2 = fq_decl_to_file.get(camelled)
        if f2:
            return [ResolveResult(kind="internal", spec=norm, resolved=f2, reason="fq_decl_match")]

    return [ResolveResult(kind="external", spec=norm, resolved=None, reason="spec_not_found")]


def _camelize_path(spec: str) -> str:
    """
    Convert a require path to a Ruby constant name heuristic.
    "my_module/my_class" -> "MyModule::MyClass"
    """
    parts = spec.split("/")
    segments = []
    for part in parts:
        words = part.split("_")
        segments.append("".join(w.capitalize() for w in words if w))
    return "::".join(s for s in segments if s)