from pathlib import Path
import unittest


class WebPanelGatewayTests(unittest.TestCase):
    def test_panel_renders_gateway_route_and_latency_metadata(self):
        html = Path(__file__).resolve().parents[1].joinpath("web_panel.html").read_text(encoding="utf-8")
        self.assertIn("d.backend", html)
        self.assertIn("d.reason", html)
        self.assertIn("d.latency", html)
        self.assertIn("/api/health", html)


if __name__ == "__main__":
    unittest.main()
