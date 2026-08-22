from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SelectorValue = str | dict[str, str] | None


@dataclass
class LoginSelectors:
    email_input: SelectorValue
    password_input: SelectorValue
    submit_button: SelectorValue
    logged_in_marker: SelectorValue


@dataclass
class BuilderSelectors:
    url: str
    instrument_select: SelectorValue
    start_date_input: SelectorValue
    end_date_input: SelectorValue
    add_leg_button: SelectorValue
    leg_row: str  # format string, e.g. "#backtest-leg-{n}"
    leg_action: SelectorValue
    leg_option_type: SelectorValue
    leg_strike_selector: SelectorValue
    leg_strike_criteria: SelectorValue
    leg_strike_premium_lower: SelectorValue
    leg_strike_premium_upper: SelectorValue
    leg_strike_premium_value: SelectorValue
    leg_lots: SelectorValue
    leg_target_toggle: SelectorValue
    leg_target_type: SelectorValue
    leg_target_value: SelectorValue
    leg_stoploss_toggle: SelectorValue
    leg_stoploss_type: SelectorValue
    leg_stoploss_value: SelectorValue
    leg_trail_toggle: SelectorValue
    leg_trail_type: SelectorValue
    leg_trail_x: SelectorValue
    leg_trail_y: SelectorValue
    entry_time_input: SelectorValue
    exit_time_input: SelectorValue
    stoploss_toggle: SelectorValue
    stoploss_type: SelectorValue
    stoploss_value: SelectorValue
    target_toggle: SelectorValue
    target_type: SelectorValue
    target_value: SelectorValue
    trail_sl_toggle: SelectorValue
    trail_sl_mode: SelectorValue
    trail_sl_x: SelectorValue
    trail_sl_y: SelectorValue
    trail_sl_step: SelectorValue
    trail_sl_trail_by: SelectorValue
    reentry_type: SelectorValue
    reentry_count: SelectorValue
    run_backtest_button: SelectorValue


@dataclass
class ResultsSelectors:
    ready_marker: SelectorValue
    running_marker: SelectorValue
    error_marker: SelectorValue
    metrics: dict[str, SelectorValue] = field(default_factory=dict)


@dataclass
class Selectors:
    login: LoginSelectors
    builder: BuilderSelectors
    results: ResultsSelectors


def load_selectors(path: Path) -> Selectors:
    raw = yaml.safe_load(path.read_text())
    return Selectors(
        login=LoginSelectors(**raw["login"]),
        builder=BuilderSelectors(**raw["builder"]),
        results=ResultsSelectors(**raw["results"]),
    )


@dataclass
class SweepConfig:
    fixed: dict[str, Any]
    vary: dict[str, list[Any]]
    exclude: list[str]
    limit: int | None
    shuffle: bool


def load_sweep(path: Path) -> SweepConfig:
    raw = yaml.safe_load(path.read_text())
    return SweepConfig(
        fixed=raw.get("fixed", {}),
        vary=raw.get("vary", {}),
        exclude=raw.get("exclude", []) or [],
        limit=raw.get("limit"),
        shuffle=bool(raw.get("shuffle", False)),
    )
