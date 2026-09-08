# macOS Project Guidance

Canonical remote: `git@github.com:CSI-OpenBase/mac.git`

This repository owns the SwiftUI/WKWebView host, backend supervisor, macOS
packaging, signing inputs, and distribution. Backend behavior comes from the
`csi-openbase` Python package at
`git@github.com:CSI-OpenBase/local-web.git`.

- Do not duplicate Python collection or archive logic in Swift.
- Keep this repository independently cloneable; accept the separately cloned
  Python backend through documented build inputs instead of assuming an aggregate
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
bash -n build_macos.sh
```

Final Swift compilation, signed headed-Chromium execution, process-group cleanup,
notarization, and Gatekeeper assessment must run on supported macOS hardware.
