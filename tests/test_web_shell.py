"""Build contract for the React keyword-discovery workspace."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = PROJECT_ROOT / "web"
INDEX_FILE = WEB_DIR / "index.html"
APP_FILE = PROJECT_ROOT / "frontend" / "src" / "App.tsx"


class WebShellContractTests(unittest.TestCase):
    def test_vite_build_exposes_a_react_mount_and_bundled_assets(self) -> None:
        document = INDEX_FILE.read_text(encoding="utf-8")
        self.assertIn('id="root"', document)
        self.assertIn('/assets/', document)
        self.assertTrue((WEB_DIR / "assets").is_dir())

    def test_react_workspace_exposes_navigation_and_seed_expansion_entry(self) -> None:
        document = APP_FILE.read_text(encoding="utf-8")
        for element_id in ("research", "review", "library", "seed-keywords", "start-suggest-expansion"):
            self.assertIn(f'"{element_id}"', document)
