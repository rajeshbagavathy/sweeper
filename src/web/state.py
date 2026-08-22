from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src import browser, store
from src.auth import LoginNotConfigured, is_logged_in
from src.config import load_selectors
from src.runner import run_sweep
from src.web.expand import expand_ui_config
from src.web.models import SweepUIConfig

ROOT = Path(__file__).resolve().parent.parent.parent
SELECTORS_PATH = ROOT / "config" / "selectors.yaml"
OUTPUT_DIR = ROOT / "output"
LOG_PATH = OUTPUT_DIR / "run.log"


@dataclass
class RunState:
    status: str = "idle"  # idle | running | done | stopped | error
    current: int = 0
    total: int = 0
    ok: int = 0
    error: int = 0
    skipped: int = 0
    csv_path: str | None = None
    message: str | None = None
    started_at: str | None = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _stop_event: threading.Event | None = field(default=None, repr=False, compare=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "current": self.current,
                "total": self.total,
                "ok": self.ok,
                "error": self.error,
                "skipped": self.skipped,
                "csv_path": self.csv_path,
                "message": self.message,
                "started_at": self.started_at,
            }

    def is_running(self) -> bool:
        with self._lock:
            return self.status == "running"

    def start(self, cfg: SweepUIConfig) -> None:
        if self.is_running():
            raise RuntimeError("A sweep is already running - stop it first.")

        combos = expand_ui_config(cfg)
        OUTPUT_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = OUTPUT_DIR / f"results_web_{timestamp}.csv"
        metric_names = list(load_selectors(SELECTORS_PATH).results.metrics.keys())
        fieldnames = store.build_fieldnames(combos, metric_names)

        with self._lock:
            self.status = "running"
            self.current = 0
            self.total = len(combos)
            self.ok = 0
            self.error = 0
            self.skipped = 0
            self.csv_path = str(csv_path)
            self.message = None
            self.started_at = datetime.now().isoformat()
            self._stop_event = threading.Event()

        stop_event = self._stop_event
        thread = threading.Thread(
            target=self._run, args=(cfg, combos, csv_path, fieldnames, stop_event), daemon=True
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            if self._stop_event is not None:
                self._stop_event.set()

    def _on_progress(self, update: dict[str, Any]) -> None:
        with self._lock:
            self.current = update["current"]
            self.ok = update["ok"]
            self.error = update["error"]
            self.skipped = update["skipped"]

    def _run(
        self,
        cfg: SweepUIConfig,
        combos: list[dict[str, Any]],
        csv_path: Path,
        fieldnames: list[str],
        stop_event: threading.Event,
    ) -> None:
        load_dotenv()
        try:
            selectors = load_selectors(SELECTORS_PATH)
            with browser.persistent_context(headless=cfg.headless) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(selectors.builder.url)

                try:
                    is_logged_in(page, selectors)
                except LoginNotConfigured as exc:
                    with self._lock:
                        self.status = "error"
                        self.message = str(exc)
                    return

                run_sweep(
                    page,
                    combos,
                    selectors,
                    csv_path,
                    LOG_PATH,
                    fieldnames,
                    existing_statuses={},
                    delay_s=cfg.delay,
                    result_timeout_s=cfg.result_timeout,
                    max_retries=cfg.max_retries,
                    email=os.environ.get("ALGOTEST_EMAIL"),
                    password=os.environ.get("ALGOTEST_PASSWORD"),
                    on_progress=self._on_progress,
                    stop_event=stop_event,
                )

            with self._lock:
                self.status = "stopped" if stop_event.is_set() else "done"

        except Exception as exc:  # noqa: BLE001 - surface any crash to the UI instead of dying silently
            with self._lock:
                self.status = "error"
                self.message = str(exc)


run_state = RunState()
