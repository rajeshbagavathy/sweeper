from __future__ import annotations

import csv
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from src.sweep import TooManyCombinations
from src.web.expand import expand_ui_config, load_ui_config, save_ui_config
from src.web.models import SweepUIConfig
from src.web.narrow import narrow_config, rmdd_sort_key
from src.web.state import run_state

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="AlgoTest Sweeper")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def get_config() -> SweepUIConfig:
    return load_ui_config()


@app.post("/api/config")
def post_config(cfg: SweepUIConfig) -> dict:
    save_ui_config(cfg)
    return {"ok": True}


@app.post("/api/dry-run")
def dry_run(cfg: SweepUIConfig) -> dict:
    try:
        combos = expand_ui_config(cfg)
    except TooManyCombinations as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"count": len(combos), "sample": combos[:10]}


@app.post("/api/run")
def start_run(cfg: SweepUIConfig) -> dict:
    save_ui_config(cfg)
    try:
        run_state.start(cfg)
    except TooManyCombinations as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/stop")
def stop_run() -> dict:
    run_state.stop()
    return {"ok": True}


@app.get("/api/status")
def get_status() -> dict:
    return run_state.snapshot()


@app.get("/api/results")
def get_results(limit: int = 50) -> dict:
    snapshot = run_state.snapshot()
    csv_path = snapshot.get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        return {"rows": [], "columns": []}

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        rows = list(reader)

    rows.sort(key=rmdd_sort_key, reverse=True)
    return {"rows": rows[:limit], "columns": columns}


@app.post("/api/narrow")
def narrow(top_n: int = 10) -> SweepUIConfig:
    """Read the active/most-recent results CSV, and narrow every range in the saved UI
    config to center on whatever won in the top `top_n` rows by Return/MaxDD - one
    round of coarse-grid-then-refine instead of hand-editing every range."""
    snapshot = run_state.snapshot()
    csv_path = snapshot.get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        raise HTTPException(status_code=400, detail="No results yet to narrow from - run the sweep first.")

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    cfg = load_ui_config()
    try:
        narrowed = narrow_config(cfg, rows, top_n=top_n)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    save_ui_config(narrowed)
    return narrowed
