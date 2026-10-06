"""Tests de /reglages :  python -m unittest test_reglages -v"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.update(WATERMARK="true", ADMIN_IDS="42", CHANNEL_ID="-1001")
import bot  # noqa: E402
from telegram.ext import Application  # noqa: E402


def _update(data=None, text=None):
    u = MagicMock()
    u.effective_user.id = 42
    u.message.text = text
    u.message.reply_text = AsyncMock()
    if data:
        u.callback_query = q = MagicMock()
        q.data = data
        q.answer, q.edit_message_text, q.edit_message_reply_markup = AsyncMock(), AsyncMock(), AsyncMock()
        q.message.reply_text = AsyncMock()
    else:
        u.callback_query = None
    return u


class ReglagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bot.DB_PATH = os.path.join(self.tmp.name, "s.db")
        bot.init_db()
        bot.apply_settings()
        self.app = Application.builder().token("1234567:" + "x" * 35).build()
        self.ctx = MagicMock()
        self.ctx.user_data = {}
        self.ctx.application = self.app

    def tearDown(self):
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_defaults_come_from_env(self):
        s = bot.get_settings()
        self.assertEqual(s["filigrane"], "true")
        self.assertEqual(s["bilan_heure"], "22:00")

    def test_validation(self):
        self.assertEqual(bot._check_setting("favoris", "gold, nasdaq ,btc"), "XAUUSD,NAS100,BTCUSD")
        self.assertEqual(bot._check_setting("bilan_heure", "7h30"), "07:30")
        self.assertEqual(bot._check_setting("bilan_heure", "off"), "")
        self.assertEqual(bot._check_setting("fuseau", "Europe/Paris"), "Europe/Paris")
        for key, bad in (("bilan_heure", "25:00"), ("fuseau", "Mars/Olympus"), ("favoris", ""), ("tp_pct", "0")):
            with self.assertRaises(ValueError):
                bot._check_setting(key, bad)

    def test_toggle_and_menu_choice_apply_immediately(self):
        async def go():
            await bot.on_settings_button(_update("set:toggle:filigrane"), self.ctx)
            self.assertFalse(bot.WATERMARK)
            await bot.on_settings_button(_update("set:set:tp_pct:25"), self.ctx)
            self.assertEqual(bot.TP_DEFAULT_PCT, 25)
            await bot.on_settings_button(_update("set:set:fuseau:Africa/Casablanca"), self.ctx)
            self.assertEqual(bot.TZ.key, "Africa/Casablanca")
        self.run_async(go())
        bot.apply_settings()                     # relu depuis la base (survit au redémarrage)
        self.assertFalse(bot.WATERMARK)
        self.assertEqual(bot.TZ.key, "Africa/Casablanca")

    def test_typed_favourites_and_bad_value(self):
        async def go():
            await bot.on_settings_button(_update("set:ask:favoris"), self.ctx)
            self.assertEqual(self.ctx.user_data["await"], ("setting", "favoris"))
            await bot.on_admin_text(_update(text="US30, gold, GBP/JPY"), self.ctx)
            self.assertEqual(bot.QUICK_PAIRS, ["US30", "XAUUSD", "GBPJPY"])
            await bot.on_settings_button(_update("set:ask:bilan_heure"), self.ctx)
            u = _update(text="midi")
            await bot.on_admin_text(u, self.ctx)
            self.assertIn("heure invalide", u.message.reply_text.call_args.args[0])
            self.assertEqual(self.ctx.user_data["await"], ("setting", "bilan_heure"))   # on redemande
        self.run_async(go())
        self.assertEqual([b.text for b in bot._pairs_kb_rows()[0]], ["US30", "XAUUSD"])

    def test_report_jobs_rescheduled(self):
        async def go():
            jq = self.app.job_queue
            jq.set_application(self.app)
            await jq.start()
            try:
                bot.schedule_reports(jq)
                self.assertEqual(len(jq.get_jobs_by_name("daily")), 1)
                bot.save_setting("bilan_heure", "off")
                await bot._after_change(self.ctx, "bilan_heure")
                await asyncio.sleep(0.05)
                self.assertEqual(jq.get_jobs_by_name("daily"), ())
                bot.save_setting("bilan_heure", "21:30")
                await bot._after_change(self.ctx, "bilan_heure")
                await asyncio.sleep(0.05)
                job = jq.get_jobs_by_name("daily")[0]
                self.assertEqual((job.next_t.astimezone(bot.TZ).hour, job.next_t.astimezone(bot.TZ).minute), (21, 30))
                self.assertEqual(len(jq.get_jobs_by_name("weekly")), 1)
            finally:
                await jq.stop()
        self.run_async(go())

    def test_settings_screen(self):
        txt = bot.settings_text(bot.get_settings())
        self.assertIn("Réglages du robot", txt)
        data = [b.callback_data for r in bot.settings_kb().inline_keyboard for b in r]
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))


if __name__ == "__main__":
    unittest.main()
