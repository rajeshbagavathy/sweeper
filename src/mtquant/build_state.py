"""Runs portfolio creation against the live mtQuant app on a background thread.

Importing this module does not import pywinauto. The Windows-only automation
module is imported inside the worker, after the route has already rejected
non-Windows callers.
"""

from __future__ import annotations

import re
import threading

_TAG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class MTQuantBuildState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.status = "idle"
        self.message = ""
        self.current = 0
        self.total = 0
        self.results: list[dict] = []

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "message": self.message,
                "current": self.current,
                "total": self.total,
                "results": list(self.results),
            }

    def start(self, plans: list, strategy_tag: str) -> None:
        tag = strategy_tag.strip()
        if not _TAG_RE.fullmatch(tag):
            raise ValueError("Strategy tag must be letters, numbers, '_' or '-' only (example: NIFTY_1DTE).")
        if not plans:
            raise ValueError("No strategies selected.")
        with self._lock:
            if self.status == "running":
                raise RuntimeError("A mtQuant build is already running.")
            self.status = "running"
            self.message = "Connecting to mtQuant..."
            self.current = 0
            self.total = len(plans)
            self.results = []
        thread = threading.Thread(target=self._run, args=(plans, tag), name="mtquant-build", daemon=True)
        self._thread = thread
        thread.start()

    def _run(self, plans: list, strategy_tag: str) -> None:
        from src.mtquant.automation import MTQuantSession, build_portfolio

        try:
            session = MTQuantSession.connect()
            tag_options = session.ensure_strategy_tag(strategy_tag)
            session.select_main_tab("Multi-Leg")
        except Exception as exc:
            self._finish_error(f"Couldn't prepare mtQuant: {exc}")
            return

        for index, plan in enumerate(plans, start=1):
            with self._lock:
                self.current = index
                self.message = f"Building {plan.portfolio_name}"
            try:
                build_portfolio(session, plan, strategy_tag=strategy_tag, tag_options=tag_options, save=True)
            except Exception as exc:
                with self._lock:
                    self.results.append(
                        {"strategy_id": plan.source_strategy_id, "name": plan.portfolio_name, "ok": False, "error": str(exc)}
                    )
                try:
                    session._close_stale_portfolio_dialogs()
                except Exception:
                    pass
                self._finish_error(f"Stopped on {plan.portfolio_name}: {exc}")
                return
            with self._lock:
                self.results.append({"strategy_id": plan.source_strategy_id, "name": plan.portfolio_name, "ok": True, "error": ""})

        with self._lock:
            self.status = "done"
            self.message = f"Saved {len(plans)} portfolio(s) in mtQuant under tag {strategy_tag}."

    def _finish_error(self, message: str) -> None:
        with self._lock:
            self.status = "error"
            self.message = message


mtquant_build_state = MTQuantBuildState()
