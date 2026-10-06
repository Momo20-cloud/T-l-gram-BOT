"""Tests de la lecture des captures TradingView :  python -m unittest test_chart -v

Les images de tests_fixtures/ imitent ce que reçoit le robot : captures au style TradingView,
réduites à 1280 px et compressées en JPEG comme le fait Telegram.
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

import chart_reader as C

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "tests_fixtures")


def load(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


class PureFunctionTests(unittest.TestCase):
    def test_numbers_with_thousands_separator(self):
        self.assertEqual(C._num("2,660.00"), (2660.0, 2))
        self.assertEqual(C._num("1.08300"), (1.083, 5))
        self.assertEqual(C._num("63500"), (63500.0, 0))
        self.assertIsNone(C._num("1h"))

    def test_scale_fit_ignores_misread_labels(self):
        pts = [(100, 192.2, 3), (200, 192.0, 3), (300, 191.8, 3), (400, 191.6, 3),
               (350, 181.7, 3),            # « 191.7 » mal lu
               (500, 191.4, 3)]
        a, b, dec, _ = C.fit_scale(pts)
        self.assertAlmostEqual(a * 250 + b, 191.9, places=3)
        self.assertEqual(dec, 3)

    def test_scale_needs_enough_labels(self):
        with self.assertRaises(C.ChartReadError):
            C.fit_scale([(10, 1.0, 1), (20, 0.9, 1)])

    def test_snap_only_within_half_a_pixel(self):
        self.assertEqual(C._snap(2649.99, 2, px=0.03), 2650.0)
        self.assertEqual(C._snap(2651.37, 2, px=0.03), 2651.37)     # vrai prix non rond : on n'y touche pas


@unittest.skipUnless(C.available(), "Tesseract n'est pas installé")
class ReadCaptureTests(unittest.TestCase):
    def assertClose(self, got, want, tol):
        self.assertLessEqual(abs(got - want), tol, f"{got} au lieu de {want}")

    def test_buy_dark_theme(self):
        r = C.read_position_tool(load("tv_xauusd_buy_dark.jpg"))
        self.assertEqual((r.pair, r.direction), ("XAUUSD", "BUY"))
        self.assertClose(r.entry, 2650.0, 0.1)
        self.assertClose(r.sl, 2645.0, 0.1)
        self.assertClose(r.tp, 2660.0, 0.1)

    def test_sell_light_theme_odd_prices(self):
        r = C.read_position_tool(load("tv_eurusd_sell_light.jpg"))
        self.assertEqual((r.pair, r.direction), ("EURUSD", "SELL"))
        self.assertClose(r.entry, 1.08637, 0.00005)
        self.assertClose(r.sl, 1.08812, 0.00005)
        self.assertClose(r.tp, 1.08214, 0.00005)

    def test_tool_with_labels_reads_exact_values(self):
        r = C.read_position_tool(load("tv_gbpjpy_sell_labels.jpg"))
        self.assertEqual((r.pair, r.direction), ("GBPJPY", "SELL"))
        self.assertClose(r.sl, 191.65, 0.006)
        self.assertClose(r.tp, 190.45, 0.006)
        self.assertTrue(r.exact)                  # au moins un niveau lu tel quel sur l'image

    def test_image_without_tool_is_refused(self):
        with self.assertRaises(C.ChartReadError):
            C.read_position_tool(load("tv_sans_outil.jpg"))


@unittest.skipUnless(C.available(), "Tesseract n'est pas installé")
class BotFlowTests(unittest.TestCase):
    """Capture envoyée au robot → aperçu ; correction tapée → nouvel aperçu avec la même photo."""

    def setUp(self):
        os.environ.update(ADMIN_IDS="42", CHANNEL_ID="-1001", WATERMARK="false")
        import bot
        self.bot = bot
        self.tmp = tempfile.TemporaryDirectory()
        bot.DB_PATH = os.path.join(self.tmp.name, "s.db")
        bot.ADMIN_IDS = {42}
        bot.init_db()
        self.ctx = MagicMock()
        self.ctx.user_data = {}
        self.sent = []

        async def send_message(chat_id, text, **k):
            self.sent.append(("texte", chat_id, text))
            return MagicMock(message_id=1, photo=None)

        async def send_photo(chat_id, photo, caption=None, **k):
            self.sent.append(("photo", chat_id, caption))
            return MagicMock(message_id=2, photo=None)
        self.ctx.bot.send_message, self.ctx.bot.send_photo = send_message, send_photo

    def tearDown(self):
        self.tmp.cleanup()

    def _update(self, caption=None, image=None, text=None):
        u = MagicMock()
        u.effective_user.id = 42
        u.effective_chat.id = 42
        u.callback_query = None
        m = u.message
        m.text, m.caption = text, caption
        m.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock(), delete=AsyncMock()))
        if image is not None:
            m.photo = []
            f = MagicMock()
            f.download_as_bytearray = AsyncMock(return_value=bytearray(image))
            m.document.get_file = AsyncMock(return_value=f)
        return u

    def test_capture_then_correction(self):
        b = self.bot
        upd = self._update(caption="Cassure H1", image=load("tv_xauusd_buy_dark.jpg"))
        state = asyncio.run(b.chart_signal(upd, self.ctx))
        self.assertEqual(state, b.CONFIRM)
        s = self.ctx.user_data["sig"]
        self.assertEqual((s["pair"], s["direction"], s["source"]), ("XAUUSD", "BUY", "capture"))
        self.assertEqual(s["note"], "Cassure H1")
        self.assertIsNotNone(s["photo_bytes"])                   # envoyée en fichier : l'image est gardée
        replies = " ".join(c.args[0] for c in upd.message.reply_text.call_args_list)
        self.assertIn("Lu sur ta capture", replies)
        self.assertTrue(any(kind == "photo" for kind, *_ in self.sent))   # aperçu avec la photo

        fix = self._update(text="XAUUSD BUY 2650 SL 2644 TP 2660 2670")
        state = asyncio.run(b.sig_correct(fix, self.ctx))
        self.assertEqual(state, b.CONFIRM)
        s2 = self.ctx.user_data["sig"]
        self.assertEqual((s2["sl_text"], s2["tps_text"]), ("2644", ["2660", "2670"]))
        self.assertEqual(s2["photo_bytes"], s["photo_bytes"])     # même photo
        self.assertEqual(s2["note"], "Cassure H1")

    def test_unreadable_capture_explains_what_is_needed(self):
        b = self.bot
        upd = self._update(image=load("tv_sans_outil.jpg"))
        state = asyncio.run(b.chart_signal(upd, self.ctx))
        self.assertEqual(state, b.ConversationHandler.END)
        wait = upd.message.reply_text.return_value
        self.assertIn("Position longue / courte", wait.edit_text.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
