"""CLI 参数的离线回归，不启动服务器或联网。"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch, sentinel

ROOT = Path(__file__).resolve().parents[1]


class WebCliTests(unittest.TestCase):
    def test_explicit_proxy_whitelist_is_forwarded_without_starting_server(self):
        from highscore_web.__main__ import main
        with patch.dict(os.environ, {"HIGHSCORE_TRUSTED_PROXY_IPS": "127.0.0.1,172.21.0.1"}), \
                patch("highscore_web.app.create_app", return_value=sentinel.application), \
                patch("uvicorn.run") as run:
            self.assertEqual(main(["serve", "--root", str(ROOT)]), 0)
        self.assertEqual(run.call_args.kwargs.get("forwarded_allow_ips"), "127.0.0.1,172.21.0.1")
        self.assertIs(run.call_args.kwargs.get("proxy_headers"), True)
        self.assertEqual(run.call_args.kwargs["workers"], 1)
        self.assertIs(run.call_args.args[0], sentinel.application)

    def test_proxy_whitelist_defaults_to_loopback_only(self):
        from highscore_web.__main__ import main
        with patch.dict(os.environ), \
                patch("highscore_web.app.create_app", return_value=sentinel.application), \
                patch("uvicorn.run") as run:
            os.environ.pop("HIGHSCORE_TRUSTED_PROXY_IPS", None)
            self.assertEqual(main(["serve", "--root", str(ROOT)]), 0)
        self.assertEqual(run.call_args.kwargs.get("forwarded_allow_ips"), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
