# Embedded Python Web Guidance

Canonical repository: `git@github.com:CSI-OpenBase/mac.git`

This repository is the source of truth for creator authorization, automatic data
table export, local video archives, user-triggered anonymized comment export,
persistence, backend APIs, and the local web UI used by the macOS host.

- Keep runtime data outside installed package directories.
- Keep this directory installable and testable as a Python project, but do not
  fetch code from another repository during normal development or release builds.
- Preserve the authenticated desktop contract for `/health` and `/api/shutdown`.
- Treat exported creator data and browser sessions as sensitive local data.
- Do not introduce CSI Core scoring models, proprietary weights, benchmarks, or
  commercial report logic.
- Keep migrations and `LICENSE`/`NOTICE` resources present in built wheels.
- Treat `python-web/VERSION` as the Python package version source. Versions use
  `x.x.xx`, start at `0.0.10`, and roll `1.1.99` to `1.2.10`; run
  `python scripts/bump_version.py --apply` from this directory to advance it.
- Do not commit `var/` data, `workspace-data/`, browser profiles, exports, or
  generated build artifacts.

## Local Data Contracts

- `admin_app/local_app.py` and `admin_app/templates/local_home.html` own the
  local operator workflow; `admin_app/local_store.py` owns its SQLite index,
  and `admin_app/local_cleanup.py` is the only entry point for destructive
  local-data maintenance.
- Treat `exports/` and `works/` as application-managed trees. Preserve the
  selected work directory itself, unrelated root-level files and directories,
  `logs/`, the SQLite file and schema, and the browser authorization profile
  unless a future user-facing contract explicitly says otherwise.
- Keep homepage-visible comments and exported comments as separate measures.
  `visible_comment_count` records the platform value observed during video
  synchronization; `comment_count` and `last_comment_export_at` describe the
  user's latest manual comment export. Do not substitute or reset the visible
  count when comments are exported or cleared.
- `docs/data-model.md` is the detailed source for local archive and cleanup
  semantics. Update it together with any change to these data boundaries.

## Destructive Maintenance

- Keep cleanup scopes allowlisted: `exports` clears platform downloads and
  their export state; `comments` clears per-video comment files and export
  state while preserving video archives and visible counts; `all` clears
  collected archives and SQLite account/video/job rows while preserving the
  workspace shell, logs, database structure, and browser authorization.
- Never recursively delete a caller-supplied path. Resolve cleanup targets from
  `LocalSettings`, reject symlinks, Windows junctions, filesystem mount points,
  path escapes, and any overlap with the browser authorization directory.
- Reject cleanup while a local job is queued or running. Keep filesystem
  staging, the durable cleanup manifest, and the SQLite operation marker as one
  crash-recovery protocol; the marker must be committed in the same SQLite
  transaction as index deletion.
- Run interrupted-cleanup recovery only after acquiring the workspace lease and
  before starting the local job runner. An uncommitted operation restores its
  staged directories; a committed operation finishes physical deletion.
- Test destructive behavior only with temporary workspaces. Cover every scope,
  preservation boundary, active-job rejection, database/staging failures,
  restart recovery, and unsafe filesystem boundaries when changing cleanup.

Verify changes with:

```bash
python -m pytest
python -m compileall -q admin_app scripts
python -m pip wheel . --no-deps --wheel-dir dist
```
