#diagnostics/logging.py
from __future__ import annotations
from typing import Any, Dict, Iterable, Optional, Set
import sys
import time

# --------------------------------------------------------------------
# Known categories / kinds registry (aggregated from all modules)
# --------------------------------------------------------------------

ALL_CATEGORIES: Set[str] = {
    # Core visual + layout
    "PAINT",
    "SCENE",
    "LAYOUT",

    # Build / analyzer pipeline
    "BUILD",
    "BUILD_UTIL",
    "FACT",
    "ART",
    "SRC",
    "PRUNE",
    "FINALIZE",
    "SEED",
    "NODEINFO",
    "INFO_POPUP",
    "HEADLESS",

    # Zones mode (scene + package zones)
    "ZONES",
    "ZONES_CTX",
    "ZONES_PICK",
    "ZONES_PLAN",
    "ZONES_OUTLINE",
    "ZONES_PKG",
    "PKGZ",

    # Workspace / store / background
    "WS",
    "STORE",
    "NODEINFO",
    "BG",
    "PRECOMP",
    "AN_BRIDGE",

    # App / UI flow
    "APP",
    "VSOPEN",
    "ENTRY",
    "MENU",
    "PROMPT",
    "BOOT",
    "MODE",

    # Misc / legacy categories still in code
    "EDGE",
    "NODE",
    "FACTORY",   # legacy factory logs
    "INIT",
    "DUP",       # e.g. "DUP:UI"
    "DEAD",      # e.g. "DEAD:UI"
}

ALL_KINDS: Set[str] = {
    # ---- ARTIFACT IO ----
    "ART:WRITE",

    # ---- APP / ENTRY / PROMPT / MODE ----
    "APP:init_clean",
    "ENTRY:run_gui_roots",
    "MODE:trigger",
    "MODE:after",
    "PROMPT:startup_before_dialog",
    "PROMPT:startup_show_dialog",
    "PROMPT:startup_choice_add_folder",
    "PROMPT:disable_add_folder_method",
    "PROMPT:workspace_saved",
    "PROMPT:add_folder_ok",

    # ---- WS / BOOT / PRECOMP ----
    "BOOT:prompt_result",
    "BOOT:workspace_active_after_prompt",
    "WS:after_selected_begin",
    "WS:after_selected_end",
    "PRECOMP:call_start_precompute",
    "PRECOMP:resolve",
    "PRECOMP:launch",
    "PRECOMP:start_precompute_ok",
    "PRECOMP:progress",
    "PRECOMP:done",

    # ---- BUILD CORE ----
    "BUILD:entry_roots",
    "BUILD:entry_mode",
    "BUILD:inputs",
    "BUILD:art_ctx",
    "BUILD:cleanup",
    "BUILD:home",
    "BUILD:start",
    "BUILD:classic_begin",
    "BUILD:class_end",
    "BUILD:result",
    "BUILD:metrics",
    "BUILD:artifacts_begin",
    "BUILD:artifacts_done",
    "BUILD:flow",

    # ---- FACT: NODE + EDGE FACTORY ----
    "FACT:make_node:enter",
    "FACT:make_node:success",
    "FACT:EDGE:skip_pkg_router",
    "FACT:EDGE:ctor_ok",

    # ---- LAYOUT ----
    "LAYOUT:enter",
    "LAYOUT:applier_begin",
    "LAYOUT:applier_graph_source",
    "LAYOUT:applier_graph_full",
    "LAYOUT:applier_phase",
    "LAYOUT:applier_seed_resolve",
    "LAYOUT:applier_no_seed",
    "LAYOUT:exit",
    "LAYOUT:post",
    "LAYOUT:done",
    "LAYOUT:render_input",
    "LAYOUT:applier_prior_plan_present",
    "LAYOUT:applier_layout_graph",
    "LAYOUT:applier_planner_call",
    "LAYOUT:plan_scope",
    "LAYOUT:planner_enter",
    "LAYOUT:plan_begin",
    "LAYOUT:plan_prior_preload",
    "LAYOUT:plan_seed_locked",
    "LAYOUT:plan_wave_ids",
    "LAYOUT:plan_wave",
    "LAYOUT:plan_core_lane_assign",
    "LAYOUT:plan_core_cols",
    "LAYOUT:plan_leaf_piles",
    "LAYOUT:plan_done",
    "LAYOUT:planner_exit",
    "LAYOUT:applier_planner_returned",
    "LAYOUT:applier_plan_stats",
    "LAYOUT:applier_plan_sample",
    "LAYOUT:applier_id_map_stats",
    "LAYOUT:applier_apply_pixels_begin",
    "LAYOUT:applier_apply_pixels_end",
    "LAYOUT:applier_node_movement",
    "LAYOUT:applier_end",

    # ---- ZONES (classic + pkg) ----
    "ZONES:ENTRY_not_detected",
    "ZONES:classic_build_begin",
    "ZONES:classic_build_end",
    "ZONES:enter_emit_plan_artifacts",
    "ZONES:enter_build_nodes_only_plan",
    "ZONES:planner_call_from_build_plan",
    "ZONES:exit_build_nodes_only_plan",
    "ZONES:exit_emit_plan_artifacts",
    "ZONES:load_zones_meta",
    "ZONES:load_zones_ok",
    "ZONES:NODE:sel_change",
    "ZONES:NODE:pos_change",
    "ZONES:NODE:paint",
    "ZONES:NODE:ident",
    "ZONES:NODE:info_hit",
    "ZONES:NODE:h:init_rect",
    "ZONES:NODE:h:band_rederive",
    "ZONES:NODE:h:init_band",
    "ZONES:NODE:init",
    "ZONES:NODE:init_scene",
    "ZONES:NODE:layout_children",
    "ZONES:NODE:scene_change",

    # ---- PKGZ ----
    "PKGZ:build_entry",
    "PKGZ:build_entry_full_edges_path",
    "PKGZ:artifact_read_begin",
    "PKGZ:artifact_read_ok",
    "PKGZ:build_pkgzones_begin",
    "PKGZ:build_pkgzones_end",
    "PKGZ:artifact_write_ok",
    "PKGZ:dbg_build_ctx_from_store",
    "PKGZ:pkgzones_register_mode",

    # ---- ZONES_PKG ----
    "ZONES_PKG:build_begin",
    "ZONES_PKG:build_infer_nodes_from_edges",
    "ZONES_PKG:build_pkgs_raw",
    "ZONES_PKG:build_pkgs_filtered",
    "ZONES_PKG:build_pkgs_sorted",
    "ZONES_PKG:build_grid_assigned",
    "ZONES_PKG:build_zone",
    "ZONES_PKG:build_children_summary",
    "ZONES_PKG:build_done",

    # ---- PRUNE ----
    "PRUNE:begin",
    "PRUNE:end",

    # ---- SEED ----
    "SEED:enter",
    "SEED:variants",
    "SEED:exact_key_match",

    # ---- SCENE ----
    "SCENE:build_nodes:begin",
    "SCENE:build_nodes:end",
    "SCENE:probe",

    # ---- FINALIZE ----
    "FINALIZE:begin",
    "FINALIZE:end",

    # ---- ART (summary / reachable_write etc.) ----
    "ART:summary",
    "ART:write",
    "ART:reachable_write",

    "VSOPEN:raw_path", 
    "VSOPEN:missing", 
    "VSOPEN:which_code", 
    "VSOPEN:no_code_cli",
    "VSOPEN:cmd",
    "VSOPEN:launched",
    "VSOPEN:launch_error",

}

# --------------------------------------------------------------------
# Global config state
# --------------------------------------------------------------------

# None  → allow all kinds/categories
# set() → allow all kinds/categories (just no whitelist)
_ALLOWED: Optional[Set[str]] = None   # <-- FIX: no stray "PGKZ" string
_SILENCED: Set[str] = set()  # start with everything silenced by default

_MAX_PER_KIND: Dict[str, int] = {}
_COUNTS: Dict[str, int] = {}


def configure(
    *,
    allowed: Optional[Iterable[str]] = None,
    silenced: Iterable[str] = (),
    max_per_kind: Dict[str, int] | None = None,
) -> None:
    """
    Global config:

      allowed:
        - None     → allow all kinds/categories
        - iterable → ONLY these categories/kinds are printed
                     (e.g. {"PAINT", "ZONES:zone_pass_begin"})

      silenced:
        - iterable of categories/kinds to completely suppress
          (e.g. {"PAINT", "BUILD:entry"})

      max_per_kind:
        - dict token -> max number of lines (after which we auto-suppress)
        - token may be a full kind ("PAINT:font_before_fit") or a category
          ("PAINT"). Full-kind caps take precedence over category caps.

    Important precedence:
      - Explicit individual **silence** wins over everything.
      - Explicit **allow** (kind or category) can override a **category silence**.
      - Category silences apply to all kinds in that category unless an
        individual allow (kind or category) says otherwise.
    """
    global _ALLOWED, _SILENCED, _MAX_PER_KIND, _COUNTS
    _ALLOWED = set(allowed) if allowed is not None else None
    _SILENCED = set(silenced or ())
    _MAX_PER_KIND = dict(max_per_kind or {})
    _COUNTS = {}  # reset counts whenever config changes


def _category_of(kind: str) -> str:
    """Return the category part of a kind, e.g. 'PAINT' for 'PAINT:foo'."""
    cat, _, _ = kind.partition(":")
    return cat or kind


def log_event(kind: str, *parts: Any, **fields: Any) -> None:
    """
    Core logger. 'kind' should be a stable token like:

      "ZONES:zone_pass_begin"
      "ZONES:seed_protect"
      "PAINT:font_before_fit"
      "BUILD:entry"

    Filtering, throttling, and silencing are done on this 'kind' and its
    category (prefix before the first ':').

    Precedence rules:

      1. If kind is explicitly silenced → always suppressed.
      2. Else if category is silenced:
           - Suppress unless the kind or category is explicitly allowed.
      3. Whitelist (allowed) is checked on both kind and category.
      4. Per-kind/per-category caps apply after all of the above.
    """
    cat = _category_of(kind)

    # ----------------- Explicit silencing -----------------
    # Individual silence always wins.
    if kind in _SILENCED:
        return

    # Explicit allow (kind or category) when a whitelist exists.
    explicitly_allowed = (
        _ALLOWED is not None
        and (kind in _ALLOWED or cat in _ALLOWED)
    )

    # Category silence applies only if this kind/category is *not* explicitly allowed.
    if cat in _SILENCED and not explicitly_allowed:
        return

    # ----------------- Whitelist filtering -----------------
    if _ALLOWED is not None:
        # If a whitelist is active, we allow when either the individual
        # kind or its category appears in the whitelist.
        if not explicitly_allowed:
            return

    # ----------------- Per-kind / per-category caps -----------------
    max_n = _MAX_PER_KIND.get(kind)
    if max_n is None:
        max_n = _MAX_PER_KIND.get(cat)

    if max_n is not None:
        key = kind
        cnt = _COUNTS.get(key, 0)
        if cnt >= max_n:
            if cnt == max_n:
                _COUNTS[key] = cnt + 1
                print(
                    f"[{kind}] further logs suppressed after {max_n} entries.",
                    file=sys.stderr,
                )
            else:
                _COUNTS[key] = cnt + 1
            return
        _COUNTS[key] = cnt + 1

    # ----------------- Pretty print -----------------
    ts = time.strftime("%H:%M:%S")
    msg = " ".join(str(p) for p in parts if p is not None)
    kv  = " ".join(f"{k}={v!r}" for k, v in fields.items())

    line = f"[{ts}] {kind}"
    if msg:
        line += " " + msg
    if kv:
        line += " " + kv

    print(line, file=sys.stderr)
