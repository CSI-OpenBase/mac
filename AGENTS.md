# macOS Project Guidance

Canonical remote: `git@github.com:CSI-OpenBase/mac.git`

This repository owns the SwiftUI/WKWebView host, a controlled Python backend
snapshot in `python-web/`, the backend supervisor, macOS packaging, signing
inputs, and distribution. The canonical Python upstream is
`git@github.com:CSI-OpenBase/local-web.git`; `python-web/UPSTREAM.md` records the
snapshot's imported commit.

- Keep creator authorization, collection, archive, comment, persistence, and web
  UI behavior in `python-web/`; do not duplicate it in Swift.
- Keep the repository self-contained. `python-web/` must be committed source, not
  a submodule, and normal development or release builds must not clone another
  repository.
- Synchronize shared backend changes deliberately from `local-web`, update
  `python-web/UPSTREAM.md`, and verify both repositories.
- Keep Release builds restricted to the bundled backend and supervisor.
- Preserve authenticated loopback sessions, single-window cookie ownership, and
  fail-closed backend process handling.
- Preserve safe output-path checks, complete license collection, inside-out code
  signing, and Chromium entitlements.
- Do not commit `.build/`, `dist/`, browser runtimes, user data, or Python caches.

Verify portable helper changes with:

```bash
python3 -m unittest discover -s tests -v
python3 -m pytest python-web/tests
bash -n build_macos.sh
```

Final Swift compilation, signed headed-Chromium execution, process-group cleanup,
notarization, and Gatekeeper assessment must run on supported macOS hardware.
