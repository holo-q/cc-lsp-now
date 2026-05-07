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
        self.assertEqual(context.workgroup_source, "fallback")
        self.assertEqual(discovered, [])

    def test_project_root_does_not_escape_active_workgroup(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            umbrella = Path(root)
            domain = umbrella / "domain"
            domain.mkdir()
            (umbrella / "workgroup.toml").write_text(
                "[workgroup]\nname = 'umbrella'\nlevel = 'umbrella'\n",
                encoding="utf-8",
            )
            (umbrella / "Justfile").write_text("default:\n  echo umbrella\n", encoding="utf-8")
            (domain / "workgroup.toml").write_text(
                "[workgroup]\nname = 'domain'\nlevel = 'domain'\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"HSP_WORKGROUP_BOUNDARY": str(umbrella)}, clear=False):
                context = scope_context_for(domain)

        self.assertEqual(context.active_workgroup_root, str(domain.resolve()))
        self.assertEqual(context.project_root, str(domain.resolve()))

    def test_workgroup_observation_policy_supports_network_roots(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            umbrella = Path(root)
            domain = umbrella / "domain"
            sibling = umbrella / "sibling"
            domain.mkdir()
            sibling.mkdir()
            (domain / "workgroup.toml").write_text(
                "[workgroup]\nname = 'domain'\nlevel = 'domain'\n"
                "[observe]\nmode = 'network'\nroots = ['../sibling']\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"HSP_WORKGROUP_BOUNDARY": str(umbrella)}, clear=False):
                context = scope_context_for(domain)

        self.assertEqual(context.observation_mode, "network")
        self.assertEqual(
            context.observation_roots,
            (str(domain.resolve()), str(sibling.resolve())),
        )

    def test_fallback_workgroup_observation_is_exact(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            with patch.dict("os.environ", {"HSP_WORKGROUP_BOUNDARY": root}, clear=False):
                context = scope_context_for(root)

        self.assertEqual(context.observation_mode, "exact")
        self.assertEqual(context.observation_roots, (str(Path(root).resolve()),))
