"""Per-parameter performance breakdown + coverage-vs-configured-space stats.

Complements heatmap.py (entry time x leg SL% only) with two things it can't show:
which *values* of any one swept dimension tend to produce better results, and - just
as important - how much of the currently loaded sweep config's space has actually
been executed, per dimension and overall, so the next run can be aimed at what's
still untried instead of re-covering the same ground.

The list of "possible values" for a dimension always comes from
`to_sweep_config(cfg).vary` (the same flat vary-dict src.sweep.expand() Cartesian-
multiplies) - never hand-typed here - so it's automatically correct for whatever
ranges/checkboxes are active in the config right now, dual-basis toggles included.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable

from src.sweep import expand as expand_flat
from src.web.expand import to_sweep_config
from src.web.models import SweepUIConfig


def _num(v: Any) -> float | None:
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_num(v: float) -> str:
    # Whole numbers print without a trailing ".0" (e.g. "45%" not "45.0%").
    return f"{v:g}"


def _row_get(row: dict, *keys: str) -> Any:
    """First present, non-blank column among `keys` (checked in priority order) -
    lets a dimension read the same way regardless of whether this particular row
    was written by the dual-basis (kind/value pair) shape or an older single flat
    column, or whether a dict-typed field's sub-columns even exist yet in this CSV
    (build_fieldnames only ever includes columns some row actually used - see
    store.flatten)."""
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


# --- Each dimension's config-side value (straight from to_sweep_config(cfg).vary,
# i.e. None / a scalar / one of the {"kind"/"type", ...} dicts nest_combo pops back
# out) and its row-side value (read from whichever CSV columns that dict/scalar was
# flattened into) are normalized into the SAME small tuple shape before formatting -
# so the two sides can never drift into incompatible label formats. ---


def _sl_norm(v: Any) -> tuple | None:
    if v is None:
        return None
    return ("underlying" if v.get("kind") == "underlying_percentage" else "pct", v.get("value"))


def _sl_row(row: dict) -> tuple | None:
    kind = row.get("leg_risk.stoploss_pct.kind") or None
    if kind:
        value = _row_get(row, "leg_risk.stoploss_pct.value")
    else:
        value = _row_get(row, "leg_risk.stoploss_pct")
    if value is None:
        return None
    return ("underlying" if kind == "underlying_percentage" else "pct", _num(value))


def _scalar_norm(v: Any) -> tuple | None:
    if v is None:
        return None
    return ("value", v)


def _scalar_row_fn(*columns: str, numeric: bool = True) -> Callable[[dict], tuple | None]:
    def fn(row: dict) -> tuple | None:
        raw = _row_get(row, *columns)
        if raw is None:
            return None
        return ("value", _num(raw) if numeric else raw)

    return fn


def _trail_norm(v: Any) -> tuple | None:
    if v is None:
        return None
    return (v.get("type"), v.get("x"), v.get("y"))


def _trail_row(prefix: str) -> Callable[[dict], tuple | None]:
    def fn(row: dict) -> tuple | None:
        t = _row_get(row, f"{prefix}.type")
        x = _row_get(row, f"{prefix}.x")
        y = _row_get(row, f"{prefix}.y")
        if t is None and x is None and y is None:
            return None
        return (t, _num(x), _num(y))

    return fn


def _risk_norm(v: Any) -> tuple | None:
    if v is None:
        return None
    return (v.get("kind"), v.get("value"))


def _risk_row(prefix: str) -> Callable[[dict], tuple | None]:
    def fn(row: dict) -> tuple | None:
        kind = _row_get(row, f"{prefix}.kind")
        value = _row_get(row, f"{prefix}.value") if kind else _row_get(row, prefix)
        if value is None:
            return None
        return (kind, _num(value))

    return fn


def _trail_sl_norm(v: Any) -> tuple | None:
    if v is None:
        return None
    return (v.get("x"), v.get("y"), v.get("step"), v.get("trail_by"))


def _trail_sl_row(row: dict) -> tuple | None:
    x = _row_get(row, "trail_sl.x")
    y = _row_get(row, "trail_sl.y")
    step = _row_get(row, "trail_sl.step")
    trail_by = _row_get(row, "trail_sl.trail_by")
    if x is None and y is None and step is None and trail_by is None:
        return None
    return (_num(x), _num(y), _num(step), _num(trail_by))


def _strike_norm(v: Any) -> tuple | None:
    # The raw vary-list entry is always a {"kind": "offset"|"premium_closest",
    # "value": ...} choice dict (see expand._strike_choices) - not yet reduced to
    # the bare-string-or-{"mode",...} shape nest_combo/_strike_from_choice turns it
    # into for the final combo, which is what _strike_row reads back off the CSV.
    if v is None:
        return None
    if v.get("kind") == "offset":
        return ("offset", v.get("value"))
    return ("premium", v.get("value"))


def _strike_row(prefix: str) -> Callable[[dict], tuple | None]:
    def fn(row: dict) -> tuple | None:
        mode = _row_get(row, f"{prefix}.mode")
        if mode:
            return ("premium", _num(_row_get(row, f"{prefix}.value")))
        flat = _row_get(row, prefix)
        if flat is None:
            return None
        return ("offset", flat)

    return fn


def _label(t: tuple | None) -> str:
    if t is None:
        return "not set"
    if t[0] == "value":
        v = t[1]
        return _fmt_num(v) if isinstance(v, (int, float)) else str(v)
    if t[0] in ("pct", "underlying"):
        val = _fmt_num(t[1]) if t[1] is not None else "?"
        return f"{val}% (Underlying)" if t[0] == "underlying" else f"{val}%"
    if t[0] == "offset":
        return str(t[1])
    if t[0] == "premium":
        return f"premium~{_fmt_num(t[1])}" if t[1] is not None else "premium~?"
    if t[0] == "percentage":
        return f"{_fmt_num(t[1])}%"
    if t[0] == "amount":
        return f"Rs {_fmt_num(t[1])}"
    if len(t) == 3:  # trail: (type, x, y)
        kind, x, y = t
        return f"{kind or '?'} x={_fmt_num(x) if x is not None else '?'} y={_fmt_num(y) if y is not None else '?'}"
    if len(t) == 4:  # trail_sl: (x, y, step, trail_by)
        x, y, step, trail_by = t
        parts = [_fmt_num(v) if v is not None else "?" for v in (x, y, step, trail_by)]
        return f"x={parts[0]} y={parts[1]} step={parts[2]} trail_by={parts[3]}"
    return str(t)


@dataclasses.dataclass
class Dimension:
    key: str  # flat key in to_sweep_config(cfg).vary
    label: str
    normalize_config: Callable[[Any], tuple | None]
    row_value: Callable[[dict], tuple | None]


def _dimensions(cfg: SweepUIConfig) -> list[Dimension]:
    leg_prefix = "shared" if cfg.linked_ce_pe else "leg0"
    dims = [
        Dimension("entry_time", "Entry time", _scalar_norm, _scalar_row_fn("entry_time", numeric=False)),
        Dimension("exit_time", "Exit time", _scalar_norm, _scalar_row_fn("exit_time", numeric=False)),
        Dimension("legrisk_stoploss_pct", "Leg Stop Loss", _sl_norm, _sl_row),
        Dimension(
            "legrisk_target_pct", "Leg Target %", _scalar_norm,
            _scalar_row_fn("leg_risk.target_pct"),
        ),
        Dimension("legrisk_trail", "Leg Trail SL", _trail_norm, _trail_row("leg_risk.trail")),
        Dimension("overall_stoploss", "Overall Stop Loss", _risk_norm, _risk_row("stoploss")),
        Dimension("overall_target", "Overall Target", _risk_norm, _risk_row("target")),
        Dimension("trail_sl", "Overall Trail SL", _trail_sl_norm, _trail_sl_row),
        Dimension(f"{leg_prefix}_strike", "Strike (CE/PE)" if cfg.linked_ce_pe else "Strike (leg 1)",
                  _strike_norm, _strike_row("legs.0.strike")),
    ]
    if not cfg.linked_ce_pe and len(cfg.legs) > 1:
        dims.append(Dimension("leg1_strike", "Strike (leg 2)", _strike_norm, _strike_row("legs.1.strike")))
    return dims


def available_dimensions(cfg: SweepUIConfig) -> list[dict[str, str]]:
    """[{"key", "label"}, ...] for the UI's dimension picker - only dimensions this
    particular config actually varies (a checkbox left off means that dimension is
    always the same one "not set" value, not worth offering a breakdown for)."""
    sweep_cfg = to_sweep_config(cfg)
    out = []
    for d in _dimensions(cfg):
        values = sweep_cfg.vary.get(d.key, [])
        if len({_label(d.normalize_config(v)) for v in values}) > 1:
            out.append({"key": d.key, "label": d.label})
    return out


def build_param_breakdown(
    rows: list[dict],
    cfg: SweepUIConfig,
    dimension_key: str,
    *,
    metric: str = "return_max_dd",
) -> dict[str, Any]:
    """rows should already be status == "ok" and scoped to whatever instrument/DTE/
    entry-time filter the caller wants. Returns one row per distinct configured
    value of `dimension_key`, each showing whether it's been tried yet, how many
    times, and the average of `metric` - plus any value seen in the data that ISN'T
    among the currently configured choices (kept, not dropped, since narrowing the
    config after a run shouldn't make historical rows vanish from view; flagged via
    `in_current_config: false` so the UI can visually set it apart)."""
    dims = {d.key: d for d in _dimensions(cfg)}
    dim = dims.get(dimension_key)
    if dim is None:
        return {"label": dimension_key, "values": [], "total": 0}

    sweep_cfg = to_sweep_config(cfg)
    configured = sweep_cfg.vary.get(dimension_key, [])
    # De-duplicate by label - trail's X>=Y filtering etc. can otherwise leave the
    # "same-looking" combination appearing more than once in the raw vary list.
    configured_labels: list[str] = []
    seen_labels: set[str] = set()
    for v in configured:
        lbl = _label(dim.normalize_config(v))
        if lbl not in seen_labels:
            seen_labels.add(lbl)
            configured_labels.append(lbl)

    buckets: dict[str, list[dict]] = {lbl: [] for lbl in configured_labels}
    total = 0
    for row in rows:
        lbl = _label(dim.row_value(row))
        buckets.setdefault(lbl, [])
        buckets[lbl].append(row)
        total += 1

    values_out = []
    for lbl, cell_rows in buckets.items():
        vals: list[float] = []
        for r in cell_rows:
            raw = r.get(metric)
            if raw in (None, ""):
                continue
            try:
                vals.append(float(raw))
            except ValueError:
                continue
        values_out.append({
            "label": lbl,
            "in_current_config": lbl in seen_labels,
            "count": len(cell_rows),
            "pct_of_total": (100.0 * len(cell_rows) / total) if total else 0.0,
            "avg_metric": (sum(vals) / len(vals)) if vals else None,
        })

    # Configured-but-untried values first (in configured order, so gaps are easy to
    # spot), then everything actually seen, best average metric first.
    tried = [v for v in values_out if v["count"] > 0]
    untried = [v for v in values_out if v["count"] == 0]
    tried.sort(key=lambda v: (v["avg_metric"] is None, -(v["avg_metric"] or 0)))

    covered = len(configured_labels) - len(untried)
    return {
        "label": dim.label,
        "metric": metric,
        "total": total,
        "configured_count": len(configured_labels),
        "covered_count": max(covered, 0),
        "values": tried + untried,
    }


def overall_coverage(cfg: SweepUIConfig, executed_count: int) -> dict[str, Any]:
    """How many of the *currently configured* sweep's combos (post-exclude rules,
    ignoring any "limit" cap - that caps how many get queued to run, it doesn't
    shrink the space itself) have actually been executed so far. Reuses the same
    expand_flat() src.sweep already runs for real, with `limit` stripped so the
    count reflects the full configured grid regardless of what a prior run happened
    to cap itself at."""
    sweep_cfg = to_sweep_config(cfg)
    unlimited = dataclasses.replace(sweep_cfg, limit=None)
    total_configured = len(expand_flat(unlimited))
    pct = (100.0 * executed_count / total_configured) if total_configured else 0.0
    return {
        "executed": executed_count,
        "total_configured": total_configured,
        "pct": pct,
    }
