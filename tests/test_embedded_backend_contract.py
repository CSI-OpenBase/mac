from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


MAC_ROOT = Path(__file__).resolve().parents[1]
PYTHON_WEB_ROOT = MAC_ROOT / "python-web"


class EmbeddedBackendContractTests(unittest.TestCase):
    def test_default_build_uses_the_committed_python_web_project(self) -> None:
        source = (MAC_ROOT / "build_macos.sh").read_text(encoding="utf-8")

        self.assertIn('PYTHON_PROJECT_SOURCE="$PROJECT_DIR/python-web"', source)
        self.assertIn(
            'if [[ -z "$INPUT_MODE" ]]; then\n    INPUT_MODE=python-project\nfi',
            source,
        )
        self.assertNotIn('$PROJECT_DIR/../python', source)
        self.assertNotIn("CSI_OPENBASE_BACKEND_BINARY", source)
        self.assertNotIn("git clone", source)
        self.assertNotIn("git fetch", source)

    def test_python_web_is_regular_committed_source(self) -> None:
        self.assertTrue(PYTHON_WEB_ROOT.is_dir())
        self.assertFalse(PYTHON_WEB_ROOT.is_symlink())
        self.assertEqual(list(PYTHON_WEB_ROOT.rglob(".git")), [])

        gitmodules = MAC_ROOT / ".gitmodules"
        if gitmodules.exists():
            self.assertNotIn(
                "python-web",
                gitmodules.read_text(encoding="utf-8"),
            )

    def test_release_backend_and_launcher_overrides_are_debug_only(self) -> None:
        source = (
            MAC_ROOT / "Sources" / "CSIOpenBaseMac" / "BackendController.swift"
        ).read_text(encoding="utf-8")

        for variable in ("CSI_OPENBASE_BACKEND", "CSI_OPENBASE_LAUNCHER"):
            with self.subTest(variable=variable):
                override = source.index(f'environment["{variable}"]')
                prefix = source[:override]
                self.assertGreater(prefix.rfind("#if DEBUG"), prefix.rfind("#endif"))
                self.assertNotEqual(source.find("#endif", override), -1)

    def test_browser_bundle_is_copied_after_pyinstaller_freezes_backend(self) -> None:
        source = (MAC_ROOT / "build_macos.sh").read_text(encoding="utf-8")

        freeze = source.index('"$VENV_PYTHON" -m PyInstaller')
        browser_destination = source.index(
            'FROZEN_BROWSER_ROOT="$BACKEND_SOURCE/_internal/ms-playwright"'
        )
        browser_copy = source.index("ditto --norsrc --noextattr --noqtn --noacl")
        browser_validation = source.index(
            'validate_playwright_runtime "$frozen_browser_dir"'
        )

        self.assertIn("--contents-directory _internal", source)
        self.assertNotIn('PYINSTALLER_ARGS+=(--add-data "$browser_dir', source)
        self.assertLess(freeze, browser_destination)
        self.assertLess(browser_destination, browser_copy)
        self.assertLess(browser_copy, browser_validation)

    def test_embedded_project_preserves_the_backend_entry_contract(self) -> None:
        configuration = tomllib.loads(
            (PYTHON_WEB_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(configuration["project"]["name"], "csi-openbase")
        self.assertEqual(
            configuration["project"]["scripts"]["csi-openbase"],
            "scripts.run_openbase:main",
        )
        self.assertTrue((PYTHON_WEB_ROOT / "scripts" / "run_openbase.py").is_file())
        self.assertTrue((PYTHON_WEB_ROOT / "admin_app" / "local_app.py").is_file())

    def test_embedded_project_carries_required_attribution(self) -> None:
        for relative in (
            "LICENSE",
            "NOTICE",
            "THIRD-PARTY-NOTICES.md",
            "licenses/htmx-0BSD.txt",
            "licenses/lucide-ISC.txt",
            "UPSTREAM.md",
        ):
            with self.subTest(relative=relative):
                self.assertTrue((PYTHON_WEB_ROOT / relative).is_file())


if __name__ == "__main__":
    unittest.main()
