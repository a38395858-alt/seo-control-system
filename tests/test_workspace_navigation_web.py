"""Red-first contract for the multi-page React workspace navigation."""

from pathlib import Path
import json
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_FILE = PROJECT_ROOT / "frontend" / "src" / "App.tsx"
PACKAGE_FILE = PROJECT_ROOT / "frontend" / "package.json"


class WorkspaceNavigationWebTests(unittest.TestCase):
    """Sidebar entries must navigate between real route-level workspaces."""

    def setUp(self) -> None:
        self.app = APP_FILE.read_text(encoding="utf-8")
        self.package = json.loads(PACKAGE_FILE.read_text(encoding="utf-8"))

    def test_react_router_is_a_runtime_dependency(self) -> None:
        self.assertIn("react-router-dom", self.package["dependencies"])

    def test_sidebar_uses_route_navigation_not_hash_anchors(self) -> None:
        self.assertIn("NavLink", self.app)
        for route in ("/research", "/keywords", "/titles", "/title-library", "/scoring"):
            self.assertIn(f'to="{route}"', self.app)
        self.assertNotIn('href="#research"', self.app)

    def test_workspace_has_independent_keyword_title_and_scoring_routes(self) -> None:
        self.assertIn("<Routes>", self.app)
        for route in ("/research", "/keywords", "/titles", "/title-library", "/scoring"):
            self.assertIn(f'path="{route}"', self.app)


if __name__ == "__main__":
    unittest.main()
