# MTQuant integration - where to start

MTQuant is a **Windows desktop application**, not a website - the AlgoTest
integration pattern in this codebase (Playwright driving a browser) does not
apply here at all. Do not model this on `combo_launcher.py`/`src/auth.py`/
`config/selectors.yaml` - those are web-automation specific.

The automation approach itself (which Windows desktop-automation technique to
use - UI Automation API, `pywinauto`, COM, an MTQuant-provided scripting/API
surface if one exists, etc.) is already decided - that analysis is done. This
doc is only the "where to start" for landing it in *this* repo without
disturbing the existing macOS/AlgoTest setup.

## The one hard constraint: total isolation from the existing setup

**No changes to anything the current macOS/AlgoTest workflow depends on.**
Whichever machine this project is opened on - MacBook or Windows - it must run
correctly using only the parts relevant to that machine, with nothing
Windows-only ever loaded, imported, or required on macOS (and vice versa,
though today there's nothing macOS-specific to protect the other way).
Concretely:

- **New, separate module(s) for MTQuant** - e.g. `src/mtquant/` as its own
  package, not additions scattered into `src/web/app.py` or the existing
  `src/` files. Nothing in the current AlgoTest/sweep code should need to
  know MTQuant integration exists.
- **New, separate optional dependency group** in `pyproject.toml` (e.g. an
  `[project.optional-dependencies]` group named `windows` or `mtquant`) for
  whatever library the chosen automation approach needs. A plain `uv sync` on
  macOS must keep working without ever attempting to install a Windows-only
  package - it should simply not be in the default dependency set at all.
- **Gate by `platform.system()` at the actual entry point** (the FastAPI
  route or CLI command that triggers an MTQuant action), not by scattering
  `if` checks through shared code. Something like: the route imports the
  `src.mtquant` module lazily, inside the handler, and returns a clear "not
  available on this OS" response if `platform.system() != "Windows"` before
  ever touching it. That import boundary is what actually protects macOS -
  if `src.mtquant` is never imported there, whatever it needs installed
  never matters there either.
- **The web UI can offer or hide the MTQuant controls based on the same
  check** - a small `GET /api/platform` (or similar) the frontend calls once
  on load, so a MacBook session simply never shows a "Save to MTQuant"
  button rather than showing one that then errors.

## Suggested first steps, in order

1. Create `src/mtquant/` (empty package is fine to start) and add its
   dependency group to `pyproject.toml`, unpopulated except for whatever the
   chosen automation library needs - prove `uv sync` on macOS is completely
   unaffected before writing any real logic.
2. Add the `platform.system()` gate at whichever single entry point will
   eventually trigger MTQuant actions (even a stub endpoint that just returns
   "not implemented yet" on Windows and "not available" elsewhere) - get the
   isolation boundary in place and verified before building behind it.
3. Build the actual MTQuant automation inside `src/mtquant/`, using whatever
   approach the existing analysis already settled on. This part has no
   precedent in this codebase to follow (it's not the web-automation pattern
   used elsewhere) - reference the analysis already done for this rather than
   anything in this repo's own `src/` or `src/web/`.
4. Wire it up to the web UI the same shape as everything else already
   running long jobs here (`RunState`/`CorrelateState`/`PortfolioSweepState`/
   `BasketSaveState` - background thread, `start()`/`stop()`/`snapshot()`,
   polled from the frontend) only if that shape actually fits a desktop-app
   automation flow - it might not, given MTQuant isn't a browser page you can
   poll a `Page` object against the same way.
