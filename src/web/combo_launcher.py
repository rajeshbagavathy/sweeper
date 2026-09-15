"""Reconstructs a combo dict from a stored CSV row and opens a real, headed browser
with every field prefilled (and the backtest submitted) - so a combo whose scraped
metrics don't match a manual re-entry can be inspected directly, instead of retyping
15+ settings by hand and hoping nothing was missed.

The reverse of src/store.py's flatten(): flatten() is a generic dict/list flattener,
but its output is ambiguous to invert in general (a leaf value and a one-key nested
dict look identical once both are blanked to "" for CSV rows that didn't use that
shape - see the strike field, which is a plain offset string for one leg and a
{"mode", "value"} dict for another). row_to_combo() only needs to invert the one
fixed schema _build_row()/nest_combo() actually produce, so it's written against
that fixed shape rather than as a fully generic unflatten.
"""
from __future__ import annotations

import copy
import csv
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src import browser, store
from src.auth import ensure_logged_in, is_logged_in
from src.config import Selectors, load_selectors
from src.form import apply_combination
from src.locators import resolve
from src.results import (
    apply_result_settings,
    ensure_brokerage_rate,
    parse_number,
    scrape_metrics,
    wait_for_result,
)
from src.web.expand import load_ui_config

SELECTORS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "selectors.yaml"


def _num(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _strike(row: dict[str, str], prefix: str) -> str | dict[str, Any]:
    mode = row.get(f"{prefix}.mode")
    if mode:
        return {"mode": mode, "value": _num(row.get(f"{prefix}.value"))}
    return row[prefix]


def _lazy_leg(row: dict[str, str], leg_prefix: str) -> dict[str, Any] | None:
    """Rebuilds a leg's OWN "lazy_leg" dict (see src/lazy_leg.py's
    derive_lazy_leg and src/web/expand.py's nest_combo, which attach this to a
    LEG, not to leg_risk) from its "{leg_prefix}.lazy_leg.*" flattened columns.
    None whenever that leg was never eligible for one in the first place (see
    below) - option_type is the cheapest reliable "was this ever populated"
    check, since every attached lazy_leg dict always has one."""
    prefix = f"{leg_prefix}.lazy_leg"
    option_type = row.get(f"{prefix}.option_type")
    if not option_type:
        return None
    return {
        "option_type": option_type,
        "strike": _strike(row, f"{prefix}.strike"),
        "stoploss_pct": _kind_value(row, f"{prefix}.stoploss_pct"),
        "trail": _trail(row, f"{prefix}.trail"),
        "momentum": _momentum(row, f"{prefix}.momentum"),
    }


def _legs(row: dict[str, str]) -> list[dict[str, Any]]:
    legs = []
    i = 0
    while f"legs.{i}.action" in row:
        leg: dict[str, Any] = {
            "action": row[f"legs.{i}.action"],
            "option_type": row[f"legs.{i}.option_type"],
            "lots": _num(row[f"legs.{i}.lots"]),
            "strike": _strike(row, f"legs.{i}.strike"),
        }
        lazy_leg = _lazy_leg(row, f"legs.{i}")
        if lazy_leg is not None:
            leg["lazy_leg"] = lazy_leg
        legs.append(leg)
        i += 1
    return legs


def _trail(row: dict[str, str], prefix: str = "leg_risk.trail") -> dict[str, Any] | None:
    if not row.get(f"{prefix}.type"):
        return None
    return {"type": row[f"{prefix}.type"], "x": _num(row[f"{prefix}.x"]), "y": _num(row[f"{prefix}.y"])}


def _momentum(row: dict[str, str], prefix: str = "leg_risk.momentum") -> dict[str, Any] | None:
    if not row.get(f"{prefix}.direction"):
        return None
    return {"direction": row[f"{prefix}.direction"], "value": _num(row[f"{prefix}.value"])}


def _reentry_sl(row: dict[str, str]) -> dict[str, Any] | None:
    reentry_type = row.get("leg_risk.reentry_sl.type")
    if not reentry_type:
        return None
    if reentry_type == "LAZY_LEG":
        # No "count" concept for Lazy Leg (see src/web/expand.py's own
        # _reentry_sl_choices) - unlike RE_ASAP/RE_COST below, this flat marker
        # alone is all form.py's _apply_leg_risk needs (it's a deliberate
        # no-op for this type there - see its own docstring); the actual
        # per-leg values live in each leg's own "lazy_leg" dict instead (see
        # _lazy_leg/_legs above), which apply_combination reads directly.
        # Confirmed live: reading "leg_risk.reentry_sl.count" here for a Lazy
        # Leg row crashed relaunch entirely ("int() argument must be ... not
        # 'NoneType'") - that column is always blank for this type, there was
        # never a count to read.
        return {"type": "LAZY_LEG"}
    # count is matched against a literal button value ("1".."6") when applied, not a
    # free-typed number - must stay a plain int, not "2.0".
    return {"type": reentry_type, "count": int(_num(row["leg_risk.reentry_sl.count"]))}


def _kind_value(row: dict[str, str], prefix: str) -> dict[str, Any] | None:
    kind = row.get(f"{prefix}.kind")
    if kind:
        return {"kind": kind, "value": _num(row[f"{prefix}.value"])}
    # Backward compatibility: rows saved before the leg Stop Loss dual-basis feature
    # (2026-08-28) stored this as one flat numeric column - always the percentage-of-
    # premium basis, since that was the only option AlgoTest offered here at the time.
    # Without this fallback, replaying an older row silently drops its leg-level Stop
    # Loss while Trail SL (read from separate, unaffected columns) still gets applied -
    # AlgoTest then rejects the combo ("Stop loss value should be set for leg trail")
    # and the page never produces a result, which looks like a hang until the 180s
    # timeout (x however many retries) finally gives up.
    legacy = row.get(prefix)
    if legacy:
        return {"kind": "percentage", "value": _num(legacy)}
    return None


def _trail_sl(row: dict[str, str]) -> dict[str, Any] | None:
    if not row.get("trail_sl.x"):
        return None
    return {
        "x": _num(row["trail_sl.x"]),
        "y": _num(row["trail_sl.y"]),
        "step": _num(row["trail_sl.step"]),
        "trail_by": _num(row["trail_sl.trail_by"]),
    }


def row_to_combo(row: dict[str, str]) -> dict[str, Any]:
    """Rebuild the nested combo dict src/form.py's apply_combination() expects from
    one CSV row (as produced by csv.DictReader) - the same shape nest_combo() builds
    before _build_row() flattens it for storage."""
    return {
        "instrument": row["instrument"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "entry_time": row["entry_time"],
        "exit_time": row["exit_time"],
        "legs": _legs(row),
        "leg_risk": {
            "target_pct": _num(row.get("leg_risk.target_pct")),
            "stoploss_pct": _kind_value(row, "leg_risk.stoploss_pct"),
            "trail": _trail(row),
            "momentum": _momentum(row),
            "reentry_sl": _reentry_sl(row),
        },
        "stoploss": _kind_value(row, "stoploss"),
        "target": _kind_value(row, "target"),
        "trail_sl": _trail_sl(row),
    }


def _parse_dte_values(dte: str | None) -> list[int]:
    """The DTE filter applied to a combo's results - stored per-row (e.g. "0" or
    "0,1,2"), unlike brokerage/slippage which AlgoTest treats as one-time-per-session
    settings (see _settings_to_apply)."""
    if not dte:
        return []
    return [int(v) for v in dte.split(",")]


def _compare_metrics(row: dict[str, str], raw_metrics: dict[str, str | None]) -> list[dict[str, Any]]:
    """Every metric this app scrapes, stored value vs. freshly re-run value - lets the
    UI show a plain match/mismatch per metric instead of leaving you to eyeball 13
    numbers, which is exactly what triggered this feature in the first place."""
    comparison = []
    for name, live_raw in raw_metrics.items():
        stored_raw = row.get(name)
        stored_val = parse_number(stored_raw)
        live_val = parse_number(live_raw)
        if stored_val is None and live_val is None:
            match = True
        elif stored_val is None or live_val is None:
            match = False
        else:
            match = abs(stored_val - live_val) < 0.01
        comparison.append({"metric": name, "stored": stored_raw, "live": live_raw, "match": match})
    return comparison


def strategy_save_name(combo_id: str, row: dict[str, str], prefix: str | None = None) -> str:
    """"{prefix}_{combo_id}_{entry time}" (prefix optional) - e.g.
    "morning_basket_4c50d84cb587_1126" - so a strategy saved on AlgoTest's side is
    traceable straight back to the exact combo it came from, without relying on
    AlgoTest's own (separate, easy to lose track of) naming. The prefix exists so
    strategies belonging to different portfolios stay identifiable once there are
    many saved side by side - without one, every save just reads as "combo_id_time"
    with nothing to group them by. Colons are stripped from the entry time since
    it's going into a single name field alongside the combo_id, not a separate one."""
    entry_time = (row.get("entry_time") or "").replace(":", "")
    base = f"{combo_id}_{entry_time}" if entry_time else combo_id
    prefix = (prefix or "").strip()
    return f"{prefix}_{base}" if prefix else base


def scale_combo_for_save(combo: dict[str, Any], target_lots: float = 1.0) -> dict[str, Any]:
    """Returns a COPY of `combo` resized to `target_lots` per leg, with every
    absolute-rupee risk field scaled by the same ratio so the relative risk profile
    is unchanged at the smaller size - only the leg lots and the *amount*-basis
    overall Stop Loss / Target / Trail SL (its x/y/step/trail_by, all rupee amounts,
    no percentage-basis variant exists for it) are lot-size-dependent; every
    percentage-basis field (leg-level Stop Loss/Target, momentum, overall Stop
    Loss/Target when kind == "percentage") is already relative to premium/notional
    and needs no scaling regardless of lot size.

    For saving a strategy to trade manually at a size decided later (e.g. "save at
    1 lot, multiply up based on the recommendation"), NOT for the sweep itself -
    that always runs at whatever lot size is actually configured, unscaled."""
    combo = copy.deepcopy(combo)
    legs = combo.get("legs") or []
    if not legs:
        return combo
    try:
        original_lots = float(legs[0]["lots"])
    except (TypeError, ValueError, KeyError):
        return combo
    if original_lots <= 0:
        return combo
    scale = target_lots / original_lots

    for leg in legs:
        leg["lots"] = target_lots

    for key in ("stoploss", "target"):
        section = combo.get(key)
        if section is not None and section.get("kind") == "amount":
            section["value"] = round(section["value"] * scale, 2)

    trail_sl = combo.get("trail_sl")
    if trail_sl is not None:
        for f in ("x", "y", "step", "trail_by"):
            trail_sl[f] = round(trail_sl[f] * scale, 2)

    return combo


def _save_strategy(page, selectors: Selectors, name: str) -> None:
    """Confirmed live: "Save Strategy" (next to Start Backtest, only present once a
    backtest has run) opens a "Save New Strategy" dialog with a name input and a
    Done button - a folder can optionally be picked there too, not automated here
    (saves to the default/no folder)."""
    b = selectors.builder
    resolve(page, b.save_strategy_button).click()
    resolve(page, b.save_strategy_name_input).fill(name)
    resolve(page, b.save_strategy_confirm_button).click()


def find_row(csv_paths: list[Path], combo_id: str) -> dict[str, str] | None:
    for path in csv_paths:
        if not path.exists():
            continue
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("combo_id") == combo_id:
                    return row
    return None


def find_row_with_path(csv_paths: list[Path], combo_id: str) -> tuple[Path, dict[str, str]] | None:
    """Same search as find_row, but also returns which file it came from - needed
    to update that exact row in place (store.update_row) rather than just read it."""
    for path in csv_paths:
        if not path.exists():
            continue
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("combo_id") == combo_id:
                    return path, row
    return None


@dataclass
class ComboLauncherState:
    """Opens a real, headed browser on the primary profile, replays one combo end to
    end - fills the builder form, submits, waits for the result, then applies the
    exact same post-result settings the real sweep run applies before scraping
    (brokerage, taxes, slippage, DTE filter) - and compares the freshly-scraped
    metrics against what's stored in the CSV. Mirrors login_helper's thread/status
    pattern. Uses the primary profile, same as login_helper - safe alongside a
    parallel sweep (each worker uses its own copied profile), but will conflict with
    a running *sequential* (parallelism=1) sweep, which already holds that profile.

    Brokerage rate and slippage aren't stored per-row (AlgoTest treats them as
    one-time-per-session settings, not swept dimensions - see runner.py), so the
    best available signal is whatever's in the currently-saved sweep config. DTE
    *is* stored per-row and is applied from there instead."""

    status: str = "idle"  # idle | opening | filling | waiting_for_result | applying_settings | saving_strategy | ready | error
    message: str | None = None
    combo_id: str | None = None
    comparison: list[dict[str, Any]] | None = None
    settings_used: dict[str, Any] | None = None
    saved_strategy_name: str | None = None  # set once "Launch & Save" actually saves it

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "message": self.message,
                "combo_id": self.combo_id,
                "comparison": self.comparison,
                "settings_used": self.settings_used,
                "saved_strategy_name": self.saved_strategy_name,
            }

    def launch(self, csv_paths: list[str], combo_id: str, save: bool = False, prefix: str | None = None) -> None:
        with self._lock:
            if self.status in ("opening", "filling", "waiting_for_result", "applying_settings", "saving_strategy"):
                raise RuntimeError("A launch is already in progress - close that browser window first.")
            self.status = "opening"
            self.message = None
            self.combo_id = combo_id
            self.comparison = None
            self.settings_used = None
            self.saved_strategy_name = None
        self._stop.clear()
        thread = threading.Thread(target=self._run, args=(csv_paths, combo_id, save, prefix), daemon=True)
        self._thread = thread
        thread.start()

    def _run(self, csv_paths: list[str], combo_id: str, save: bool = False, prefix: str | None = None) -> None:
        try:
            row = find_row([Path(p) for p in csv_paths], combo_id)
            if row is None:
                raise ValueError(f"combo_id {combo_id!r} not found in the selected file(s).")
            combo = row_to_combo(row)
            dte_values = _parse_dte_values(row.get("dte"))
            cfg = load_ui_config()
            selectors = load_selectors(SELECTORS_PATH)

            load_dotenv()
            with browser.persistent_context(headless=False) as context:
                page = context.pages[0] if context.pages else context.new_page()
                with self._lock:
                    self.status = "filling"
                # The primary profile's session has been observed to expire within
                # minutes of inactivity (confirmed live) - same per-attempt check the
                # real sweep runner does, so a stale session here doesn't just time
                # out hunting for a tab button that isn't there.
                page.goto(selectors.builder.url)
                ensure_logged_in(page, selectors, os.environ.get("ALGOTEST_EMAIL"), os.environ.get("ALGOTEST_PASSWORD"))
                apply_combination(page, selectors, combo)  # also clicks Start Backtest

                with self._lock:
                    self.status = "waiting_for_result"
                outcome = wait_for_result(page, selectors, timeout_s=180)
                if outcome.status != "ok":
                    raise RuntimeError(f"Backtest did not complete: {outcome.status} - {outcome.error}")

                with self._lock:
                    self.status = "applying_settings"
                    self.settings_used = {
                        "brokerage_rate": cfg.brokerage_rate,
                        "slippage_pct": cfg.slippage_pct,
                        "dte_values": dte_values,
                    }
                # Confirmed live: opened from a background thread, this window doesn't
                # reliably get OS focus, and an unfocused/backgrounded Chromium window
                # throttles its own timers/transitions - the toggle-settle waits
                # ensure_brokerage_rate/apply_result_settings rely on (tuned against a
                # normal, focused headless run) then aren't long enough, and the
                # brokerage-rate button stays disabled past when the code expects it
                # to be enabled. Bringing the window to front avoids the throttling.
                page.bring_to_front()
                ensure_brokerage_rate(page, selectors, cfg.brokerage_rate)
                apply_result_settings(page, selectors, cfg.slippage_pct, dte_values)

                raw_metrics = scrape_metrics(page, selectors)
                comparison = _compare_metrics(row, raw_metrics)

                saved_name = None
                if save:
                    with self._lock:
                        self.status = "saving_strategy"
                    saved_name = strategy_save_name(combo_id, row, prefix)
                    _save_strategy(page, selectors, saved_name)

                with self._lock:
                    self.comparison = comparison
                    self.saved_strategy_name = saved_name
                    self.status = "ready"

                # Keep the window open (and the profile lock held) until the user
                # closes the browser tab themselves, or asks us to close it.
                while not self._stop.is_set():
                    if not context.pages:
                        break
                    page.wait_for_timeout(1000)
        except Exception as exc:  # noqa: BLE001 - surface any crash to the UI
            with self._lock:
                self.status = "error"
                self.message = str(exc)

    def close(self) -> None:
        self._stop.set()


combo_launcher_state = ComboLauncherState()


@dataclass
class BasketSaveState:
    """Saves a whole list of combos (e.g. a Portfolio basket's picks across every
    bucket) as named strategies on AlgoTest, one after another in a SINGLE headed
    browser session - the manual flow is open-fill-wait-save-close per combo,
    repeated by hand for every basket member; this reuses one page/session across
    all of them instead (mirrors src/runner.py's own per-combo loop: navigate to the
    builder once, then just re-apply_combination for each subsequent combo, only
    re-checking login between combos rather than a full re-login every time).

    One combo failing (bad data, a timeout, a stale session) doesn't abort the rest
    - it's recorded as that combo's own error and the loop moves on, same
    tolerance as the sweep runner's own per-combo retry/skip behavior."""

    status: str = "idle"  # idle | opening | running | stopping | done | error
    total: int = 0
    current_index: int = 0  # 0-based index of the combo currently being processed
    prefix: str = ""
    # combo_id -> {"status": "pending"|"saving"|"saved"|"error"|"skipped", "saved_name": str|None, "error": str|None}
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    message: str | None = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "total": self.total,
                "current_index": self.current_index,
                "prefix": self.prefix,
                "results": dict(self.results),
                "message": self.message,
            }

    def is_running(self) -> bool:
        with self._lock:
            return self.status in ("opening", "running", "stopping")

    def start(self, csv_paths: list[str], combo_ids: list[str], prefix: str) -> None:
        with self._lock:
            if self.status in ("opening", "running", "stopping"):
                raise RuntimeError("A basket save is already in progress - stop it first.")
            self.status = "opening"
            self.total = len(combo_ids)
            self.current_index = 0
            self.prefix = prefix
            self.results = {cid: {"status": "pending", "saved_name": None, "error": None} for cid in combo_ids}
            self.message = None
        self._stop.clear()
        thread = threading.Thread(target=self._run, args=(csv_paths, combo_ids, prefix), daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            if self.status in ("opening", "running"):
                self.status = "stopping"
        self._stop.set()

    def close(self) -> None:
        self._stop.set()

    def _run(self, csv_paths: list[str], combo_ids: list[str], prefix: str) -> None:
        try:
            # Pre-validate every combo_id against the selected file(s) BEFORE opening
            # a browser at all - a typo'd or stale combo_id fails immediately as its
            # own error (not different in kind from one that fails mid-batch), and a
            # basket that's entirely invalid never bothers launching a browser window
            # just to discover that on the very first combo.
            paths = [Path(p) for p in csv_paths]
            rows_by_id: dict[str, dict[str, str]] = {}
            for cid in combo_ids:
                row = find_row(paths, cid)
                if row is None:
                    with self._lock:
                        self.results[cid] = {
                            "status": "error", "saved_name": None,
                            "error": f"combo_id {cid!r} not found in the selected file(s).",
                        }
                else:
                    rows_by_id[cid] = row

            if not rows_by_id:
                with self._lock:
                    self.status = "done"
                return

            cfg = load_ui_config()
            selectors = load_selectors(SELECTORS_PATH)
            load_dotenv()
            brokerage_configured = False

            with browser.persistent_context(headless=False) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(selectors.builder.url)
                ensure_logged_in(page, selectors, os.environ.get("ALGOTEST_EMAIL"), os.environ.get("ALGOTEST_PASSWORD"))

                with self._lock:
                    self.status = "running"

                for i, combo_id in enumerate(combo_ids):
                    if combo_id not in rows_by_id:
                        continue  # already recorded as "not found" above
                    if self._stop.is_set():
                        with self._lock:
                            for remaining_id in combo_ids[i:]:
                                if self.results[remaining_id]["status"] == "pending":
                                    self.results[remaining_id]["status"] = "skipped"
                        break

                    with self._lock:
                        self.current_index = i
                        self.results[combo_id]["status"] = "saving"

                    try:
                        row = rows_by_id[combo_id]
                        # Saved at 1 lot, not whatever lot size the sweep itself ran
                        # at - these are being saved to trade manually at a size
                        # decided later (e.g. a Portfolio recommendation), not to
                        # re-verify the sweep's own recorded metrics.
                        combo = scale_combo_for_save(row_to_combo(row), target_lots=1.0)
                        dte_values = _parse_dte_values(row.get("dte"))

                        if not is_logged_in(page, selectors):
                            ensure_logged_in(page, selectors, os.environ.get("ALGOTEST_EMAIL"), os.environ.get("ALGOTEST_PASSWORD"))
                        apply_combination(page, selectors, combo)

                        outcome = wait_for_result(page, selectors, timeout_s=180)
                        if outcome.status != "ok":
                            raise RuntimeError(f"Backtest did not complete: {outcome.status} - {outcome.error}")

                        if not brokerage_configured:
                            page.bring_to_front()
                            ensure_brokerage_rate(page, selectors, cfg.brokerage_rate)
                            brokerage_configured = True
                        apply_result_settings(page, selectors, cfg.slippage_pct, dte_values)

                        saved_name = strategy_save_name(combo_id, row, prefix)
                        _save_strategy(page, selectors, saved_name)

                        with self._lock:
                            self.results[combo_id] = {"status": "saved", "saved_name": saved_name, "error": None}
                    except Exception as exc:  # noqa: BLE001 - one bad combo must not abort the whole basket
                        with self._lock:
                            self.results[combo_id] = {"status": "error", "saved_name": None, "error": str(exc)}
                        continue

                with self._lock:
                    self.status = "done"  # whether the loop finished naturally or was stopped early

                # Keep the window open (and the profile lock held) until the user
                # closes the browser tab themselves, or asks us to close it - same
                # convention as ComboLauncherState, so the last combo's result stays
                # inspectable rather than the window vanishing the instant it's done.
                while not self._stop.is_set():
                    if not context.pages:
                        break
                    page.wait_for_timeout(1000)
        except Exception as exc:  # noqa: BLE001 - surface any crash (e.g. login failure) to the UI
            with self._lock:
                self.status = "error"
                self.message = str(exc)


basket_save_state = BasketSaveState()


