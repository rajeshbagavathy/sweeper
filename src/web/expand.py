from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import yaml

from src.config import SweepConfig
from src.lazy_leg import _ELIGIBLE_SL_PCT_MAX, _ELIGIBLE_SL_PCT_MIN, derive_lazy_leg
from src.sweep import count_or_estimate
from src.sweep import expand as expand_flat
from src.sweep import iter_shuffled_combos
from src.web.models import (
    MAX_SANE_UNDERLYING_SL_PCT,
    LegRiskConfig,
    LegUIConfig,
    OverallRiskConfig,
    StrikeConfig,
    SweepUIConfig,
)

UI_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "sweep_ui.yaml"

_SHARED_PREFIX = "shared_"
_LEGRISK_PREFIX = "legrisk_"


def _leg_prefix(i: int) -> str:
    return f"leg{i}_"


def _strike_choices(strike: StrikeConfig) -> list[dict[str, Any]]:
    """Union (not cross product) of every strike choice implied by the checkboxes."""
    choices: list[dict[str, Any]] = []
    if strike.use_offset:
        choices += [{"kind": "offset", "value": o} for o in (strike.offsets or ["ATM"])]
    if strike.use_closest_premium:
        choices += [{"kind": "premium_closest", "value": v} for v in strike.premium_range.as_list()]
    if not choices:
        # Neither checkbox on - fall back rather than produce a leg with no strike at all.
        choices = [{"kind": "offset", "value": "ATM"}]
    return choices


def _strike_from_choice(choice: dict[str, Any]) -> str | dict[str, Any]:
    if choice["kind"] == "offset":
        return choice["value"]
    return {"mode": "premium_closest", "value": choice["value"]}


def _add_leg_vary(vary: dict[str, Any], prefix: str, leg: LegUIConfig) -> None:
    vary[f"{prefix}lots"] = leg.lots.as_list()
    vary[f"{prefix}strike"] = _strike_choices(leg.strike)


def _trail_choices(leg_risk: LegRiskConfig) -> list[dict[str, Any] | None]:
    """X ("for every X point/percent move") must be >= Y ("trail SL by Y") - AlgoTest
    itself warns "Having a TSL value where X < Y will lead to backtest and live
    results not matching" for X < Y, so those pairs are dropped here rather than
    generated and excluded later (also shrinks the combination count directly).
    None (no trailing at all) is always unioned in alongside whatever's enabled, so a
    sweep with Trail SL on still also tries every other dimension without it."""
    choices: list[dict[str, Any] | None] = []
    if leg_risk.trail_points_enabled:
        choices += [
            {"type": "Points", "x": x, "y": y}
            for x in leg_risk.trail_points_x.as_list()
            for y in leg_risk.trail_points_y.as_list()
            if x >= y
        ]
    if leg_risk.trail_percentage_enabled:
        choices += [
            {"type": "Percentage", "x": x, "y": y}
            for x in leg_risk.trail_percentage_x.as_list()
            for y in leg_risk.trail_percentage_y.as_list()
            if x >= y
        ]
    if not choices:
        return [None]
    return [None] + choices


def _overall_risk_choices(risk: OverallRiskConfig) -> list[dict[str, Any] | None]:
    """Union of percentage-basis ("Total Premium %") and amount-basis ("Max Loss" /
    "Max Profit") choices - either or both, not crossed. None (not set at all) is
    always unioned in too, alongside whichever basis is enabled."""
    choices: list[dict[str, Any] | None] = []
    if risk.use_percentage:
        choices += [{"kind": "percentage", "value": v} for v in risk.percentage_range.as_list()]
    if risk.use_amount:
        choices += [{"kind": "amount", "value": v} for v in risk.amount_range.as_list()]
    if not choices:
        return [None]
    return [None] + choices


def _stoploss_choices(leg_risk: LegRiskConfig) -> list[dict[str, Any] | None]:
    """Union of premium-basis ("Percent (%)") and underlying-basis ("Underlying %")
    leg-level Stop Loss choices - either or both, not crossed. None (no leg-level
    Stop Loss at all) is always unioned in too, same convention as
    _overall_risk_choices above.

    A value above MAX_SANE_UNDERLYING_SL_PCT on the underlying basis is silently
    dropped here (not raised as an error) - a % move that large in the underlying
    practically never happens intraday, so it's not a real stop loss, same as
    has_hard_stop_loss in src/web/portfolio.py treats it. Filtered at generation
    time rather than validated on the config model itself, since the model is
    reconstructed from PAST data all over this app (saved executions, the
    persisted sweep_ui.yaml, "save combo to AlgoTest") - rejecting construction
    there would break loading anything saved before this cutoff existed, not just
    block a NEW bad entry. Confirmed live: exactly that broke "Save basket in
    AlgoTest" for a sweep run off a stale on-disk config still carrying 15-20%."""
    choices: list[dict[str, Any] | None] = []
    if leg_risk.stoploss_enabled:
        choices += [{"kind": "percentage", "value": v} for v in leg_risk.stoploss_pct.as_list()]
    if leg_risk.stoploss_underlying_enabled:
        choices += [
            {"kind": "underlying_percentage", "value": v}
            for v in leg_risk.stoploss_underlying_pct.as_list()
            if v <= MAX_SANE_UNDERLYING_SL_PCT
        ]
    if not choices:
        return [None]
    return [None] + choices


def _momentum_choices(leg_risk: LegRiskConfig) -> list[dict[str, Any] | None]:
    """Union (not cross product) of every Simple Momentum choice implied by the
    Up/Down checkboxes - same convention as _trail_choices above, None always included."""
    choices: list[dict[str, Any] | None] = []
    if leg_risk.momentum_up_enabled:
        choices += [{"direction": "UP", "value": v} for v in leg_risk.momentum_up_pct.as_list()]
    if leg_risk.momentum_down_enabled:
        choices += [{"direction": "DOWN", "value": v} for v in leg_risk.momentum_down_pct.as_list()]
    if not choices:
        return [None]
    return [None] + choices


def _reentry_sl_choices(leg_risk: LegRiskConfig) -> list[dict[str, Any] | None]:
    """One choice per checked re-entry type (count is a single fixed value for phase 1,
    not swept, and only meaningful for RE_ASAP/RE_COST - LAZY_LEG has no count) -
    each checked type is tried as a separate alternative, plus None (no re-entry
    at all) always unioned in. A combo always lands on exactly ONE of these,
    same as AlgoTest's own Re-entry on SL dropdown only ever holding one type per
    leg - so RE_ASAP/RE_COST/LAZY_LEG are automatically mutually exclusive PER
    COMBO without any special-casing here, the same way RE_ASAP and RE_COST
    already were before LAZY_LEG existed."""
    if not leg_risk.reentry_sl_enabled or not leg_risk.reentry_sl_types:
        return [None]
    choices: list[dict[str, Any]] = []
    for t in leg_risk.reentry_sl_types:
        if t == "LAZY_LEG":
            choices.append({"type": "LAZY_LEG"})
        else:
            choices.append({"type": t, "count": leg_risk.reentry_sl_count})
    return [None] + choices


def _add_leg_risk_vary(vary: dict[str, Any], leg_risk: LegRiskConfig) -> None:
    # None (not set) is always unioned in alongside the enabled range, so a sweep with
    # e.g. leg-level Stop Loss on still also tries every other dimension without it -
    # same convention as _trail_choices/_momentum_choices/_reentry_sl_choices below.
    vary[f"{_LEGRISK_PREFIX}target_pct"] = (
        [None] + leg_risk.target_pct.as_list() if leg_risk.target_enabled else [None]
    )
    vary[f"{_LEGRISK_PREFIX}stoploss_pct"] = _stoploss_choices(leg_risk)
    vary[f"{_LEGRISK_PREFIX}trail"] = _trail_choices(leg_risk)
    vary[f"{_LEGRISK_PREFIX}momentum"] = _momentum_choices(leg_risk)
    vary[f"{_LEGRISK_PREFIX}reentry_sl"] = _reentry_sl_choices(leg_risk)


def to_sweep_config(cfg: SweepUIConfig) -> SweepConfig:
    """Build the flat vary-dict shape src.sweep.expand() already knows how to Cartesian-expand.

    Per-leg dimensions get a "leg{i}_" prefix (or "shared_" when linked_ce_pe) so they
    survive the flat expansion; nest_combo() reassembles the nested shape src/form.py
    expects afterwards.
    """
    fixed: dict[str, Any] = {
        "instrument": cfg.instrument,
        "start_date": cfg.start_date,
        "end_date": cfg.end_date,
    }
    vary: dict[str, list[Any]] = {
        "entry_time": cfg.entry_time.as_list(),
        "exit_time": cfg.exit_time.as_list(),
    }
    exclude = list(cfg.exclude)

    if cfg.linked_ce_pe:
        _add_leg_vary(vary, _SHARED_PREFIX, cfg.shared_leg)
    else:
        for i, leg in enumerate(cfg.legs):
            _add_leg_vary(vary, _leg_prefix(i), leg)

    _add_leg_risk_vary(vary, cfg.leg_risk)
    if cfg.leg_risk.target_enabled and cfg.leg_risk.stoploss_enabled:
        # Only meaningful to compare when Stop Loss landed on the same (premium %)
        # basis as Target - an underlying-% Stop Loss isn't comparable to a
        # premium-% Target, so leave that combination in (same convention as the
        # overall-target-vs-overall-stoploss exclude below).
        exclude.append(
            f"{_LEGRISK_PREFIX}target_pct is not None and {_LEGRISK_PREFIX}stoploss_pct is not None "
            f"and {_LEGRISK_PREFIX}stoploss_pct['kind'] == 'percentage' "
            f"and {_LEGRISK_PREFIX}target_pct <= {_LEGRISK_PREFIX}stoploss_pct['value']"
        )
    if cfg.leg_risk.trail_points_enabled or cfg.leg_risk.trail_percentage_enabled:
        # stoploss_pct now also unions in a None baseline (see _add_leg_risk_vary) -
        # AlgoTest trails the leg's own Stop Loss, so a combo with trailing set but no
        # concrete stoploss_pct value would be invalid (same constraint the
        # LegRiskConfig validator already enforces at the config level).
        exclude.append(f"{_LEGRISK_PREFIX}trail is not None and {_LEGRISK_PREFIX}stoploss_pct is None")
    if "LAZY_LEG" in cfg.leg_risk.reentry_sl_types:
        # A combo that lands on reentry_sl=LAZY_LEG is only ever ACTUALLY
        # different from reentry_sl=None (already generated as its own
        # separate combo above) when the leg's own Stop Loss is
        # percentage-based and within src/lazy_leg.py's own eligible range -
        # see derive_lazy_leg. Outside that range, "Re-entry on SL" never even
        # gets toggled on for that leg (see src/form.py's _apply_leg_risk),
        # so the resulting backtest is byte-identical to the None sibling
        # combo that already exists for the same other parameters - just
        # under a separate combo_id that falsely claims Lazy Leg was used.
        # Confirmed live: this produced thousands of wasted, mislabeled
        # duplicate rows in one real sweep before this exclude existed.
        exclude.append(
            f"{_LEGRISK_PREFIX}reentry_sl is not None and {_LEGRISK_PREFIX}reentry_sl['type'] == 'LAZY_LEG' "
            f"and ({_LEGRISK_PREFIX}stoploss_pct is None "
            f"or {_LEGRISK_PREFIX}stoploss_pct['kind'] != 'percentage' "
            f"or not ({_ELIGIBLE_SL_PCT_MIN} <= {_LEGRISK_PREFIX}stoploss_pct['value'] <= {_ELIGIBLE_SL_PCT_MAX}))"
        )

    vary["overall_stoploss"] = _overall_risk_choices(cfg.overall_stoploss)
    vary["overall_target"] = _overall_risk_choices(cfg.overall_target)
    # Only meaningful to compare when both landed on the same basis (a percentage and
    # an absolute amount aren't comparable) - otherwise leave the combination in.
    exclude.append(
        "overall_target is not None and overall_stoploss is not None "
        "and overall_target['kind'] == overall_stoploss['kind'] "
        "and overall_target['value'] <= overall_stoploss['value']"
    )
    # A combo with neither a leg-level nor an overall Stop Loss has nothing capping
    # its loss before Trail SL (if even that's on) has locked any profit in - on a
    # bad enough day that's unlimited downside, not a strategy worth backtesting at
    # all, let alone recommending. Only enforced when the config actually offers a
    # Stop Loss to choose (leg-level or overall, any basis) - if neither is enabled
    # at all, there's no SL dimension being swept in the first place, and excluding
    # "neither is set" would just wipe out every combo in an unrelated sweep (e.g.
    # one only exploring momentum or re-entry) rather than enforcing anything
    # meaningful. Mirrors has_hard_stop_loss in src/web/portfolio.py, which applies
    # the same rule to combos already sitting in a results CSV.
    if (
        cfg.leg_risk.stoploss_enabled
        or cfg.leg_risk.stoploss_underlying_enabled
        or cfg.overall_stoploss.use_percentage
        or cfg.overall_stoploss.use_amount
    ):
        exclude.append(f"{_LEGRISK_PREFIX}stoploss_pct is None and overall_stoploss is None")

    # Underlying % leg Stop Loss triggers off a move in the UNDERLYING's own price,
    # not the strategy's own P&L - on its own (no overall Stop Loss backing it up)
    # there's nothing capping the strategy's actual loss, only a leg-level SL that
    # may never trip even while the position bleeds. Per explicit instruction: an
    # underlying-basis leg SL is only ever generated alongside a real overall Stop
    # Loss, never standalone. Independent of MAX_SANE_UNDERLYING_SL_PCT above - this
    # applies to every underlying-basis value, not just an insane one.
    exclude.append(
        f"{_LEGRISK_PREFIX}stoploss_pct is not None "
        f"and {_LEGRISK_PREFIX}stoploss_pct['kind'] == 'underlying_percentage' "
        "and overall_stoploss is None"
    )
    # AlgoTest's "Simple Momentum" entry criteria delays entering a leg until the
    # underlying has already moved a given % - combined with an underlying-basis SL
    # (which triggers off that SAME underlying move, just measured from the SL's own
    # reference price instead), the two chase the same signal from opposite ends and
    # don't compose meaningfully. Per explicit instruction: never generated together.
    exclude.append(
        f"{_LEGRISK_PREFIX}stoploss_pct is not None "
        f"and {_LEGRISK_PREFIX}stoploss_pct['kind'] == 'underlying_percentage' "
        f"and {_LEGRISK_PREFIX}momentum is not None"
    )

    if cfg.trail_sl_enabled:
        # step ("for every increase in profit by") must be >= trail_by ("trail profit
        # by") - the same X >= Y trailing constraint as the leg-level Trail SL above.
        pairs: list[Any] = [
            {"x": x, "y": y, "step": s, "trail_by": t}
            for x in cfg.trail_sl_x.as_list()
            for y in cfg.trail_sl_y.as_list()
            for s in cfg.trail_sl_step.as_list()
            for t in cfg.trail_sl_trail_by.as_list()
            if s >= t
        ]
        if cfg.trail_sl_include_none:
            pairs = [None] + pairs
        vary["trail_sl"] = pairs

    return SweepConfig(fixed=fixed, vary=vary, exclude=exclude, limit=cfg.limit, shuffle=cfg.shuffle)


def _pop_leg(combo: dict[str, Any], prefix: str, action: str, option_type: str) -> dict[str, Any]:
    return {
        "action": action,
        "option_type": option_type,
        "lots": combo.pop(f"{prefix}lots"),
        "strike": _strike_from_choice(combo.pop(f"{prefix}strike")),
    }


def nest_combo(flat_combo: dict[str, Any], cfg: SweepUIConfig) -> dict[str, Any]:
    """Turn one flat expanded combo back into the {legs: [...], leg_risk: {...}, ...} shape form.py expects."""
    combo = dict(flat_combo)

    if cfg.linked_ce_pe:
        shared = cfg.shared_leg
        lots = combo.pop(f"{_SHARED_PREFIX}lots")
        strike = _strike_from_choice(combo.pop(f"{_SHARED_PREFIX}strike"))
        legs = [
            {"action": shared.action, "option_type": "CE", "lots": lots, "strike": strike},
            {"action": shared.action, "option_type": "PE", "lots": lots, "strike": strike},
        ]
    else:
        legs = [_pop_leg(combo, _leg_prefix(i), leg_cfg.action, leg_cfg.option_type) for i, leg_cfg in enumerate(cfg.legs)]
    combo["legs"] = legs

    combo["leg_risk"] = {
        "target_pct": combo.pop(f"{_LEGRISK_PREFIX}target_pct"),
        "stoploss_pct": combo.pop(f"{_LEGRISK_PREFIX}stoploss_pct"),
        "trail": combo.pop(f"{_LEGRISK_PREFIX}trail"),
        "momentum": combo.pop(f"{_LEGRISK_PREFIX}momentum"),
        "reentry_sl": combo.pop(f"{_LEGRISK_PREFIX}reentry_sl"),
    }

    # Triggered by THIS combo's own reentry_sl choice landing on "LAZY_LEG" (see
    # _reentry_sl_choices) - a genuine per-combo alternative alongside RE_ASAP/
    # RE_COST/None, not a separate sweep-wide toggle. The actual per-leg values
    # (strike/SL%/trail/momentum) are still derived here rather than swept -
    # see src/lazy_leg.py's derive_lazy_leg. A leg that isn't eligible (SL%
    # outside 25-60, underlying-based SL, no SL at all, or an instrument with no
    # confirmed momentum threshold) simply gets no "lazy_leg" key, same as any
    # other not-applicable optional field.
    reentry_sl = combo["leg_risk"]["reentry_sl"]
    if reentry_sl is not None and reentry_sl["type"] == "LAZY_LEG":
        for leg in legs:
            lazy_leg = derive_lazy_leg(leg, combo["leg_risk"], cfg.instrument)
            if lazy_leg is not None:
                leg["lazy_leg"] = lazy_leg

    combo["stoploss"] = combo.pop("overall_stoploss")
    combo["target"] = combo.pop("overall_target")
    combo.setdefault("trail_sl", None)

    return combo


def expand_ui_config(cfg: SweepUIConfig) -> list[dict[str, Any]]:
    flat_combos = expand_flat(to_sweep_config(cfg))
    return [nest_combo(c, cfg) for c in flat_combos]


def estimate_ui_config(cfg: SweepUIConfig, sample_size: int = 5_000, seed: int | None = None) -> dict[str, Any]:
    """Preview's answer to "how many combinations, and what do a few look like" -
    exact for a config small enough that expand_ui_config is already fast, a fast
    sampled estimate otherwise (see src.sweep.count_or_estimate/EXACT_COUNT_THRESHOLD).
    Nests the sample the same way expand_ui_config's full result would be nested."""
    result = count_or_estimate(to_sweep_config(cfg), sample_size=sample_size, seed=seed)
    result["sample"] = [nest_combo(c, cfg) for c in result["sample"]]
    return result


def iter_shuffled_ui_combos(cfg: SweepUIConfig, seed: int | None = None) -> Iterator[dict[str, Any]]:
    """The large-sweep alternative to expand_ui_config - see
    src.sweep.iter_shuffled_combos for why this exists and what "shuffled" buys a
    caller that consumes it incrementally (e.g. run_sweep_multiprocess)."""
    sweep = to_sweep_config(cfg)
    for flat_combo in iter_shuffled_combos(sweep, seed=seed):
        yield nest_combo(flat_combo, cfg)


def probe_combos(cfg: SweepUIConfig) -> list[dict[str, Any]]:
    """Every distinct nested "shape" nest_combo can produce for this config (e.g.
    trail_sl being None vs {"x": .., "y": ..}), without materializing the full
    Cartesian product - one probe combo per (key, value) pair in the flat vary dict,
    with every OTHER key held at its own first value. nest_combo pops each prefix from
    a single fixed flat key, never conditionally on another key's value, so each key's
    contribution to a combo's shape is independent of every other key's - varying one
    key at a time this way still touches every shape the full expansion could produce,
    at O(values) cost instead of O(raw product). Used to build CSV fieldnames for a
    sweep too large to fully expand - see RunState.start.

    ONE exception to "independent of every other key's": a leg's "lazy_leg"
    shape (see src/lazy_leg.py's derive_lazy_leg) depends on TWO keys at
    once - legrisk_reentry_sl landing on LAZY_LEG *and* legrisk_stoploss_pct
    landing on an eligible value, simultaneously - never just one. No
    single-key-varied-at-a-time probe above can ever produce that
    combination (whichever of the two you vary, the OTHER stays at its own
    baseline - always None for both, per _reentry_sl_choices/_stoploss_
    choices), so build_fieldnames() fed only from these probes would never
    discover the "legs.N.lazy_leg.*" columns for a sweep large enough to use
    this path - confirmed live: a real sweep's own CSV silently dropped every
    such column on every resume, for every row, because of exactly this. One
    extra, deliberately combined probe (added only when the config can
    actually produce this shape) closes the gap.

    A SECOND, narrower gap sits inside that same combined probe: derive_lazy_
    leg's own "strike" output is a plain offset STRING when the leg's own
    strike landed on offset mode, but a {"mode","value"} premium-closest DICT
    when it landed on that mode instead - two different flattened column
    shapes ("legs.N.lazy_leg.strike" vs "legs.N.lazy_leg.strike.mode"/".value"),
    a THIRD key (that same leg's own strike choice) combined with the two
    above. The one combined probe leaves every other key at ITS OWN baseline
    (first) value, so it only ever exercises whichever shape that leg's
    strike vary list happens to list first - if the list also includes the
    OTHER shape, that shape's columns are never discovered, and every row
    that lands on it later silently drops its lazy strike on write (append_
    row's extrasaction="ignore"). Confirmed live: exactly this - a sweep whose
    legs used premium-closest strikes - broke "Download Reports" replay for
    every affected DTE-2 row (Playwright timing out trying to select a blank
    strike in the "Create New Lazy Leg" popup). One extra probe per leg whose
    strike vary list offers both shapes closes this gap too."""
    sweep = to_sweep_config(cfg)
    keys = list(sweep.vary.keys())
    value_lists = [sweep.vary[k] for k in keys]
    baseline = {k: values[0] for k, values in zip(keys, value_lists)}
    probes = []
    for k, values in zip(keys, value_lists):
        for v in values:
            flat = dict(sweep.fixed)
            flat.update(baseline)
            flat[k] = v
            probes.append(nest_combo(flat, cfg))

    if "LAZY_LEG" in cfg.leg_risk.reentry_sl_types:
        stoploss_key = f"{_LEGRISK_PREFIX}stoploss_pct"
        eligible_sl = next(
            (
                v for v in sweep.vary.get(stoploss_key, [])
                if v is not None
                and v.get("kind") == "percentage"
                and _ELIGIBLE_SL_PCT_MIN <= v["value"] <= _ELIGIBLE_SL_PCT_MAX
            ),
            None,
        )
        if eligible_sl is not None:
            base_lazy_flat = dict(sweep.fixed)
            base_lazy_flat.update(baseline)
            base_lazy_flat[f"{_LEGRISK_PREFIX}reentry_sl"] = {"type": "LAZY_LEG"}
            base_lazy_flat[stoploss_key] = eligible_sl
            probes.append(nest_combo(base_lazy_flat, cfg))

            strike_keys = (
                [f"{_SHARED_PREFIX}strike"]
                if cfg.linked_ce_pe
                else [f"{_leg_prefix(i)}strike" for i in range(len(cfg.legs))]
            )
            for strike_key in strike_keys:
                baseline_kind = baseline.get(strike_key, {}).get("kind")
                alt_choice = next(
                    (v for v in sweep.vary.get(strike_key, []) if v.get("kind") != baseline_kind),
                    None,
                )
                if alt_choice is not None:
                    flat = dict(base_lazy_flat)
                    flat[strike_key] = alt_choice
                    probes.append(nest_combo(flat, cfg))

    return probes


def load_ui_config() -> SweepUIConfig:
    if not UI_CONFIG_PATH.exists():
        return SweepUIConfig()
    raw = yaml.safe_load(UI_CONFIG_PATH.read_text()) or {}
    return SweepUIConfig(**raw)


def save_ui_config(cfg: SweepUIConfig) -> None:
    UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    UI_CONFIG_PATH.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False))
