from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hsp.workgroup import discover_workgroups, scope_context_for


class WorkgroupDiscoveryTests(unittest.TestCase):
    def test_discovers_nested_workgroup_stack_and_project_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            umbrella = Path(root)
            domain = umbrella / "domain"
            project = domain / "app"
            source = project / "src"
            source.mkdir(parents=True)
            (umbrella / "workgroup.toml").write_text(
                "[workgroup]\nname = 'umbrella'\nlevel = 'umbrella'\n",
                encoding="utf-8",
            )
            (domain / ".hsp").mkdir()
            (domain / ".hsp" / "workgroup.toml").write_text(
                "[workgroup]\nname = 'domain'\nlevel = 'domain'\n",
                encoding="utf-8",
            )
            (project / "pyproject.toml").write_text("[project]\nname = 'app'\n", encoding="utf-8")

            with patch.dict("os.environ", {"HSP_WORKGROUP_BOUNDARY": str(umbrella)}, clear=False):
                context = scope_context_for(source)

        self.assertFalse(context.fallback_workgroup)
        self.assertEqual(context.active_workgroup_root, str(domain.resolve()))
        self.assertEqual(context.parent_workgroup_root, str(umbrella.resolve()))
        self.assertEqual(context.project_root, str(project.resolve()))
        self.assertEqual([item.name for item in context.workgroups], ["umbrella", "domain"])

    def test_falls_back_to_location_when_no_marker_exists(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            with patch.dict("os.environ", {"HSP_WORKGROUP_BOUNDARY": root}, clear=False):
                context = scope_context_for(root)
                discovered = discover_workgroups(root)

        self.assertTrue(context.fallback_workgroup)
        self.assertEqual(context.active_workgroup_root, str(Path(root).resolve()))
        self.assertEqual(discovered, [])
