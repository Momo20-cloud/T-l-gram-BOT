"""
USINE À ROBOTS — vends ton robot de signaux en abonnement
---------------------------------------------------------
Un robot « usine » (ton robot de vente) où chaque client :
  1. crée son propre robot avec @BotFather et colle le jeton,
  2. ajoute son robot comme admin de son canal,
  3. choisit son nom de marque,
→ l'usine lance une copie de bot.py rien que pour lui, avec sa propre base.
Essai gratuit, puis abonnement payé en Telegram Stars (ou prolongé à la main).
À l'expiration, son robot est mis en pause automatiquement ; il repart dès le paiement.

bot.py n'est pas modifié : ton canal VIP continue de tourner sur son propre service.
Lancement : python usine.py   (variables : voir .env.usine.example)
"""
import asyncio
import html
import json
import logging
import os
import re
import subprocess
import sys
import time as _time
from datetime import datetime

from dotenv import load_dotenv
from telegram import (Bot, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, MessageOriginChannel,
                      Update)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
                          MessageHandler, PreCheckoutQueryHandler, filters)

from templates import escape
from usine_db import STATUS_ACTIVE, STATUS_SUSPENDED, ClientStore, days_left, is_running_allowed, needs_reminder

load_dotenv()

# ---------------------------------------------------------------- réglages
USINE_TOKEN = os.getenv("USINE_BOT_TOKEN", "").strip().strip("'\"")
OWNER_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("OWNER_IDS", "")) if x.strip().isdigit()}
DATA_DIR = os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "usine_data"
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7") or 7)
SUB_DAYS = int(os.getenv("SUB_DAYS", "30") or 30)
PRICE_STARS = int(os.getenv("PRICE_STARS", "1500") or 0)        # 0 = paiement Stars désactivé
REMIND_DAYS = int(os.getenv("REMIND_DAYS", "3") or 3)
MAX_BOTS_PER_USER = int(os.getenv("MAX_BOTS_PER_USER", "1") or 1)
OPEN_SIGNUP = os.getenv("OPEN_SIGNUP", "true").lower() in ("1", "true", "oui", "yes")
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "").strip()
SITE_NAME = os.getenv("SITE_NAME", "").strip() or "Anonymetrader Signals"
PORT = int(os.getenv("PORT", "0") or 0)          # Railway le fournit : la page de vente est servie dessus
SITE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "index.html")
CLIENT_SCRIPT = os.getenv("CLIENT_SCRIPT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")

TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("usine")

# Variables transmises aux robots clients. Rien d'autre : jamais le jeton de l'usine ni tes secrets.
PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
                   "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
                   "RAILWAY_ENVIRONMENT", "RAILWAY_VOLUME_MOUNT_PATH")


# ================================================================ superviseur des robots clients
class Supervisor:
    """Lance / arrête / relance un processus bot.py par client selon son abonnement."""

    LOG_MAX_BYTES = 2_000_000

    def __init__(self, store: ClientStore, data_dir: str, script: str, python: str = sys.executable):
        self.store, self.data_dir, self.script, self.python = store, data_dir, script, python
        self.procs: dict[int, subprocess.Popen] = {}
        self.crashes: dict[int, int] = {}        # échecs consécutifs
        self.retry_at: dict[int, float] = {}     # pas de relance avant cet instant (time.monotonic)
        self.started_at: dict[int, float] = {}

    def client_dir(self, client_id: int) -> str:
        d = os.path.join(self.data_dir, "clients", str(client_id))
        os.makedirs(d, exist_ok=True)
        return d

    def client_env(self, client: dict) -> dict:
        env = {k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ}
        env.update({
            "BOT_TOKEN": client["bot_token"],
            "CHANNEL_ID": str(client["channel_id"]),
            "ADMIN_IDS": str(client["owner_id"]),
            "BRAND": client["brand"],
            "TIMEZONE": client.get("timezone") or "UTC",
            "DB_PATH": os.path.join(self.client_dir(client["id"]), "signals.db"),
            "API_KEY": "",                   # pas de serveur HTTP par client (évite les conflits de port)
            "QUICK_PAIRS": "XAUUSD,BTCUSD,EURUSD,GBPUSD",
            "DAILY_REPORT_TIME": "22:00",
            "WEEKLY_REPORT_DAY": "vendredi",
            "WATERMARK": "true",
            "PYTHONUNBUFFERED": "1",
        })
        return env

    def is_running(self, client_id: int) -> bool:
        p = self.procs.get(client_id)
        return p is not None and p.poll() is None

    def start(self, client: dict):
        cid = client["id"]
        folder = self.client_dir(cid)
        log_path = os.path.join(folder, "bot.log")
        if os.path.exists(log_path) and os.path.getsize(log_path) > self.LOG_MAX_BYTES:
            os.replace(log_path, log_path + ".1")
        with open(log_path, "ab") as out:
            self.procs[cid] = subprocess.Popen([self.python, self.script], cwd=folder, env=self.client_env(client),
                                               stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.started_at[cid] = _time.monotonic()
        log.info("▶️ Robot client #%s démarré (pid %s)", cid, self.procs[cid].pid)

    def stop(self, client_id: int, timeout: float = 15):
        p = self.procs.pop(client_id, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            log.info("⏸ Robot client #%s arrêté", client_id)

    def stop_all(self):
        for cid in list(self.procs):
            self.stop(cid)

    def sync(self) -> list[tuple[str, dict]]:
        """Aligne les processus sur la base. Retourne des événements (type, client) à notifier."""
        events = []
        now = _time.monotonic()
        clients = {c["id"]: c for c in self.store.all()}
        for cid in [c for c in self.procs if c not in clients]:      # client supprimé
            self.stop(cid)
        for cid, client in clients.items():
            allowed = is_running_allowed(client)
            p = self.procs.get(cid)
            if not allowed:
                if p is not None:
                    was_running = p.poll() is None
                    self.stop(cid)
                    self.crashes.pop(cid, None)
                    if was_running and client["status"] == STATUS_ACTIVE:
                        events.append(("expired", client))
                continue
            if p is not None and p.poll() is None:
                if now - self.started_at.get(cid, now) > 300:       # stable depuis 5 min : compteur remis à zéro
                    self.crashes.pop(cid, None)
                continue
            if p is not None:                                       # le processus s'est arrêté tout seul
                self.procs.pop(cid)
                n = self.crashes.get(cid, 0) + 1
                self.crashes[cid] = n
                self.retry_at[cid] = now + min(600, 10 * 2 ** (n - 1))
                log.warning("💥 Robot client #%s arrêté (code %s), échec n°%s", cid, p.returncode, n)
                if n == 3:
                    events.append(("crashing", client))
            if now >= self.retry_at.get(cid, 0):
                self.start(client)
        return events

    def tail_log(self, client_id: int, lines: int = 25) -> str:
        path = os.path.join(self.client_dir(client_id), "bot.log")
        if not os.path.exists(path):
            return "(pas encore de journal)"
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 20_000))
            text = f.read().decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])


STORE: ClientStore = None  # type: ignore[assignment]
SUP: Supervisor = None     # type: ignore[assignment]


# ================================================================ affichage
def fmt_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").strftime("%d/%m/%Y %H:%M UTC")


def client_line(c: dict, admin: bool = False) -> str:
    if c["status"] == STATUS_SUSPENDED:
        state = "⛔ suspendu"
    elif is_running_allowed(c):
        state = "🟢 actif" + (" (en marche)" if SUP and SUP.is_running(c["id"]) else " (démarrage…)")
    else:
        state = "⏸ expiré — en pause"
    left = days_left(c)
    line = (f"<b>#{c['id']}</b> @{escape(c['bot_username'] or '?')} — {escape(c['brand'])}\n"
            f"   {state} · jusqu'au {fmt_date(c['paid_until'])}"
            + (f" ({left:.0f} j restants)" if left > 0 else ""))
    if admin:
        line += f"\n   client <code>{c['owner_id']}</code> · canal <code>{escape(c['channel_id'])}</code>"
    return line


def pay_kb(client_id: int) -> InlineKeyboardMarkup | None:
    if PRICE_STARS <= 0:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"⭐ Payer {SUB_DAYS} jours — {PRICE_STARS} Stars", callback_data=f"pay:{client_id}")]])


async def notify_owners(bot, text: str):
    for uid in OWNER_IDS:
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            log.warning("Notification propriétaire impossible (%s) : %s", uid, e)


def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in OWNER_IDS)


# ================================================================ commandes client
PRICE_LINE = f"Ensuite : <b>{PRICE_STARS} ⭐ / {SUB_DAYS} jours</b>." if PRICE_STARS else ""
WELCOME = f"""🏭 <b>Crée ton robot de signaux Telegram</b>

Publie tes signaux de trading dans ton canal avec photo, suivi TP/SL, bilans automatiques et annonces économiques.

🎁 <b>{TRIAL_DAYS} jours d'essai gratuit</b>, sans carte bancaire.
{PRICE_LINE}

/creer — créer ton robot (3 minutes)
/monrobot — état de ton robot et abonnement
/abonner — prolonger ton abonnement
/paysupport — aide sur un paiement"""

OWNER_HELP = """
<b>Commandes propriétaire</b>
/clients — liste de tous les clients
/prolonger <code>ID JOURS</code> — ajouter des jours (paiement manuel)
/suspendre <code>ID</code> · /reactiver <code>ID</code>
/supprimer <code>ID</code> — arrêter et retirer un client (ses données restent sur le disque)
/journal <code>ID</code> — dernières lignes du journal de son robot"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = WELCOME + (OWNER_HELP if is_owner(update) else "")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_monrobot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mine = STORE.by_owner(update.effective_user.id)
    if not mine:
        await update.message.reply_text("Tu n'as pas encore de robot. Tape /creer pour commencer.")
        return
    for c in mine:
        await update.message.reply_text(client_line(c), parse_mode=ParseMode.HTML, reply_markup=pay_kb(c["id"]))


async def cmd_abonner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if PRICE_STARS <= 0:
        await update.message.reply_text("Le paiement en ligne n'est pas encore ouvert. "
                                        + (f"Contacte {SUPPORT_CONTACT}." if SUPPORT_CONTACT else ""))
        return
    mine = STORE.by_owner(update.effective_user.id)
    if not mine:
        await update.message.reply_text("Crée d'abord ton robot avec /creer.")
        return
    for c in mine:
        await send_invoice(context.bot, update.effective_chat.id, c)


async def send_invoice(bot, chat_id: int, c: dict):
    await bot.send_invoice(
        chat_id=chat_id,
        title=f"Abonnement {SUB_DAYS} jours",
        description=f"Robot @{c['bot_username']} ({c['brand']}) : {SUB_DAYS} jours de plus.",
        payload=f"sub:{c['id']}",
        currency="XTR",                 # Telegram Stars : pas de fournisseur de paiement à configurer
        prices=[LabeledPrice(f"{SUB_DAYS} jours", PRICE_STARS)],
    )


async def on_pay_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    c = STORE.get(int(q.data.split(":")[1]))
    if not c or c["owner_id"] != q.from_user.id:
        await q.answer("Robot introuvable.", show_alert=True)
        return
    await q.answer()
    await send_invoice(context.bot, q.message.chat.id, c)


async def on_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    m = re.fullmatch(r"sub:(\d+)", q.invoice_payload or "")
    c = STORE.get(int(m.group(1))) if m else None
    if not c or q.currency != "XTR" or q.total_amount != PRICE_STARS:
        await q.answer(ok=False, error_message="Ce paiement n'est plus valable. Tape /abonner pour recommencer.")
        return
    if c["status"] == STATUS_SUSPENDED:
        await q.answer(ok=False, error_message="Ce robot est suspendu. Contacte le support (/paysupport).")
        return
    await q.answer(ok=True)


async def on_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    m = re.fullmatch(r"sub:(\d+)", sp.invoice_payload or "")
    if not m:
        return
    cid = int(m.group(1))
    until = STORE.extend(cid, SUB_DAYS, "STARS", sp.total_amount, sp.telegram_payment_charge_id)
    if until is None:
        return   # paiement déjà crédité
    SUP.sync()
    c = STORE.get(cid)
    await update.message.reply_text(
        f"✅ Merci ! Abonnement prolongé jusqu'au <b>{fmt_date(until)}</b>.\n\n{client_line(c)}",
        parse_mode=ParseMode.HTML)
    await notify_owners(context.bot, f"💰 Paiement reçu : {sp.total_amount} ⭐\n{client_line(c, admin=True)}")


async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💬 Un souci avec un paiement ? "
        + (f"Écris à {SUPPORT_CONTACT} avec ton identifiant : " if SUPPORT_CONTACT else "Réponds ici avec ton identifiant : ")
        + f"<code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)
    if not SUPPORT_CONTACT:
        await notify_owners(context.bot, f"💬 Demande de support de <code>{update.effective_user.id}</code>")


# ================================================================ création d'un robot client
C_TOKEN, C_CHANNEL, C_BRAND = range(3)


async def creer_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not OPEN_SIGNUP and uid not in OWNER_IDS:
        await update.message.reply_text("Les inscriptions sont fermées pour le moment.")
        return ConversationHandler.END
    if len(STORE.by_owner(uid)) >= MAX_BOTS_PER_USER and uid not in OWNER_IDS:
        await update.message.reply_text("Tu as déjà ton robot. Tape /monrobot pour le voir.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "🧩 <b>Étape 1/3 — Ton robot</b>\n\n"
        "1. Ouvre @BotFather et envoie /newbot\n"
        "2. Choisis un nom et un identifiant (finissant par « bot »)\n"
        "3. Copie le jeton qu'il te donne (ex : <code>7412345678:AAH3k…</code>) et colle-le ici.\n\n"
        "/annuler pour arrêter.", parse_mode=ParseMode.HTML)
    return C_TOKEN


async def creer_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    m = TOKEN_RE.search(raw.replace(" ", ""))
    try:
        await update.message.delete()   # on ne laisse pas traîner le jeton dans la conversation
    except TelegramError:
        pass
    if not m:
        await update.effective_chat.send_message("❌ Ça ne ressemble pas à un jeton. Recopie-le depuis @BotFather.")
        return C_TOKEN
    token = m.group(0)
    if token == USINE_TOKEN or STORE.token_exists(token):
        await update.effective_chat.send_message("❌ Ce robot est déjà utilisé. Crée-en un nouveau avec /newbot.")
        return C_TOKEN
    try:
        async with Bot(token) as b:
            me = await b.get_me()
    except TelegramError:
        await update.effective_chat.send_message(
            "❌ Telegram refuse ce jeton. Dans @BotFather : /mybots → ton robot → API Token, puis recopie-le.")
        return C_TOKEN
    context.user_data.update(token=token, username=me.username, bot_id=me.id)
    await update.effective_chat.send_message(
        f"✅ Robot reconnu : @{escape(me.username)} (j'ai effacé ton message pour protéger le jeton).\n\n"
        "📢 <b>Étape 2/3 — Ton canal</b>\n\n"
        f"1. Ouvre ton canal → Administrateurs → Ajouter → @{escape(me.username)}\n"
        "2. Donne-lui le droit de <b>publier des messages</b> (et de modifier/supprimer)\n"
        "3. Puis <b>transfère-moi un message du canal</b> (ou envoie son ID qui commence par -100).",
        parse_mode=ParseMode.HTML)
    return C_CHANNEL


async def creer_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    origin = msg.forward_origin
    if isinstance(origin, MessageOriginChannel):
        channel = origin.chat.id
    elif msg.text and re.fullmatch(r"-100\d{5,}", msg.text.strip()):
        channel = int(msg.text.strip())
    elif msg.text and re.fullmatch(r"@[A-Za-z0-9_]{4,}", msg.text.strip()):
        channel = msg.text.strip()
    else:
        await msg.reply_text("Transfère-moi un message <b>du canal</b>, ou envoie son ID (-100…).",
                             parse_mode=ParseMode.HTML)
        return C_CHANNEL
    try:
        async with Bot(context.user_data["token"]) as b:
            chat = await b.get_chat(channel)
            member = await b.get_chat_member(chat.id, context.user_data["bot_id"])
    except TelegramError as e:
        await msg.reply_text(f"❌ Ton robot n'arrive pas à voir ce canal ({escape(e.message)}).\n"
                             "Vérifie qu'il est bien administrateur, puis renvoie le message.", parse_mode=ParseMode.HTML)
        return C_CHANNEL
    if member.status != "administrator" or not getattr(member, "can_post_messages", False):
        await msg.reply_text("❌ Ton robot est dans le canal mais n'a pas le droit de <b>publier</b>.\n"
                             "Modifie ses droits d'administrateur puis renvoie le message.", parse_mode=ParseMode.HTML)
        return C_CHANNEL
    context.user_data["channel"] = chat.id
    await msg.reply_text(
        f"✅ Canal « {escape(chat.title or chat.id)} » vérifié.\n\n"
        "🏷 <b>Étape 3/3 — Ta marque</b>\n\nQuel nom afficher sur tes signaux et en filigrane des photos ?\n"
        "(ex : <code>GOLD SNIPER VIP</code>)", parse_mode=ParseMode.HTML)
    return C_BRAND


async def creer_brand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    brand = re.sub(r"\s+", " ", update.message.text or "").strip()
    if not 2 <= len(brand) <= 40 or re.search(r"[<>&]", brand):
        await update.message.reply_text("Entre 2 et 40 caractères, sans < > &, s'il te plaît.")
        return C_BRAND
    d = context.user_data
    if STORE.token_exists(d["token"]):
        await update.message.reply_text("❌ Ce robot vient d'être enregistré. Tape /monrobot.")
        return ConversationHandler.END
    cid = STORE.add(update.effective_user.id, d["token"], d["username"], str(d["channel"]), brand, TRIAL_DAYS)
    d.clear()
    SUP.sync()
    c = STORE.get(cid)
    await update.message.reply_text(
        f"🎉 <b>C'est prêt !</b>\n\n{client_line(c)}\n\n"
        f"👉 Ouvre @{escape(c['bot_username'])} et tape /start : il te montre toutes ses commandes "
        "(/signal, /bilan, /news…).\n"
        f"🎁 Essai gratuit de {TRIAL_DAYS} jours. Je te préviens avant la fin.", parse_mode=ParseMode.HTML)
    await notify_owners(context.bot, f"🆕 Nouveau client\n{client_line(c, admin=True)}")
    return ConversationHandler.END


async def creer_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Création annulée. /creer pour recommencer.")
    return ConversationHandler.END


# ================================================================ commandes propriétaire
def _owner_arg_client(update: Update, context) -> dict | None:
    if not context.args or not context.args[0].lstrip("#").isdigit():
        return None
    return STORE.get(int(context.args[0].lstrip("#")))


async def cmd_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    rows = STORE.all()
    if not rows:
        await update.message.reply_text("Aucun client pour l'instant.")
        return
    active = sum(1 for c in rows if is_running_allowed(c))
    text = f"👥 <b>{len(rows)} clients</b> · {active} actifs\n\n" + "\n\n".join(client_line(c, True) for c in rows)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000], parse_mode=ParseMode.HTML)


async def cmd_prolonger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    c = _owner_arg_client(update, context)
    if not c or len(context.args) < 2 or not context.args[1].isdigit():
        await update.message.reply_text("Usage : /prolonger ID JOURS  (ex : /prolonger 3 30)")
        return
    until = STORE.extend(c["id"], int(context.args[1]), "MANUEL")
    SUP.sync()
    await update.message.reply_text(f"✅ #{c['id']} prolongé jusqu'au {fmt_date(until)}")
    try:
        await context.bot.send_message(c["owner_id"], f"✅ Ton abonnement est prolongé jusqu'au <b>{fmt_date(until)}</b>.",
                                       parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


async def _set_status(update, context, status, label):
    if not is_owner(update):
        return
    c = _owner_arg_client(update, context)
    if not c:
        await update.message.reply_text(f"Usage : /{label} ID")
        return
    STORE.set_status(c["id"], status)
    SUP.sync()
    await update.message.reply_text(client_line(STORE.get(c["id"]), True), parse_mode=ParseMode.HTML)


async def cmd_suspendre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_status(update, context, STATUS_SUSPENDED, "suspendre")


async def cmd_reactiver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_status(update, context, STATUS_ACTIVE, "reactiver")


async def cmd_supprimer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    c = _owner_arg_client(update, context)
    if not c:
        await update.message.reply_text("Usage : /supprimer ID")
        return
    STORE.delete(c["id"])
    SUP.sync()
    await update.message.reply_text(f"🗑 Client #{c['id']} retiré. Ses données restent dans {SUP.client_dir(c['id'])}")


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    c = _owner_arg_client(update, context)
    if not c:
        await update.message.reply_text("Usage : /journal ID")
        return
    await update.message.reply_text(f"<pre>{escape(SUP.tail_log(c['id'])[-3800:])}</pre>", parse_mode=ParseMode.HTML)


# ================================================================ tâche de fond
async def job_supervise(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    for kind, c in SUP.sync():
        if kind == "expired":
            try:
                await bot.send_message(
                    c["owner_id"], f"⏸ Ton robot @{escape(c['bot_username'])} est en pause : l'abonnement est terminé.\n"
                    "Tes signaux et ton historique sont conservés. Il repart dès le paiement.",
                    parse_mode=ParseMode.HTML, reply_markup=pay_kb(c["id"]))
            except TelegramError:
                pass
            await notify_owners(bot, f"⏸ Abonnement expiré\n{client_line(c, True)}")
        elif kind == "crashing":
            await notify_owners(bot, f"💥 Le robot du client #{c['id']} s'arrête en boucle. /journal {c['id']}")
    for c in STORE.all():
        if needs_reminder(c, REMIND_DAYS):
            try:
                await bot.send_message(
                    c["owner_id"], f"⏰ Ton robot @{escape(c['bot_username'])} s'arrête le <b>{fmt_date(c['paid_until'])}</b>.\n"
                    "Prolonge maintenant pour ne pas couper tes signaux :",
                    parse_mode=ParseMode.HTML, reply_markup=pay_kb(c["id"]))
            except TelegramError as e:
                log.warning("Rappel impossible pour #%s : %s", c["id"], e)
            STORE.mark_reminded(c["id"])


# ================================================================ page de vente (site web)
def render_site(bot_username: str | None) -> bytes:
    with open(SITE_FILE, encoding="utf-8") as f:
        page = f.read()
    link = f"https://t.me/{bot_username}?start=site" if bot_username else "#"
    support = SUPPORT_CONTACT or (f"@{bot_username}" if bot_username else "")
    values = {"SITE_NAME": SITE_NAME, "BOT_LINK": link, "TRIAL_DAYS": TRIAL_DAYS, "SUB_DAYS": SUB_DAYS,
              "PRICE_STARS_FMT": f"{PRICE_STARS:,}".replace(",", "\u202f"), "SUPPORT": support}
    for k, v in values.items():
        page = page.replace("{{" + k + "}}", html.escape(str(v), quote=True))
    return page.encode("utf-8")


def site_response(path: str, page: bytes) -> tuple[str, str, bytes]:
    """(statut, type, contenu) pour un chemin donné."""
    path = path.split("?", 1)[0]
    if path in ("/", "/index.html"):
        return "200 OK", "text/html; charset=utf-8", page
    if path == "/health":
        body = {"ok": True, "clients_en_marche": len(SUP.procs) if SUP else 0}
        return "200 OK", "application/json", json.dumps(body).encode()
    if path == "/robots.txt":
        return "200 OK", "text/plain", b"User-agent: *\nAllow: /\n"
    return "404 Not Found", "text/plain; charset=utf-8", "Page introuvable".encode()


async def start_site(app: Application, bot_username: str | None):
    page = render_site(bot_username)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            while (await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
                pass
            parts = line.split()
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("GET", "/")
            status, ctype, body = site_response(path, page)
            head = (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                    "Cache-Control: public, max-age=300\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n")
            writer.write(head.encode() + (b"" if method == "HEAD" else body))
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    app.bot_data["site"] = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log.info("🌐 Page de vente en ligne sur le port %s", PORT)


async def post_init(app: Application):
    try:
        await app.bot.set_my_commands([("creer", "Créer mon robot"), ("monrobot", "Mon robot et mon abonnement"),
                                       ("abonner", "Prolonger mon abonnement"), ("paysupport", "Aide paiement")])
    except TelegramError as e:
        log.warning("Menu des commandes non mis à jour : %s", e)
    if PORT:
        await start_site(app, app.bot.username)
    SUP.sync()
    log.info("🏭 Usine démarrée — %s robots clients en marche", len(SUP.procs))


async def post_shutdown(app: Application):
    srv = app.bot_data.get("site")
    if srv:
        srv.close()
    SUP.stop_all()


# ================================================================ démarrage
def main():
    global STORE, SUP
    if not TOKEN_RE.fullmatch(USINE_TOKEN):
        raise SystemExit("❌ USINE_BOT_TOKEN manquant ou mal formé (jeton de TON robot de vente, depuis @BotFather).")
    if not OWNER_IDS:
        log.warning("OWNER_IDS vide : tu ne recevras pas les notifications ni les commandes propriétaire.")
    os.makedirs(DATA_DIR, exist_ok=True)
    STORE = ClientStore(os.path.join(DATA_DIR, "usine.db"))
    SUP = Supervisor(STORE, DATA_DIR, CLIENT_SCRIPT)
    log.info("Données : %s", os.path.abspath(DATA_DIR))

    app = Application.builder().token(USINE_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    private = filters.ChatType.PRIVATE
    txt = filters.TEXT & ~filters.COMMAND
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("creer", creer_start, filters=private)],
        states={
            C_TOKEN: [MessageHandler(txt, creer_token)],
            C_CHANNEL: [MessageHandler((txt | filters.FORWARDED) & ~filters.COMMAND, creer_channel)],
            C_BRAND: [MessageHandler(txt, creer_brand)],
        },
        fallbacks=[CommandHandler("annuler", creer_cancel)],
        conversation_timeout=20 * 60,
    ))
    for name, fn in (("start", cmd_start), ("aide", cmd_start), ("monrobot", cmd_monrobot), ("abonner", cmd_abonner),
                     ("paysupport", cmd_paysupport), ("clients", cmd_clients), ("prolonger", cmd_prolonger),
                     ("suspendre", cmd_suspendre), ("reactiver", cmd_reactiver), ("supprimer", cmd_supprimer),
                     ("journal", cmd_journal)):
        app.add_handler(CommandHandler(name, fn, filters=private))
    app.add_handler(CallbackQueryHandler(on_pay_button, pattern=r"^pay:\d+$"))
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_paid))
    app.job_queue.run_repeating(job_supervise, interval=30, first=30, name="supervise")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
