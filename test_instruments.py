"""Tests du catalogue d'actifs :  python -m unittest test_instruments -v"""
import os
import unittest

os.environ.setdefault("WATERMARK", "false")
import bot  # noqa: E402
import instruments as I  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_aliases_and_broker_suffixes(self):
        cases = {"gold": "XAUUSD", "EUR/USD": "EURUSD", "XAUUSD.m": "XAUUSD", "XAUUSDm": "XAUUSD",
                 "EURUSD.pro": "EURUSD", "NASDAQ": "NAS100", "US100": "NAS100", "DAX": "GER40", "DE40": "GER40",
                 "S&P500": "US500", "WTI": "USOIL", "Brent": "UKOIL", "bitcoin": "BTCUSD", "BTC-USD": "BTCUSD",
                 "US30": "US30", "GBPJPY": "GBPJPY", "BTCUSDT": "BTCUSDT"}
        for raw, want in cases.items():
            self.assertEqual(I.normalize(raw), want, raw)

    def test_catalogue_size(self):
        self.assertGreaterEqual(len(I.PIP_SIZES), 100)
        for sym in ("EURUSD", "GBPJPY", "AUDNZD", "USDZAR", "XAUUSD", "US30", "JPN225", "USOIL", "SOLUSD"):
            self.assertTrue(I.is_known(sym), sym)
        self.assertFalse(I.is_known("ABCXYZ"))


class PipTests(unittest.TestCase):
    def test_realistic_pips(self):
        cases = [("XAUUSD", 2650, 2660, 100), ("EURUSD", 1.0850, 1.0860, 10), ("USDJPY", 150.20, 150.50, 30),
                 ("US30", 42000, 42100, 100), ("USOIL", 75.20, 76.20, 100), ("SOLUSD", 150, 155, 500),
                 ("BTCUSDT", 63000, 63500, 500), ("GOLD", 2650, 2660, 100)]
        for pair, entry, price, want in cases:
            self.assertEqual(bot.calc_pips(pair, "BUY", entry, price), want, pair)

    def test_vip_assets_unchanged(self):
        # mêmes valeurs qu'avant le catalogue pour les actifs déjà gérés
        for pair, pip in (("XAUUSD", 0.1), ("XAGUSD", 0.01), ("BTCUSD", 1.0), ("ETHUSD", 0.1), ("EURUSD", 0.0001),
                          ("GBPUSD", 0.0001), ("USDJPY", 0.01), ("US30", 1.0), ("NAS100", 1.0), ("GER40", 1.0),
                          ("US500", 0.1)):
            self.assertEqual(bot.pip_size(pair), pip, pair)


class QuickModeTests(unittest.TestCase):
    def test_quick_signal_symbols(self):
        cases = {"GOLD BUY 2650 SL 2645 TP 2660": "XAUUSD",
                 "EUR/USD BUY 1.085 SL 1.083 TP 1.088": "EURUSD",
                 "nasdaq sell 20000 sl 20050 tp 19900": "NAS100",
                 "XAUUSD.m BUY 2650 SL 2645 TP 2660": "XAUUSD",
                 "WTI BUY 75.2 SL 74.8 TP 76": "USOIL",
                 "S&P500 BUY 5800 SL 5790 TP 5820": "US500"}
        for text, want in cases.items():
            self.assertEqual(bot.parse_quick_signal(text)["pair"], want, text)

    def test_zone_entry_still_works(self):
        s = bot.parse_quick_signal("XAUUSD BUY 2650-2652 SL 2645 TP 2660")
        self.assertEqual(s["entry_text"], "2650 – 2652")

    def test_unknown_symbol_warns_but_is_accepted(self):
        s = bot.parse_quick_signal("ABCXYZ BUY 10 SL 9 TP 12")
        self.assertEqual(s["pair"], "ABCXYZ")
        self.assertIn("pas dans le catalogue", bot.unknown_pair_warning(s["pair"]))
        self.assertEqual(bot.unknown_pair_warning("XAUUSD"), "")


class KeyboardTests(unittest.TestCase):
    def test_menu_has_favourites_and_categories(self):
        rows = bot._pairs_kb_rows()
        data = [b.callback_data for r in rows for b in r]
        self.assertIn("pair:XAUUSD", data)
        for key in I.CATEGORIES:
            self.assertIn(f"cat:{key}", data)
        for r in rows:
            for b in r:
                self.assertLessEqual(len(b.callback_data.encode()), 64)


if __name__ == "__main__":
    unittest.main()
