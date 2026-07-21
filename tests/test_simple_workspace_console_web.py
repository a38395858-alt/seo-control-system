from pathlib import Path
import unittest


class SimpleWorkspaceConsoleWebTests(unittest.TestCase):
    def test_each_site_tab_has_a_confirmed_delete_control(self) -> None:
        page = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "AgentPlatformConsole.tsx").read_text(encoding="utf-8")
        self.assertIn("删除网站", page)
        self.assertIn('method:"DELETE"', page)
        self.assertIn("确认删除网站", page)

    def test_site_has_its_own_route_and_module_dashboard(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "frontend" / "src" / "AgentPlatformConsole.tsx").read_text(encoding="utf-8")
        app = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn("/projects/:siteId", app)
        self.assertIn("Website Project", app)
        self.assertIn("关键词挖掘", app)
        self.assertIn('href={link("/research")}', page)
        self.assertIn('href="#knowledge"', page)
        self.assertIn("site-dashboard", page)
        self.assertIn('className="module-card"', page)
        self.assertIn('href={link("/content")}', page)
        server = (root / "src" / "seo_control" / "web.py").read_text(encoding="utf-8")
        self.assertIn('r"/(agent-platform/site|projects)/\\d+"', server)
