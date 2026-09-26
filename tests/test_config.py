import tempfile
import unittest
from pathlib import Path

from coindata.config import Config, ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent


class ConfigTest(unittest.TestCase):
    def _load(self, text: str) -> Config:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coindata.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_defaults_without_file(self) -> None:
        config = load_config(None)
        self.assertEqual(config.data.symbol, "ETHUSDT")
        self.assertEqual(config.data.init_days, 130)
        self.assertEqual(config.indicators.zigzag.k, 2.0)
        self.assertEqual(config.events.report_bars, {"15m": 16, "30m": 8, "1h": 8, "1d": 3})

    def test_partial_file_keeps_other_defaults(self) -> None:
        config = self._load("[indicators.zigzag]\nk = 2.5\n[data]\ninit_days = 10\n")
        self.assertEqual(config.indicators.zigzag.k, 2.5)
        self.assertEqual(config.data.init_days, 10)
        self.assertEqual(config.indicators.atr.n, 14)

    def test_int_accepted_for_float(self) -> None:
        self.assertEqual(self._load("[indicators.zigzag]\nk = 3\n").indicators.zigzag.k, 3.0)

    def test_unknown_key_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "indicators.zigzag.kk"):
            self._load("[indicators.zigzag]\nkk = 2.5\n")

    def test_wrong_type_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "data.init_days"):
            self._load('[data]\ninit_days = "130"\n')
        with self.assertRaisesRegex(ConfigError, "data.init_days"):
            self._load("[data]\ninit_days = true\n")

    def test_invalid_value_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "rate_limit_ratio"):
            self._load("[runtime]\nrate_limit_ratio = 1.5\n")

    def test_timeframe_lists_checked(self) -> None:
        with self.assertRaisesRegex(ConfigError, "levels.normalize_tf"):
            self._load('[indicators]\ntimeframes = ["15m", "1d"]\n[levels]\nswing_timeframes = ["15m"]\n')
        with self.assertRaisesRegex(ConfigError, "타임프레임 표기"):
            self._load('[levels]\nnormalize_tf = "1x"\n')
        with self.assertRaisesRegex(ConfigError, "events.report_bars"):
            self._load('[events]\nreport_bars = { "15m" = 16 }\n')

    def test_missing_file_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(Path("/nonexistent/coindata.toml"))

    def test_example_file_matches_defaults(self) -> None:
        """예시 설정 파일의 값은 코드의 기본값과 같아야 한다(기본값이 두 곳에서 어긋나지 않게)."""
        self.assertEqual(load_config(ROOT / "coindata.example.toml"), Config())


if __name__ == "__main__":
    unittest.main()
