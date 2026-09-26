"""MTQuant desktop-app integration (Windows-only).

This package is isolated from the rest of the codebase on purpose - see
docs/mtquant-integration.md for the isolation rules. Nothing outside this
package should import from here, and this package itself must only ever be
imported lazily, inside a `platform.system() == "Windows"` gate at the
entry point that needs it (never at module load time of a shared file like
src/web/app.py).
"""
