from __future__ import annotations

import time

import pytest

from src.web.login_helper import LoginHelperState


def test_snapshot_starts_idle():
    state = LoginHelperState()
    assert state.snapshot() == {"status": "idle", "message": None, "account": 1}


def test_start_raises_if_already_opening_or_waiting(monkeypatch):
    state = LoginHelperState()
    with state._lock:
        state.status = "waiting"
    with pytest.raises(RuntimeError):
        state.start()


def test_start_is_allowed_again_after_a_previous_run_finished(monkeypatch):
    state = LoginHelperState()

    def fake_run(self, relogin=False, account=1):
        with self._lock:
            self.status = "logged_in"

    monkeypatch.setattr(LoginHelperState, "_run", fake_run)
    state.start()
    for _ in range(50):
        if state.snapshot()["status"] != "opening":
            break
        time.sleep(0.01)
    assert state.snapshot()["status"] == "logged_in"

    # a second start() shouldn't raise now that the first run has finished
    state.start()


def test_dismiss_clears_terminal_states_but_not_in_progress():
    state = LoginHelperState()

    with state._lock:
        state.status = "waiting"
    state.dismiss()
    assert state.snapshot()["status"] == "waiting"  # untouched - still in progress

    for terminal in ("timeout", "error", "logged_in"):
        with state._lock:
            state.status = terminal
            state.message = "something"
        state.dismiss()
        snap = state.snapshot()
        assert snap["status"] == "idle"
        assert snap["message"] is None


def test_start_raises_if_already_logging_out():
    state = LoginHelperState()
    with state._lock:
        state.status = "logging_out"
    with pytest.raises(RuntimeError):
        state.start(relogin=True)


def test_start_passes_relogin_flag_through_to_run(monkeypatch):
    state = LoginHelperState()
    received = {}

    def fake_run(self, relogin=False, account=1):
        received["relogin"] = relogin
        received["account"] = account
        with self._lock:
            self.status = "logged_in"

    monkeypatch.setattr(LoginHelperState, "_run", fake_run)
    state.start(relogin=True)
    for _ in range(50):
        if state.snapshot()["status"] != "opening":
            break
        time.sleep(0.01)
    assert received["relogin"] is True
    assert received["account"] == 1


def test_start_passes_account_through_to_run(monkeypatch):
    state = LoginHelperState()
    received = {}

    def fake_run(self, relogin=False, account=1):
        received["account"] = account
        with self._lock:
            self.status = "logged_in"

    monkeypatch.setattr(LoginHelperState, "_run", fake_run)
    state.start(account=2)
    for _ in range(50):
        if state.snapshot()["status"] != "opening":
            break
        time.sleep(0.01)
    assert received["account"] == 2
    assert state.snapshot()["account"] == 2


def test_start_rejects_unknown_account():
    state = LoginHelperState()
    with pytest.raises(ValueError):
        state.start(account=4)


def test_dismiss_does_not_touch_logging_out():
    state = LoginHelperState()
    with state._lock:
        state.status = "logging_out"
    state.dismiss()
    assert state.snapshot()["status"] == "logging_out"  # in progress, same as "waiting"


def test_refresh_worker_profiles_removes_only_the_given_accounts_directory(tmp_path, monkeypatch):
    import src.runner as runner_mod
    import src.web.login_helper as login_helper_mod

    fake_dir = tmp_path / ".browser-profiles"
    (fake_dir / "browser-profile" / "worker-0").mkdir(parents=True)
    (fake_dir / "browser-profile" / "worker-0" / "cookie.txt").write_text("stale")
    (fake_dir / "browser-profile-2" / "worker-0").mkdir(parents=True)
    (fake_dir / "browser-profile-2" / "worker-0" / "cookie.txt").write_text("still healthy")

    monkeypatch.setattr(runner_mod, "WORKER_PROFILES_DIR", fake_dir)

    login_helper_mod._refresh_worker_profiles(tmp_path / ".browser-profile")
    assert not (fake_dir / "browser-profile").exists()
    assert (fake_dir / "browser-profile-2").exists()  # untouched - different account
