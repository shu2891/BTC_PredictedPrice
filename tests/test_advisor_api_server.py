import unittest
import urllib.error
import urllib.parse
import urllib.request
from argparse import Namespace
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

import advisor_api_server as api_server


class HtmlPageTests(unittest.TestCase):
    def test_page_uses_dom_render_instead_of_results_innerhtml(self) -> None:
        page = api_server.html_page("conservative", "off")

        self.assertIn("results.replaceChildren()", page)
        self.assertIn("document.createElement('div')", page)
        self.assertIn("textContent", page)
        self.assertNotIn("results.innerHTML =", page)
        self.assertIn("toFixed(4)", page)


class ApiHandlerTests(unittest.TestCase):
    def _serve_once(self, path: str) -> tuple[int, str]:
        args = Namespace(
            host="127.0.0.1",
            port=0,
            risk_profile="conservative",
            llama="off",
            llama_model="unused",
            quote="USDT",
            timeout=1,
        )

        handler = api_server.build_handler(args)
        server = ThreadingHTTPServer((args.host, args.port), handler)
        port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_api_analyze_returns_results(self) -> None:
        fake_result = {
            "symbol": "BTCUSDT",
            "price": 74123.0,
            "decision": "watch",
            "risk_level": "high",
            "risk_score": 55,
            "beginner_summary": {"headline": "test"},
            "actionable_levels": {},
            "long_short_plan": {},
            "short_term_signal": {},
        }

        with (
            patch.object(api_server, "fetch_rss_items", return_value=[]),
            patch.object(api_server, "normalize_symbol", side_effect=lambda symbol, quote: f"{symbol}{quote}"),
            patch.object(api_server, "build_symbol_analysis", return_value=object()),
            patch.object(api_server, "analysis_to_dict", return_value=fake_result),
        ):
            status, body = self._serve_once("/api/analyze?symbols=BTC")

        payload = api_server.json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["results"][0]["symbol"], "BTCUSDT")
        self.assertEqual(payload["errors"], [])

    def test_api_analyze_collects_symbol_errors_without_failing_whole_request(self) -> None:
        fake_result = {
            "symbol": "BTCUSDT",
            "price": 74123.0,
            "decision": "watch",
            "risk_level": "high",
            "risk_score": 55,
            "beginner_summary": {"headline": "test"},
            "actionable_levels": {},
            "long_short_plan": {},
            "short_term_signal": {},
        }

        def build_side_effect(**kwargs):
            if kwargs["symbol"] == "ETHUSDT":
                raise RuntimeError("fail eth")
            return object()

        with (
            patch.object(api_server, "fetch_rss_items", return_value=[]),
            patch.object(api_server, "normalize_symbol", side_effect=lambda symbol, quote: f"{symbol}{quote}"),
            patch.object(api_server, "build_symbol_analysis", side_effect=build_side_effect),
            patch.object(api_server, "analysis_to_dict", return_value=fake_result),
        ):
            status, body = self._serve_once("/api/analyze?symbols=BTC,ETH")

        payload = api_server.json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["symbol"], "BTCUSDT")
        self.assertEqual(payload["errors"][0]["symbol"], "ETHUSDT")
        self.assertIn("fail eth", payload["errors"][0]["error"])

    def test_api_analyze_rejects_missing_symbols(self) -> None:
        query = urllib.parse.urlencode({"symbols": ""})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._serve_once(f"/api/analyze?{query}")
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
