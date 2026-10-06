"""Tests de l'usine à robots :  python -m unittest test_usine -v"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import timedelta

from usine_db import STATUS_ACTIVE, STATUS_SUSPENDED, ClientStore, is_running_allowed, needs_reminder, utc_now
from usine import Supervisor

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_A = "1111111:" + "A" * 35
TOKEN_B = "2222222:" + "B" * 35

# Faux robot : écrit ses variables d'environnement puis attend (ou s'arrête si FAKE_EXIT est défini)
FAKE_BOT = textwrap.dedent("""
    import json, os, sys, time
    json.dump(dict(os.environ), open("env.json", "w"))
    if os.path.exists("exit_now"):
        sys.exit(1)
    time.sleep(60)
""")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ClientStore(os.path.join(self.tmp.name, "usine.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_trial_then_expiry(self):
        now = utc_now()
        cid = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7, now=now)
        c = self.store.get(cid)
        self.assertTrue(is_running_allowed(c, now))
        self.assertTrue(is_running_allowed(c, now + timedelta(days=6)))
        self.assertFalse(is_running_allowed(c, now + timedelta(days=7, seconds=1)))

    def test_extend_adds_to_remaining_time(self):
        now = utc_now()
        cid = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7, now=now)
        self.store.extend(cid, 30, "STARS", now=now)
        self.assertTrue(is_running_allowed(self.store.get(cid), now + timedelta(days=36)))

    def test_extend_after_expiry_starts_today(self):
        now = utc_now()
        cid = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=1, now=now)
        later = now + timedelta(days=10)
        self.store.extend(cid, 30, "STARS", now=later)
        c = self.store.get(cid)
        self.assertTrue(is_running_allowed(c, later + timedelta(days=29)))
        self.assertFalse(is_running_allowed(c, later + timedelta(days=31)))

    def test_same_payment_never_credited_twice(self):
        cid = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7)
        self.assertIsNotNone(self.store.extend(cid, 30, "STARS", 1500, "charge-1"))
        before = self.store.get(cid)["paid_until"]
        self.assertIsNone(self.store.extend(cid, 30, "STARS", 1500, "charge-1"))
        self.assertEqual(before, self.store.get(cid)["paid_until"])

    def test_suspended_never_runs(self):
        cid = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7)
        self.store.set_status(cid, STATUS_SUSPENDED)
        self.assertFalse(is_running_allowed(self.store.get(cid)))

    def test_token_unique(self):
        self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7)
        self.assertTrue(self.store.token_exists(TOKEN_A))
        with self.assertRaises(Exception):
            self.store.add(11, TOKEN_A, "a_bot", "-1002", "BETA", trial_days=7)

    def test_one_reminder_per_period(self):
        now = utc_now()
        cid = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7, now=now)
        self.assertFalse(needs_reminder(self.store.get(cid), 3, now))
        soon = now + timedelta(days=5)
        self.assertTrue(needs_reminder(self.store.get(cid), 3, soon))
        self.store.mark_reminded(cid, soon)
        self.assertFalse(needs_reminder(self.store.get(cid), 3, soon))
        self.store.extend(cid, 30, "STARS", now=soon)          # nouvelle période → nouveau rappel possible
        self.assertTrue(needs_reminder(self.store.get(cid), 3, soon + timedelta(days=30)))


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.store = ClientStore(os.path.join(self.data, "usine.db"))
        script = os.path.join(self.data, "fake_bot.py")
        with open(script, "w") as f:
            f.write(FAKE_BOT)
        self.sup = Supervisor(self.store, self.data, script)

    def tearDown(self):
        self.sup.stop_all()
        self.tmp.cleanup()

    def _env(self, cid):
        path = os.path.join(self.data, "clients", str(cid), "env.json")
        for _ in range(50):
            if os.path.exists(path) and os.path.getsize(path):
                with open(path) as f:
                    return json.load(f)
            time.sleep(0.1)
        self.fail("le robot client n'a pas démarré")

    def test_each_client_gets_own_process_db_and_settings(self):
        os.environ["USINE_BOT_TOKEN"] = "secret-usine"
        os.environ["API_KEY"] = "secret-vip"
        try:
            a = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7)
            b = self.store.add(20, TOKEN_B, "b_bot", "-1002", "BETA", trial_days=7)
            self.sup.sync()
            self.assertTrue(self.sup.is_running(a) and self.sup.is_running(b))
            self.assertNotEqual(self.sup.procs[a].pid, self.sup.procs[b].pid)
            ea, eb = self._env(a), self._env(b)
        finally:
            del os.environ["USINE_BOT_TOKEN"], os.environ["API_KEY"]
        self.assertEqual((ea["BOT_TOKEN"], ea["CHANNEL_ID"], ea["ADMIN_IDS"], ea["BRAND"]), (TOKEN_A, "-1001", "10", "ALPHA"))
        self.assertEqual((eb["BOT_TOKEN"], eb["CHANNEL_ID"], eb["ADMIN_IDS"], eb["BRAND"]), (TOKEN_B, "-1002", "20", "BETA"))
        self.assertNotEqual(ea["DB_PATH"], eb["DB_PATH"])
        self.assertIn(os.path.join("clients", str(a)), ea["DB_PATH"])
        # aucun secret de l'usine ou du canal VIP ne fuit vers les clients
        self.assertNotIn("USINE_BOT_TOKEN", ea)
        self.assertEqual(ea["API_KEY"], "")

    def test_expired_client_is_stopped_and_restarts_after_payment(self):
        now = utc_now()
        cid = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7, now=now - timedelta(days=6))
        self.sup.sync()
        self.assertTrue(self.sup.is_running(cid))
        # on simule la fin de l'essai
        with self.store._db() as c:
            c.execute("UPDATE clients SET paid_until=? WHERE id=?", ((now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), cid))
        events = self.sup.sync()
        self.assertFalse(self.sup.is_running(cid))
        self.assertEqual([e[0] for e in events], ["expired"])
        self.store.extend(cid, 30, "STARS")
        self.sup.sync()
        self.assertTrue(self.sup.is_running(cid))

    def test_suspend_and_reactivate(self):
        cid = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7)
        self.sup.sync()
        self.store.set_status(cid, STATUS_SUSPENDED)
        self.assertEqual(self.sup.sync(), [])          # pas de message « expiré » pour une suspension
        self.assertFalse(self.sup.is_running(cid))
        self.store.set_status(cid, STATUS_ACTIVE)
        self.sup.sync()
        self.assertTrue(self.sup.is_running(cid))

    def test_deleted_client_is_stopped(self):
        cid = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7)
        self.sup.sync()
        self.store.delete(cid)
        self.sup.sync()
        self.assertFalse(self.sup.is_running(cid))

    def test_crash_is_restarted_with_backoff(self):
        cid = self.store.add(10, TOKEN_A, "a_bot", "-1001", "ALPHA", trial_days=7)
        open(os.path.join(self.sup.client_dir(cid), "exit_now"), "w").close()
        self.sup.sync()
        self.sup.procs[cid].wait(10)
        self.sup.sync()                                # crash détecté → pas de relance immédiate
        self.assertEqual(self.sup.crashes[cid], 1)
        self.assertNotIn(cid, self.sup.procs)
        self.sup.retry_at[cid] = 0                     # on « avance le temps »
        self.sup.sync()
        self.assertIn(cid, self.sup.procs)


class RealBotTests(unittest.TestCase):
    """Lance le VRAI bot.py comme le fait l'usine (sans réseau : jeton volontairement invalide)."""

    def test_bot_py_starts_with_client_settings_and_own_brand(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ClientStore(os.path.join(tmp, "usine.db"))
            sup = Supervisor(store, tmp, os.path.join(HERE, "bot.py"))
            cid = store.add(10, "pas-un-jeton", "a_bot", "-1001", "GOLD SNIPER", trial_days=7)
            env = sup.client_env(store.get(cid))
            r = subprocess.run([sys.executable, os.path.join(HERE, "bot.py")], cwd=sup.client_dir(cid), env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("BOT_TOKEN mal formé", r.stderr)
            code = "import templates as T, bot; print(T.BRAND); print(T.WATERMARK_TEXT); print(bot.HELP.splitlines()[0])"
            r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.splitlines(), ["GOLD SNIPER", "GOLD SNIPER", "🤖 <b>Robot GOLD SNIPER</b>"])

    def test_vip_bot_brand_unchanged_without_brand_variable(self):
        env = {k: v for k, v in os.environ.items() if k != "BRAND"}
        r = subprocess.run([sys.executable, "-c", "import templates as T; print(T.BRAND); print(T.WATERMARK_TEXT)"],
                           cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.splitlines(), ["ANONYMETRADER VIP", "ANONYMETRADER VIP"])


if __name__ == "__main__":
    unittest.main()


class SiteTests(unittest.TestCase):
    def _get(self, port, path):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_page_is_filled_and_served(self):
        import re
        import usine
        page = usine.render_site("MaUsineBot").decode()
        self.assertEqual(re.findall(r"\{\{\w+\}\}", page), [])
        self.assertIn("https://t.me/MaUsineBot?start=site", page)
        srv = usine.start_site(0)                       # port libre choisi par le système
        try:
            port = srv.server_address[1]
            usine.SITE_STATE.update(bot_username="MaUsineBot", error=None)
            code, body = self._get(port, "/")
            self.assertEqual(code, 200)
            self.assertIn("https://t.me/MaUsineBot?start=site", body)
            code, body = self._get(port, "/health")
            self.assertEqual(code, 200)
            self.assertIn('"ok": true', body)
            self.assertEqual(self._get(port, "/nope")[0], 404)
            # erreur de configuration : la page reste en ligne et /health explique le problème
            usine.SITE_STATE.update(bot_username=None, error="❌ USINE_BOT_TOKEN manquant")
            self.assertEqual(self._get(port, "/")[0], 200)
            code, body = self._get(port, "/health")
            self.assertIn('"ok": false', body)
            self.assertIn("USINE_BOT_TOKEN manquant", body)
        finally:
            srv.shutdown()
            usine.SITE_STATE.update(bot_username=None, error=None)

    def test_page_answers_even_without_token(self):
        """Comme sur Railway : jeton absent → la page répond quand même (pas « l'application n'a pas répondu »)."""
        import socket
        import urllib.request
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k != "USINE_BOT_TOKEN"}
            env.update(PORT=str(port), DATA_DIR=tmp)
            p = subprocess.Popen([sys.executable, os.path.join(HERE, "usine.py")], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                            health = r.read().decode()
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("la page n'a jamais répondu")
                self.assertIn("USINE_BOT_TOKEN", health)
                self.assertIsNone(p.poll())             # le service reste debout
            finally:
                p.kill()
                p.wait()


class LauncherTests(unittest.TestCase):
    def _which(self, extra_env):
        """Lance le vrai start.py à côté de faux bot.py / usine.py qui affichent leur nom."""
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(os.path.join(HERE, "start.py"), tmp)
            for name in ("bot.py", "usine.py"):
                with open(os.path.join(tmp, name), "w") as f:
                    f.write(f"print('LANCÉ {name}')")
            env = {k: v for k, v in os.environ.items() if k != "USINE_BOT_TOKEN"}
            env.update(extra_env)
            r = subprocess.run([sys.executable, os.path.join(tmp, "start.py")], env=env,
                               capture_output=True, text=True, timeout=30)
        return r.stdout.strip().splitlines()[-1].replace("LANCÉ ", "")

    def test_vip_service_still_runs_bot(self):
        self.assertEqual(self._which({}), "bot.py")

    def test_factory_service_runs_usine(self):
        self.assertEqual(self._which({"USINE_BOT_TOKEN": "123:abc"}), "usine.py")


