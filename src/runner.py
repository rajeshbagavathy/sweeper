from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from src.auth import LoginNotConfigured, ensure_logged_in, is_logged_in
from src.config import Selectors
from src.form import apply_combination
from src.results import parse_number, scrape_metrics, wait_for_result
from src.store import append_row, combo_id, flatten

SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"


def run_sweep(
    page: Page,
    combos: list[dict[str, Any]],
    selectors: Selectors,
    csv_path: Path,
    log_path: Path,
    fieldnames: list[str],
    existing_statuses: dict[str, str],
    *,
    delay_s: float = 2.0,
    result_timeout_s: int = 180,
    max_retries: int = 2,
    email: str | None = None,
    password: str | None = None,
) -> dict[str, int]:
    console = Console()
    stats = {"ok": 0, "error": 0, "skipped": 0}

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"Sweeping (ok=0 error=0 skipped=0)", total=len(combos)
        )

        for combo in combos:
            cid = combo_id(combo)

            # Step 2: skip combos that already succeeded. Ones that previously errored
            # are retried on a plain re-run - max_retries governs retries *within* one
            # attempt, this is what lets a whole sweep eventually succeed across runs.
            if existing_statuses.get(cid) == "ok":
                stats["skipped"] += 1
                progress.advance(task)
                progress.update(task, description=_status_line(stats))
                continue

            _run_one_combo(
                page,
                combo,
                cid,
                selectors,
                csv_path,
                log_path,
                fieldnames,
                stats,
                result_timeout_s=result_timeout_s,
                max_retries=max_retries,
                email=email,
                password=password,
            )

            progress.advance(task)
            progress.update(task, description=_status_line(stats))
            time.sleep(delay_s)

    return stats


def _status_line(stats: dict[str, int]) -> str:
    return f"Sweeping (ok={stats['ok']} error={stats['error']} skipped={stats['skipped']})"


def _run_one_combo(
    page: Page,
    combo: dict[str, Any],
    cid: str,
    selectors: Selectors,
    csv_path: Path,
    log_path: Path,
    fieldnames: list[str],
    stats: dict[str, int],
    *,
    result_timeout_s: int,
    max_retries: int,
    email: str | None,
    password: str | None,
) -> None:
    attempt = 0
    backoff_s = 1.0

    while True:
        attempt += 1
        try:
            # Step 3: ensure session alive, re-login if not.
            if not is_logged_in(page, selectors):
                ensure_logged_in(page, selectors, email, password)

            # Steps 4-6: fresh state, apply params, click run.
            apply_combination(page, selectors, combo)

            # Step 7: wait properly, racing error_marker.
            outcome = wait_for_result(page, selectors, timeout_s=result_timeout_s)
            if outcome.status != "ok":
                raise RuntimeError(f"{outcome.status}: {outcome.error}")

            # Step 8: scrape + parse.
            raw_metrics = scrape_metrics(page, selectors)
            row = _build_row(combo, cid, "ok", None, raw_metrics)

            # Step 9: append immediately.
            append_row(csv_path, fieldnames, row)
            _log(log_path, cid, "ok", None, attempt)
            stats["ok"] += 1
            return

        except LoginNotConfigured:
            # Retrying per-combo can't fix a missing login config - stop the whole sweep.
            raise

        except Exception as exc:  # noqa: BLE001 - one bad combo must not crash the sweep
            if attempt <= max_retries:
                time.sleep(backoff_s)
                backoff_s *= 2
                continue

            # Step 10: give up on this combo - save artifacts, write an error row, move on.
            _save_failure_artifacts(page, cid)
            row = _build_row(combo, cid, "error", str(exc), {})
            append_row(csv_path, fieldnames, row)
            _log(log_path, cid, "error", str(exc), attempt)
            stats["error"] += 1
            return


def _build_row(
    combo: dict[str, Any], cid: str, status: str, error: str | None, raw_metrics: dict[str, str | None]
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "combo_id": cid,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "error": error or "",
    }
    row.update(flatten(combo))
    for name, raw in raw_metrics.items():
        row[name] = parse_number(raw)
    row["raw_metrics_json"] = json.dumps(raw_metrics)
    return row


def _save_failure_artifacts(page: Page, cid: str) -> None:
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(SCREENSHOTS_DIR / f"{cid}.png"), full_page=True)
        (SCREENSHOTS_DIR / f"{cid}.html").write_text(page.content())
    except Exception:
        pass  # best-effort - artifact saving must never mask the original failure


def _log(log_path: Path, cid: str, status: str, error: str | None, attempt: int) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "combo_id": cid,
            "status": status,
            "error": error,
            "attempt": attempt,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    )
    with log_path.open("a") as f:
        f.write(line + "\n")
