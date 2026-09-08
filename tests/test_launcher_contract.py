from __future__ import annotations

import re
import unittest
from pathlib import Path


LAUNCHER_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "Sources"
    / "CSIBackendLauncher"
    / "main.c"
)


class LauncherContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

    def test_backend_parent_identity_is_rebound_before_exec(self) -> None:
        child_branch = self.source.index("if (child == 0)")
        set_parent = self.source.index(
            'setenv("CSI_OPENBASE_PARENT_PID", parent_value, 1)',
            child_branch,
        )
        execute = self.source.index("execl(argv[1]", child_branch)
        self.assertLess(set_parent, execute)
        self.assertIn("pid_t supervisor_pid = getpid();", self.source[:child_branch])

    def test_process_group_identifier_stays_anchored_until_cleanup(self) -> None:
        observe = self.source.index("WNOWAIT")
        group_kill = self.source.index("kill(-child, SIGKILL)")
        reap = self.source.index("return reap_child(child)", group_kill)
        self.assertLess(observe, group_kill)
        self.assertLess(group_kill, reap)
        self.assertRegex(self.source, re.compile(r"setpgid\(child, child\)"))


if __name__ == "__main__":
    unittest.main()
