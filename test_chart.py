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

    def test_french_decimal_comma(self):
        self.assertEqual(C._num("4227,982", True), (4227.982, 3))
        self.assertEqual(C._num("4 227,982", True), (4227.982, 3))
        self.assertEqual(C._num("2.660,00"), (2660.0, 2))
        self.assertTrue(C._comma_is_decimal(["4236,000", "4232,000"]))
        self.assertFalse(C._comma_is_decimal(["2,660", "2,655"]))

    def test_pair_from_tradingview_names(self):
        self.assertEqual(C._pair_from_names("Or / Dollar Américain · 1h · OANDA"), "XAUUSD")
        self.assertEqual(C._pair_from_names("Euro / U.S. Dollar · 15"), "EURUSD")
        self.assertEqual(C._pair_from_names("Livre Sterling / Yen Japonais"), "GBPJPY")
        self.assertIsNone(C._pair_from_names("Indicateur / Volume"))

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

    def test_real_tradingview_snapshot_french_compressed(self):
        """Vraie capture TradingView d'un utilisateur (export « instantané », cadre noir, format français,
        zones opaques, bande plus foncée près de l'entrée, ligne de prix qui traverse la zone),
        réduite et compressée comme le fait Telegram. Les niveaux sont lus EXACTEMENT sur l'échelle."""
        r = C.read_position_tool(load("tv_reel_xauusd_buy_fr.jpg"))
        self.assertEqual((r.pair, r.direction), ("XAUUSD", "BUY"))
        self.assertEqual((r.fmt(r.entry), r.fmt(r.sl), r.fmt(r.tp)), ("4163.285", "4154.141", "4227.982"))
        self.assertEqual(sorted(r.exact), ["entry", "sl", "tp"])

    def test_two_targets_and_gray_stop_zone(self):
        """Vraie capture : zone de profit pêche, zone de stop GRISE, deux lignes de TP (TP1 et TP2)."""
        r = C.read_position_tool(load("tv_reel_xauusd_2tp_gris.jpg"))
        self.assertEqual((r.pair, r.direction), ("XAUUSD", "BUY"))
        self.assertEqual((r.fmt(r.entry), r.fmt(r.sl)), ("4163.285", "4154.141"))
        self.assertEqual([r.fmt(t) for t in r.tps], ["4218.060", "4228.111"])

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
        u.effective_message = m
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

    def test_capture_with_two_targets_keeps_both(self):
        b = self.bot
        upd = self._update(image=load("tv_reel_xauusd_2tp_gris.jpg"))
        self.assertEqual(asyncio.run(b.chart_signal(upd, self.ctx)), b.CONFIRM)
        self.assertEqual(self.ctx.user_data["sig"]["tps_text"], ["4218.060", "4228.111"])

    def test_unreadable_capture_explains_what_is_needed(self):
        b = self.bot
        upd = self._update(image=load("tv_sans_outil.jpg"))
        state = asyncio.run(b.chart_signal(upd, self.ctx))
        self.assertEqual(state, b.ConversationHandler.END)
        wait = upd.message.reply_text.return_value
        self.assertIn("Position longue / courte", wait.edit_text.call_args.args[0])


class EditBeforePublishTests(unittest.TestCase):
    """Bouton ✏️ Modifier de l'aperçu : on change un champ, le signal est revalidé, la photo est gardée."""

    def setUp(self):
        os.environ.update(ADMIN_IDS="42", CHANNEL_ID="-1001", WATERMARK="false")
        import bot
        self.b = bot
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bot.DB_PATH = os.path.join(self.tmp.name, "s.db")
        bot.init_db()
        self.sig = bot.build_signal({"pair": "XAUUSD", "direction": "BUY", "entry": "2650", "sl": "2645",
                                     "tp": ["2660"], "note": "Cassure"})
        self.sig.update(photo="photo-id", source="capture")

    def test_change_one_field_keeps_the_rest(self):
        s = self.b.edit_signal(self.sig, "sl", "2643,5")
        self.assertEqual((s["sl_text"], s["entry_text"], s["photo"], s["source"]), ("2643.5", "2650", "photo-id", "capture"))
        s = self.b.edit_signal(s, "tp", "2670 2660 2680")
        self.assertEqual(s["tps_text"], ["2660", "2670", "2680"])
        self.assertIsNone(self.b.edit_signal(s, "note", "-")["note"])
        self.assertEqual(self.b.edit_signal(s, "pair", "gold")["pair"], "XAUUSD")

    def test_flip_direction_swaps_stop_and_target(self):
        s = self.b.edit_signal(self.sig, "dir", "sell")
        self.assertEqual((s["direction"], s["sl_text"], s["tps_text"]), ("SELL", "2660", ["2645"]))

    def test_inconsistent_value_is_refused(self):
        with self.assertRaises(ValueError):
            self.b.edit_signal(self.sig, "sl", "2655")

    def test_flow_from_preview_button(self):
        b, ctx = self.b, MagicMock()
        b.ADMIN_IDS = {42}
        ctx.user_data = {"sig": self.sig}
        ctx.bot.send_photo = AsyncMock(return_value=MagicMock(message_id=2))
        ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=2))

        def cb(data):
            u = MagicMock()
            u.effective_user.id = u.effective_chat.id = 42
            u.callback_query.data = data
            u.callback_query.answer = AsyncMock()
            u.callback_query.edit_message_reply_markup = AsyncMock()
            u.callback_query.message.reply_text = AsyncMock()
            u.effective_message.reply_text = AsyncMock()
            return u
        self.assertEqual(asyncio.run(b.sig_confirm(cb("ok:edit"), ctx)), b.CONFIRM)
        self.assertEqual(asyncio.run(b.sig_edit_field(cb("edit:entry"), ctx)), b.EDIT_VALUE)
        u = MagicMock()
        u.effective_user.id = u.effective_chat.id = 42
        u.callback_query = None
        u.message.text = "2651"
        u.message.reply_text = AsyncMock()
        u.effective_message.reply_text = AsyncMock()
        self.assertEqual(asyncio.run(b.sig_edit_value(u, ctx)), b.CONFIRM)
        self.assertEqual(ctx.user_data["sig"]["entry_text"], "2651")
        self.assertTrue(ctx.bot.send_photo.called or ctx.bot.send_message.called)   # nouvel aperçu


if __name__ == "__main__":
    unittest.main()
