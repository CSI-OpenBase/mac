# Source provenance

The current `python-web/` snapshot synchronized the comment-capture resilience
changes from the Apache-2.0 licensed
[`CSI-OpenBase/local-web`](https://github.com/CSI-OpenBase/local-web) repository
at commit `4bb9fec77be2a4d9ef683562706dd30b7800c97b` on 2026-09-29. The
Python-only CSI work-link bridge from upstream remains intentionally excluded
from this macOS snapshot.

On 2026-11-06, the Pop / Creator's Desk visual refresh was selectively synced
from `local-web` commit `d5b6fc4b821eafd876541d2a3919a05dd31dc511`.
Only `admin_app/static/local.css` and the homepage's studio label / comment
icons were imported; the backend behavior, version, and CSI work-link bridge
exclusion are unchanged.

The source is committed directly in this repository. It is not a Git submodule,
and builds do not contact the upstream repository. The original `LICENSE`,
`NOTICE`, third-party notices, and dependency license texts are retained here.
