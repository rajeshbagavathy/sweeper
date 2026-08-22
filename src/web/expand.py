from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.config import SweepConfig
from src.sweep import expand as expand_flat
from src.web.models import SweepUIConfig

UI_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "sweep_ui.yaml"


def _leg_prefix(i: int) -> str:
    return f"leg{i}_"


def to_sweep_config(cfg: SweepUIConfig) -> SweepConfig:
    """Build the flat vary-dict shape src.sweep.expand() already knows how to Cartesian-expand.

    Per-leg dimensions get a "leg{i}_" prefix so they survive the flat expansion; the
    leg list itself gets reassembled afterwards by nest_combo(), since apply_combination
    (src/form.py) expects a nested {"legs": [...]} shape, not flat leg0_lots keys.
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

    for i, leg in enumerate(cfg.legs):
        prefix = _leg_prefix(i)
        vary[f"{prefix}lots"] = leg.lots.as_list()

        if leg.strike_mode == "offset":
            vary[f"{prefix}offset"] = leg.offsets or ["ATM"]
        elif leg.strike_mode == "premium_range":
            vary[f"{prefix}premium_lower"] = leg.premium_lower.as_list()
            vary[f"{prefix}premium_upper"] = leg.premium_upper.as_list()
            exclude.append(f"{prefix}premium_upper <= {prefix}premium_lower")
        elif leg.strike_mode == "premium_closest":
            vary[f"{prefix}premium_value"] = leg.premium_value.as_list()

    if cfg.stoploss_enabled:
        vary["stoploss_pct"] = cfg.stoploss_pct.as_list()
    if cfg.target_enabled:
        vary["target_pct"] = cfg.target_pct.as_list()
        if cfg.stoploss_enabled:
            exclude.append("target_pct is not None and stoploss_pct is not None and target_pct <= stoploss_pct")

    if cfg.trail_sl_enabled:
        xs = cfg.trail_sl_x.as_list()
        ys = cfg.trail_sl_y.as_list()
        pairs: list[Any] = [{"x": x, "y": y} for x in xs for y in ys]
        if cfg.trail_sl_include_none:
            pairs = [None] + pairs
        vary["trail_sl"] = pairs

    return SweepConfig(fixed=fixed, vary=vary, exclude=exclude, limit=cfg.limit, shuffle=cfg.shuffle)


def nest_combo(flat_combo: dict[str, Any], cfg: SweepUIConfig) -> dict[str, Any]:
    """Turn one flat expanded combo back into the {legs: [...], ...} shape form.py expects."""
    combo = dict(flat_combo)

    legs = []
    for i, leg_cfg in enumerate(cfg.legs):
        prefix = _leg_prefix(i)
        leg: dict[str, Any] = {
            "action": leg_cfg.action,
            "option_type": leg_cfg.option_type,
            "lots": combo.pop(f"{prefix}lots"),
        }
        if leg_cfg.strike_mode == "offset":
            leg["strike"] = combo.pop(f"{prefix}offset")
        elif leg_cfg.strike_mode == "premium_range":
            leg["strike"] = {
                "mode": "premium_range",
                "lower": combo.pop(f"{prefix}premium_lower"),
                "upper": combo.pop(f"{prefix}premium_upper"),
            }
        elif leg_cfg.strike_mode == "premium_closest":
            leg["strike"] = {"mode": "premium_closest", "value": combo.pop(f"{prefix}premium_value")}
        legs.append(leg)
    combo["legs"] = legs

    combo.setdefault("stoploss_pct", None)
    combo.setdefault("target_pct", None)
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
