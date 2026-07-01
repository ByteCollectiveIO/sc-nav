"""Single source of truth for the application version (SemVer: MAJOR.MINOR.PATCH).

Surfaced at GET /api/health and stamped into the site footer so a deployed
instance can report exactly what code it's running. Each release tags the
matching commit `vX.Y.Z`; bump this string in the release commit.

Bump rules for this app:
  * MAJOR — a breaking change to the watcher<->server contract.
  * MINOR — a new user-facing feature, watcher still compatible.
  * PATCH — a bug fix / internal change, no new surface.
"""

__version__ = "0.20.0"
