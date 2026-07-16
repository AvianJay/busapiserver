from __future__ import annotations

from pathlib import Path
import unittest

from fastapi.responses import FileResponse

from app.api.routes import brand_icon, root


PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "app" / "web"


class BrandPageTests(unittest.TestCase):
    def test_root_returns_brand_page(self) -> None:
        response = root()

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(Path(response.path), WEB_ROOT / "index.html")
        self.assertEqual(response.media_type, "text/html")

    def test_brand_icon_returns_png(self) -> None:
        response = brand_icon()

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(
            Path(response.path),
            WEB_ROOT / "static" / "icon_transparent.png",
        )
        self.assertEqual(response.media_type, "image/png")

    def test_brand_page_explains_app_and_links_to_web_app(self) -> None:
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("<h1 id=\"page-title\">YetAnotherBusApp</h1>", html)
        self.assertIn("應用程式用途", html)
        self.assertIn("Google 或 Discord OAuth", html)
        self.assertIn('href="https://busapp.avianjay.sbs/"', html)
        self.assertIn('href="#features"', html)
        self.assertIn('href="#account"', html)
        self.assertIn('href="/auth"', html)
        self.assertIn('href="/privacy-policy"', html)
        self.assertIn('href="/terms-of-service"', html)
        self.assertIn('aria-label="主要導覽"', html)


if __name__ == "__main__":
    unittest.main()
