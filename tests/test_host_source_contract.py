from __future__ import annotations

import unittest
from pathlib import Path


MAC_ROOT = Path(__file__).resolve().parents[1]


class HostSourceContractTests(unittest.TestCase):
    def test_app_uses_one_window_instead_of_window_group(self) -> None:
        source = (
            MAC_ROOT / "Sources" / "CSIOpenBaseMac" / "CSIOpenBaseMacApp.swift"
        ).read_text(encoding="utf-8")
        self.assertIn('Window("CSI OpenBase", id: "main")', source)
        self.assertNotIn("WindowGroup", source)

    def test_supervisor_timeout_is_propagated_fail_closed(self) -> None:
        source = (
            MAC_ROOT / "Sources" / "CSIOpenBaseMac" / "BackendController.swift"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "private func terminate(_ child: Process, force: Bool) async throws",
            source,
        )
        self.assertIn("throw BackendError.supervisorTimeout", source)
        self.assertIn("try await stop()", source)
        self.assertFalse(
            any(
                line.strip().startswith("await terminate(child")
                for line in source.splitlines()
            )
        )

    def test_superseded_cookie_install_clears_pending_and_retries(self) -> None:
        source = (
            MAC_ROOT / "Sources" / "CSIOpenBaseMac" / "LocalWebView.swift"
        ).read_text(encoding="utf-8")
        branch = source.split("case .superseded:", maxsplit=1)[1].split(
            "case .invalidCookie:", maxsplit=1
        )[0]
        self.assertIn("self.pendingNonce = nil", branch)
        self.assertIn("self.load(session, in: webView)", branch)


if __name__ == "__main__":
    unittest.main()
