# macOS Project Guidance

Canonical remote: `git@github.com:cdsi-project/Beacon.git`

This repository owns the SwiftUI/WKWebView host, the Python web backend in
`python-web/`, the backend supervisor, macOS packaging, signing inputs, and
distribution.

- Keep creator authorization, collection, archive, comment, persistence, and web
  UI behavior in `python-web/`; do not duplicate it in Swift.
- Keep the repository self-contained. `python-web/` must be committed source, not
  a submodule, and normal development or release builds must not clone another
  repository.
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
