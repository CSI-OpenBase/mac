from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import write_dependency_licenses as licenses  # noqa: E402


class _Metadata(dict[str, str]):
    def get_all(self, _name: str, default: list[str]) -> list[str]:
        return default


class _Distribution:
    def __init__(
        self,
        files: dict[str, Path],
        *,
        name: str = "demo",
        version: str = "1.0",
        requires: list[str] | None = None,
    ) -> None:
        self.files = list(files)
        self._files = files
        self.metadata = _Metadata(Name=name)
        self.requires = requires or []
        self.version = version

    def locate_file(self, packaged_file: object) -> Path:
        return self._files[str(packaged_file)]


class DependencyLicenseTests(unittest.TestCase):
    def test_include_package_adds_its_dependency_closure_once(self) -> None:
        root = _Distribution({}, name="csi-openbase", requires=["shared-runtime>=1"])
        pyinstaller = _Distribution(
            {},
            name="PyInstaller",
            version="6.22",
            requires=["shared_runtime>=1"],
        )
        shared = _Distribution({}, name="shared-runtime", version="1.5")
        distributions = {
            "csi-openbase": root,
            "pyinstaller": pyinstaller,
            "shared-runtime": shared,
        }

        def find_distribution(name: str) -> _Distribution:
            return distributions[licenses._normalized_name(name)]

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "licenses.txt"
            with mock.patch.object(
                licenses.importlib.metadata,
                "distribution",
                side_effect=find_distribution,
            ):
                licenses.write_report(
                    output,
                    include_packages=["PyInstaller", "pyinstaller"],
                )
            report = output.read_text(encoding="utf-8")

        self.assertEqual(report.count("\nPyInstaller 6.22\n"), 1)
        self.assertEqual(report.count("\nshared-runtime 1.5\n"), 1)
        self.assertNotIn("csi-openbase 1.0", report)

    def test_license_texts_reject_unsafe_paths_and_binary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            valid = root / "LICENSE.txt"
            valid.write_text("Example license text\n", encoding="utf-8")
            bytecode = root / "license.pyc"
            bytecode.write_bytes(b"\x42\x0d\x0d\x0a\x00binary")
            outside = root / "outside-license"
            outside.write_text("must not be exposed", encoding="utf-8")

            distribution = _Distribution(
                {
                    "demo.dist-info/licenses/LICENSE.txt": valid,
                    "demo.dist-info/licenses/__pycache__/license.pyc": bytecode,
                    "../../outside/LICENSE": outside,
                    "C:/outside/NOTICE": outside,
                    "/private/outside/NOTICE": outside,
                }
            )

            found = licenses._license_texts(distribution)

        self.assertEqual(
            found,
            [("demo.dist-info/licenses/LICENSE.txt", "Example license text")],
        )
        self.assertNotIn(str(root), repr(found))

    def test_report_uses_relative_metadata_paths_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "LICENSE.txt"
            source.write_text("Example dependency license\n", encoding="utf-8")
            project = _Distribution({}, name="csi-openbase")
            dependency = _Distribution(
                {"demo.dist-info/licenses/LICENSE.txt": source},
                name="demo",
            )

            def find_distribution(name: str) -> _Distribution:
                return project if name == "csi-openbase" else dependency

            output = root / "report.txt"
            with mock.patch.object(
                licenses.importlib.metadata,
                "distribution",
                side_effect=find_distribution,
            ):
                licenses.write_report(output, include_packages=["demo"])
            report = output.read_text(encoding="utf-8")

        self.assertIn("demo.dist-info/licenses/LICENSE.txt", report)
        self.assertNotIn(str(root), report)
        self.assertNotIn("\x00", report)

    def test_copy_project_notices_requires_text_from_dist_info(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            package_files: dict[str, Path] = {}
            expected = {
                "LICENSE": b"license bytes\n",
                "NOTICE": b"notice bytes\n",
                "THIRD-PARTY-NOTICES.md": b"third-party bytes\n",
            }
            for name, payload in expected.items():
                source = root / f"source-{name}"
                source.write_bytes(payload)
                package_files[f"csi_openbase-1.0.dist-info/licenses/{name}"] = source
            distribution = _Distribution(package_files)
            output = root / "backend-notices"

            with mock.patch.object(
                licenses.importlib.metadata,
                "distribution",
                return_value=distribution,
            ):
                licenses.copy_project_notices(output)

            self.assertEqual(
                {path.name: path.read_bytes() for path in output.iterdir()},
                expected,
            )

    def test_copy_project_notices_preserves_full_license_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            package_files: dict[str, Path] = {}
            expected = {
                "LICENSE": "project license\n",
                "NOTICE": "project notice\n",
                "THIRD-PARTY-NOTICES.md": "third-party index\n",
                "licenses/htmx-0BSD.txt": "htmx license\n",
                "licenses/lucide-ISC.txt": "lucide license\n",
            }
            for relative, content in expected.items():
                source = root / relative.replace("/", "-")
                source.write_text(content, encoding="utf-8", newline="\n")
                package_files[
                    f"csi_openbase-1.0.dist-info/licenses/{relative}"
                ] = source
            distribution = _Distribution(package_files)
            output = root / "backend-notices"

            with mock.patch.object(
                licenses.importlib.metadata,
                "distribution",
                return_value=distribution,
            ):
                licenses.copy_project_notices(output)

            found = {
                path.relative_to(output).as_posix(): path.read_text(encoding="utf-8")
                for path in output.rglob("*")
                if path.is_file()
            }

        self.assertEqual(found, expected)

    def test_copy_project_notices_rejects_binary_in_license_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            package_files: dict[str, Path] = {}
            for name in ("LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md"):
                source = root / name
                source.write_text(name, encoding="utf-8")
                package_files[f"csi_openbase-1.0.dist-info/licenses/{name}"] = source
            binary = root / "payload.bin"
            binary.write_bytes(b"\x00binary")
            package_files[
                "csi_openbase-1.0.dist-info/licenses/licenses/payload.bin"
            ] = binary
            distribution = _Distribution(package_files)

            with mock.patch.object(
                licenses.importlib.metadata,
                "distribution",
                return_value=distribution,
            ):
                with self.assertRaisesRegex(RuntimeError, "non-text"):
                    licenses.copy_project_notices(root / "output")

    def test_cpython_license_is_text_only_and_does_not_embed_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            invalid = root / "binary-LICENSE.txt"
            invalid.write_bytes(b"\x00not text")
            valid = root / "LICENSE.txt"
            valid.write_text(
                "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\nTerms...\n",
                encoding="utf-8",
            )
            output = root / "CPython-LICENSE.txt"

            licenses.write_cpython_license(output, [invalid, valid])
            content = output.read_text(encoding="utf-8")

        self.assertIn("PYTHON SOFTWARE FOUNDATION LICENSE", content)
        self.assertNotIn(str(root), content)
        self.assertNotIn("\x00", content)

    def test_cpython_license_fails_without_official_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "LICENSE.txt"
            source.write_text("unrelated license", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "CPython"):
                licenses.write_cpython_license(source.parent / "output.txt", [source])


if __name__ == "__main__":
    unittest.main()
