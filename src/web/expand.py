from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.config import SweepConfig
from src.sweep import expand as expand_flat
from src.web.models import LegRiskConfig, LegUIConfig, OverallRiskConfig, StrikeConfig, SweepUIConfig

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
    choices: list[dict[str, Any] | None] = []
    if leg_risk.trail_points_enabled:
        choices += [
            {"type": "Points", "x": x, "y": y}
            for x in leg_risk.trail_points_x.as_list()
            for y in leg_risk.trail_points_y.as_list()
        ]
    if leg_risk.trail_percentage_enabled:
        choices += [
            {"type": "Percentage", "x": x, "y": y}
            for x in leg_risk.trail_percentage_x.as_list()
            for y in leg_risk.trail_percentage_y.as_list()
        ]
    if not choices:
        choices = [None]
    return choices


def _overall_risk_choices(risk: OverallRiskConfig) -> list[dict[str, Any] | None]:
    """Union of percentage-basis ("Total Premium %") and amount-basis ("Max Loss" /
    "Max Profit") choices - either or both, not crossed."""
    choices: list[dict[str, Any] | None] = []
    if risk.use_percentage:
        choices += [{"kind": "percentage", "value": v} for v in risk.percentage_range.as_list()]
    if risk.use_amount:
        choices += [{"kind": "amount", "value": v} for v in risk.amount_range.as_list()]
    if not choices:
        choices = [None]
    return choices


def _add_leg_risk_vary(vary: dict[str, Any], leg_risk: LegRiskConfig) -> None:
    vary[f"{_LEGRISK_PREFIX}target_pct"] = leg_risk.target_pct.as_list() if leg_risk.target_enabled else [None]
    vary[f"{_LEGRISK_PREFIX}stoploss_pct"] = leg_risk.stoploss_pct.as_list() if leg_risk.stoploss_enabled else [None]
    vary[f"{_LEGRISK_PREFIX}trail"] = _trail_choices(leg_risk)


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
        exclude.append(
            f"{_LEGRISK_PREFIX}target_pct is not None and {_LEGRISK_PREFIX}stoploss_pct is not None "
            f"and {_LEGRISK_PREFIX}target_pct <= {_LEGRISK_PREFIX}stoploss_pct"
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

    if cfg.trail_sl_enabled:
        pairs: list[Any] = [
            {"x": x, "y": y, "step": s, "trail_by": t}
            for x in cfg.trail_sl_x.as_list()
            for y in cfg.trail_sl_y.as_list()
            for s in cfg.trail_sl_step.as_list()
            for t in cfg.trail_sl_trail_by.as_list()
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
    }

    combo["stoploss"] = combo.pop("overall_stoploss")
    combo["target"] = combo.pop("overall_target")
    combo.setdefault("trail_sl", None)

    return combo


def expand_ui_config(cfg: SweepUIConfig) -> list[dict[str, Any]]:
    flat_combos = expand_flat(to_sweep_config(cfg))
    return [nest_combo(c, cfg) for c in flat_combos]


def load_ui_config() -> SweepUIConfig:
    if not UI_CONFIG_PATH.exists():
        return SweepUIConfig()
    raw = yaml.safe_load(UI_CONFIG_PATH.read_text()) or {}
    return SweepUIConfig(**raw)


def save_ui_config(cfg: SweepUIConfig) -> None:
    UI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    UI_CONFIG_PATH.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False))
