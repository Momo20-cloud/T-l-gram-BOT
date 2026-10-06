"""
ANONYMETRADER VIP — Version Multi-Tenancy
------------------------------------------
Adapté pour supporter N clients avec isolation automatique.

Changements clés :
1. Importe db_tenant au lieu de sqlite3
2. Ajoute set_client_context() au démarrage
3. Remplace db() par tenant_db()
4. Ajoute @subscription_only pour protéger les commandes

Le reste du code fonctionne IDENTIQUEMENT.
"""
import hmac
from templates import escape
import io
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, time, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageOriginChannel,
    ReplyParameters,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import Conflict, InvalidToken, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ============ CHANGEMENT #1 : Importer db_tenant ============
from db_tenant import set_client_context, tenant_db
from db_schema_v2 import ClientManager

import templates as T

load_dotenv()

# ================================================================ réglages
BOT_TOKEN = re.sub(r"^\s*BOT_TOKEN\s*=\s*", "", os.getenv("BOT_TOKEN", "")).strip().strip("'\"<> ").replace(" ", "")
_ch = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_ID = int(_ch) if re.fullmatch(r"-?\d+", _ch or "x") else _ch
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.strip().isdigit()}
TZ = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
DB_PATH = os.getenv("DB_PATH", "signals.db")
QUICK_PAIRS = [p.strip().upper() for p in os.getenv("QUICK_PAIRS", "XAUUSD,BTCUSD,EURUSD,GBPUSD").split(",") if p.strip()]
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "22:00").strip()
WEEKLY_REPORT_DAY = os.getenv("WEEKLY_REPORT_DAY", "vendredi").strip().lower()
WATERMARK = os.getenv("WATERMARK", "true").lower() in ("1", "true", "oui", "yes")

# ============ CHANGEMENT #2 : CLIENT_ID depuis environment ============
CLIENT_ID = None  # Défini au démarrage de main()

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("anonymetrader")

PAIR, DIRECTION, ORDER_TYPE, ENTRY, SL, TPS, EXPIRY, PHOTO, NOTE, CONFIRM = range(10)
PENDING_TYPES = ("LIMIT", "STOP")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
QUICK_RE = re.compile(r"^[A-Z]{3,6}\s+(BUY|SELL)", re.MULTILINE)


# ============ CHANGEMENT #3 : db() → tenant_db() ============
# ================================================================ base de données
def init_db():
    """Initialiser la base pour ce client (CLIENT_ID)."""
    global DB_PATH, CLIENT_ID

    folder = os.path.dirname(os.path.abspath(DB_PATH))
    try:
        os.makedirs(folder, exist_ok=True)
        sqlite3.connect(DB_PATH).close()
    except Exception as e:
        log.warning("⚠️ Impossible d'utiliser %s (%s). Base locale 'signals.db' utilisée à la place", DB_PATH, e)
        DB_PATH = "signals.db"

    if os.getenv("RAILWAY_ENVIRONMENT") and not os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
        log.warning("⚠️ Aucun volume Railway détecté : l'historique sera perdu à chaque redéploiement.")

    log.info("Base de données : %s (client_id=%s)", os.path.abspath(DB_PATH), CLIENT_ID)

    with tenant_db(DB_PATH) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS signals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER,
                created_at TEXT, pair TEXT, direction TEXT,
                entry REAL, entry_text TEXT, sl REAL, sl_text TEXT,
                tps TEXT, tps_text TEXT, note TEXT, photo TEXT,
                msg_id INTEGER, status TEXT DEFAULT 'OPEN',
                tp_hit INTEGER DEFAULT 0, be INTEGER DEFAULT 0,
                outcome TEXT, result_pips REAL, closed_at TEXT,
                ref TEXT, source TEXT DEFAULT 'manuel',
                order_type TEXT DEFAULT 'MARKET', expires_at TEXT,
                closed_pct REAL DEFAULT 0, realized_pips REAL DEFAULT 0)"""
        )
        c.commit()


def get_signal(sig_id: int):
    """Récupérer un signal (automatiquement isolé par client_id)."""
    with tenant_db(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM signals WHERE id=?", (sig_id,)).fetchone()


def update_signal(sig_id: int, **fields):
    """Mettre à jour un signal (automatiquement isolé par client_id)."""
    keys = ", ".join(f"{k}=?" for k in fields)
    with tenant_db(DB_PATH) as c:
        c.execute(f"UPDATE signals SET {keys} WHERE id=?", (*fields.values(), sig_id))
        c.commit()


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ================================================================ calculs
def pip_size(pair: str) -> float:
    p = pair.upper()
    for k, v in T.PIP_SIZES.items():
        if p.startswith(k):
            return v
    return T.JPY_PIP if "JPY" in p else T.DEFAULT_PIP


def calc_pips(pair: str, direction: str, entry: float, price: float) -> float:
    diff = (price - entry) if direction == "BUY" else (entry - price)
    return round(diff / pip_size(pair), 1)


def fmt_num(v: float) -> str:
    return (f"{v:.8f}").rstrip("0").rstrip(".")


def parse_nums(text: str):
    return [(m.replace(",", "."), float(m.replace(",", "."))) for m in NUM_RE.findall(text or "")]


def local_date(utc_str: str | None = None) -> str:
    dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if utc_str else datetime.now(timezone.utc)
    dt = dt.astimezone(TZ)
    off = int(dt.utcoffset().total_seconds() // 3600)
    return dt.strftime("%d/%m/%Y • %H:%M ") + ("GMT" if off == 0 else f"GMT{off:+d}")


def parse_expiry(text: str) -> str | None:
    """'2h', '90min', '18:00', '25/09 18:00' -> UTC 'YYYY-MM-DD HH:MM:SS'"""
    t = (text or "").strip().lower().replace(" ", "")
    now = datetime.now(TZ)
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)(h|heures?|m|min|minutes?)", t)
    if m:
        v = float(m.group(1).replace(",", "."))
        dt = now + (timedelta(hours=v) if m.group(2).startswith("h") else timedelta(minutes=v))
    else:
        m = re.fullmatch(r"(?:(\d{1,2})/(\d{1,2}))?(\d{1,2})[:h](\d{2})", t)
        if not m:
            return None
        dd, mm, hh, mi = m.groups()
        try:
            dt = now.replace(hour=int(hh), minute=int(mi), second=0, microsecond=0)
            if dd:
                dt = dt.replace(month=int(mm), day=int(dd))
                if dt < now:
                    dt = dt.replace(year=dt.year + 1)
            elif dt <= now:
                dt += timedelta(days=1)
        except ValueError:
            return None
    if dt <= now:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def signal_view(row_or_dict) -> dict:
    """Prépare les données pour templates.signal_post"""
    s = dict(row_or_dict)
    tps = json.loads(s["tps"]) if isinstance(s["tps"], str) else s["tps"]
    tps_text = json.loads(s["tps_text"]) if isinstance(s["tps_text"], str) else s["tps_text"]
    sl_pips = calc_pips(s["pair"], s["direction"], s["entry"], s["sl"])
    tp_list = [{"text": t, "pips": calc_pips(s["pair"], s["direction"], s["entry"], v)} for t, v in zip(tps_text, tps)]
    rr = round(tp_list[0]["pips"] / abs(sl_pips), 2) if sl_pips else None
    return {**s, "tps": tp_list, "sl_pips": sl_pips, "rr": rr, "date": local_date(s.get("created_at")),
            "order_type": s.get("order_type") or "MARKET",
            "expires": local_date(s["expires_at"]) if s.get("expires_at") else None}


# ================================================================ photo + filigrane
async def branded_photo(bot, file_id, label: str):
    """Ajoute un bandeau de marque en bas de la photo."""
    if not WATERMARK:
        return file_id
    try:
        from PIL import Image, ImageDraw, ImageFont

        if isinstance(file_id, (bytes, bytearray)):
            raw = bytes(file_id)
        else:
            f = await bot.get_file(file_id)
            raw = await f.download_as_bytearray()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        band = max(36, h // 14)
        canvas = Image.new("RGB", (w, h + band), (12, 14, 20))
        canvas.paste(img, (0, 0))
        d = ImageDraw.Draw(canvas)
        size = int(band * 0.45)
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
        d.rectangle([0, h, w, h + 3], fill=(212, 175, 55))
        d.text((band * 0.4, h + band / 2 + 1), T.WATERMARK_TEXT, font=font, fill=(212, 175, 55), anchor="lm")
        d.text((w - band * 0.4, h + band / 2 + 1), label, font=font, fill=(235, 235, 235), anchor="rm")
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=92)
        out.seek(0)
        return out
    except Exception as e:
        log.warning("Filigrane impossible : %s", e)
        return file_id


# ================================================================ sécurité
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        user = update.effective_user
        if not user or user.id not in ADMIN_IDS:
            log.warning("Accès refusé à l'utilisateur %s", user.id if user else "?")
            if update.callback_query:
                await update.callback_query.answer("⛔ Accès réservé", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    f"⛔ Ce robot est privé.\nTon identifiant : <code>{user.id if user else '?'}</code>",
                    parse_mode=ParseMode.HTML)
            return ConversationHandler.END
        return await func(update, context, *a, **kw)
    return wrapper


# ============ CHANGEMENT #4 : @subscription_only decorator ============
def check_subscription(user_id: int) -> bool:
    """Vérifier que l'abonnement du client est actif."""
    if not CLIENT_ID:
        return True  # Mode dev sans client_id

    manager = ClientManager(DB_PATH)
    status = manager.check_subscription(CLIENT_ID)

    if not status["active"]:
        log.warning(f"❌ Client #{CLIENT_ID} abonnement expiré ({status['status']})")
        return False

    return True


def subscription_only(func):
    """Décorateur : vérifier l'abonnement avant chaque commande."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_subscription(update.effective_user.id):
            await update.message.reply_text(
                "❌ <b>Abonnement expiré</b>\n\n"
                "Ton abonnement a expiré. Contacte @AnonymetraderFactory pour renouveler.",
                parse_mode=ParseMode.HTML
            )
            return
        return await func(update, context)
    return wrapper


# ================================================================ commandes simples
HELP = """🤖 <b>Robot ANONYMETRADER VIP</b>

<b>Signaux</b>
/signal — créer et publier un nouveau signal
/ouverts — signaux en cours
/cloture <code>ID PRIX</code> — clôture manuelle

<b>Bilans</b>
/bilan — aujourd'hui · /bilan hier
/bilan semaine · /bilan mois · /bilan mois dernier

<b>Annonces économiques</b>
/news — annonce + rappel avant + chiffre réel

<b>Canal</b>
/accueil — publie et épingle le message d'accueil
/verifier — vérifie que le robot peut publier
/id — affiche ton identifiant Telegram
/annuler — annule la saisie en cours"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ADMIN_IDS:
        await update.message.reply_text(
            f"👋 Configuration initiale.\nTon identifiant est : <code>{uid}</code>",
            parse_mode=ParseMode.HTML)
        return
    if uid not in ADMIN_IDS:
        await update.message.reply_text(
            f"⛔ Ce robot est privé.\nTon identifiant : <code>{uid}</code>",
            parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Ton identifiant : <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)


async def forwarded_from_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    origin = update.message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        await update.message.reply_text(
            f"📡 Canal : <b>{origin.chat.title}</b>\nID : <code>{origin.chat.id}</code>",
            parse_mode=ParseMode.HTML)


@admin_only
async def cmd_verifier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = await context.bot.get_chat(CHANNEL_ID)
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(CHANNEL_ID, me.id)
        can_post = getattr(member, "can_post_messages", False)
        can_edit = getattr(member, "can_edit_messages", False)
        await update.message.reply_text(
            f"✅ Canal trouvé : <b>{chat.title}</b>\n"
            f"Statut : {member.status}\n"
            f"Publier : {'✅' if can_post else '❌'}  |  Modifier : {'✅' if can_edit else '❌'}",
            parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Problème d'accès au canal : {e}")


@admin_only
async def cmd_accueil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await context.bot.send_message(CHANNEL_ID, T.WELCOME, parse_mode=ParseMode.HTML)
    try:
        await context.bot.pin_chat_message(CHANNEL_ID, msg.message_id, disable_notification=True)
        await update.message.reply_text("✅ Message d'accueil publié et épinglé.")
    except Exception as e:
        await update.message.reply_text(f"✅ Publié, mais épinglage impossible : {e}")


# ================================================================ création d'un signal
@admin_only
@subscription_only
async def sig_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sig"] = {}
    context.user_data["_flow"] = True
    rows = [[InlineKeyboardButton(p, callback_data=f"pair:{p}") for p in QUICK_PAIRS[i:i + 2]]
            for i in range(0, len(QUICK_PAIRS), 2)]
    await update.message.reply_text(
        "📊 <b>Nouveau signal</b>\n\n"
        "⚡ Mode rapide (ex) : <code>XAUUSD BUY 2650 SL 2645 TP 2655 2660</code>\n\n"
        "🐢 Pas à pas : choisis l'actif ou tape-le (ex : USDJPY) :",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    return PAIR


async def _ask_direction(msg, pair):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🟢 BUY", callback_data="dir:BUY"),
                                InlineKeyboardButton("🔴 SELL", callback_data="dir:SELL")]])
    await msg.reply_text(f"✅ {pair}\n\n2️⃣ Direction ?", reply_markup=kb)


async def sig_pair_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pair = q.data.split(":", 1)[1]
    context.user_data["sig"]["pair"] = pair
    await q.edit_message_reply_markup(None)
    await _ask_direction(q.message, pair)
    return DIRECTION


async def sig_pair_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if QUICK_RE.search(update.message.text or ""):
        return await quick_signal(update, context)
    pair = re.sub(r"[^A-Z0-9./]", "", update.message.text.upper())[:15]
    if not pair:
        await update.message.reply_text("Tape un symbole valide, ex : XAUUSD")
        return PAIR
    context.user_data["sig"]["pair"] = pair
    await _ask_direction(update.message, pair)
    return DIRECTION


async def sig_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data.split(":", 1)[1]
    context.user_data["sig"]["direction"] = d
    await q.edit_message_reply_markup(None)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Au marché", callback_data="ot:MARKET")],
        [InlineKeyboardButton(f"📥 {d} LIMIT", callback_data="ot:LIMIT"),
         InlineKeyboardButton(f"🚀 {d} STOP", callback_data="ot:STOP")],
    ])
    await q.message.reply_text(
        f"✅ {'🟢 BUY' if d == 'BUY' else '🔴 SELL'}\n\n3️⃣ Type d'ordre ?",
        parse_mode=ParseMode.HTML, reply_markup=kb)
    return ORDER_TYPE


async def sig_order_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ot = q.data.split(":", 1)[1]
    s = context.user_data["sig"]
    s["order_type"] = ot
    await q.edit_message_reply_markup(None)
    label = "au marché" if ot == "MARKET" else f"{s['direction']} {ot}"
    question = "Prix d'entrée ?" if ot == "MARKET" else "Prix de l'ordre ?"
    await q.message.reply_text(
        f"✅ {label}\n\n4️⃣ {question}",
        parse_mode=ParseMode.HTML)
    return ENTRY


async def sig_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nums = parse_nums(update.message.text)
    if not nums or len(nums) > 2:
        await update.message.reply_text("❗ Envoie un prix (ex : 2650.5) ou une zone (ex : 2650 - 2652)")
        return ENTRY
    s = context.user_data["sig"]
    if len(nums) == 2:
        lo, hi = sorted(nums, key=lambda x: x[1])
        s["entry"] = (lo[1] + hi[1]) / 2
        s["entry_text"] = f"{lo[0]} – {hi[0]}"
    else:
        s["entry"], s["entry_text"] = nums[0][1], nums[0][0]
    await update.message.reply_text("✅ Entrée notée.\n\n5️⃣ Stop Loss ?")
    return SL


async def sig_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nums = parse_nums(update.message.text)
    s = context.user_data["sig"]
    if len(nums) != 1:
        await update.message.reply_text("❗ Envoie un seul prix pour le Stop Loss.")
        return SL
    sl = nums[0][1]
    if (s["direction"] == "BUY" and sl >= s["entry"]) or (s["direction"] == "SELL" and sl <= s["entry"]):
        side = "en dessous" if s["direction"] == "BUY" else "au-dessus"
        await update.message.reply_text(f"❗ Pour un {s['direction']}, le SL doit être {side} de l'entrée.")
        return SL
    s["sl"], s["sl_text"] = sl, nums[0][0]
    await update.message.reply_text(
        "✅ SL noté.\n\n6️⃣ Objectifs (TP) ? Ex : <code>2655 2660 2670</code>",
        parse_mode=ParseMode.HTML)
    return TPS


async def sig_tps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nums = parse_nums(update.message.text)
    if not nums:
        await update.message.reply_text("❗ Envoie au moins un prix de TP.")
        return TPS
    s = context.user_data["sig"]
    s["tps"] = [n[1] for n in nums]
    s["tps_text"] = [n[0] for n in nums]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️  Passer", callback_data="exp:none")],
        [InlineKeyboardButton("⏱️  Spécifier", callback_data="exp:yes")],
    ])
    await update.message.reply_text("✅ TP notés.\n\n7️⃣ Expiration du signal ?\n<i>(ex : 2h, 18:00, ou 25/09 18:00)</i>",
                                   parse_mode=ParseMode.HTML, reply_markup=kb)
    return EXPIRY


async def sig_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = context.user_data["sig"]

    if q.data == "exp:none":
        s["expires_at"] = None
        await q.edit_message_reply_markup(None)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️  Passer", callback_data="ph:no"),
             InlineKeyboardButton("📸 Ajouter", callback_data="ph:yes")],
        ])
        await q.message.reply_text("✅ Pas d'expiration.\n\n8️⃣ Photo ?", reply_markup=kb)
        return PHOTO
    else:
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("⏱️  Envoie l'expiration (ex : 2h, 18:00)")
        return EXPIRY


async def sig_expiry_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expires_at = parse_expiry(update.message.text)
    if not expires_at:
        await update.message.reply_text("❗ Format non compris. Essaie : 2h, 18:00 ou 25/09 18:00")
        return EXPIRY
    context.user_data["sig"]["expires_at"] = expires_at
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️  Passer", callback_data="ph:no"),
         InlineKeyboardButton("📸 Ajouter", callback_data="ph:yes")],
    ])
    await update.message.reply_text("✅ Expiration notée.\n\n8️⃣ Photo ?", reply_markup=kb)
    return PHOTO


async def sig_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = context.user_data["sig"]

    if q.data == "ph:no":
        s["photo"] = None
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("✅ Pas de photo.\n\n9️⃣ Note/analyse ? (optionnel)")
        return NOTE
    else:
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("📸 Envoie une photo (optionnel)")
        return PHOTO


async def sig_photo_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        context.user_data["sig"]["photo"] = update.message.photo[-1].file_id
    await update.message.reply_text("✅ Photo reçue.\n\n9️⃣ Note/analyse ? (optionnel)")
    return NOTE


async def sig_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.lower() not in ("/annuler", "/passer"):
        context.user_data["sig"]["note"] = update.message.text[:400]
    else:
        context.user_data["sig"]["note"] = None

    s = context.user_data["sig"]
    preview = f"📊 {s['pair']} {s['direction']} @ {s.get('entry_text', s['entry'])}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Publier", callback_data="sig:publish"),
         InlineKeyboardButton("❌ Annuler", callback_data="sig:cancel")],
    ])
    await update.message.reply_text(f"{preview}\n\n✋ Valider ?", reply_markup=kb)
    return CONFIRM


async def sig_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(None)

    if q.data == "sig:cancel":
        await q.message.reply_text("🗑️  Signal annulé")
        return ConversationHandler.END

    s = context.user_data.pop("sig", {})

    # ============ CHANGEMENT #5 : Utiliser tenant_db() ============
    with tenant_db(DB_PATH) as c:
        c.execute(
            """INSERT INTO signals(
                client_id, pair, direction, entry, entry_text, sl, sl_text,
                tps, tps_text, note, photo, status, created_at, order_type, expires_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                CLIENT_ID,  # Isolé automatiquement
                s["pair"], s["direction"],
                s["entry"], s["entry_text"],
                s["sl"], s["sl_text"],
                json.dumps(s["tps"]), json.dumps(s["tps_text"]),
                s.get("note"), s.get("photo"),
                "OPEN", utc_now_str(),
                s.get("order_type", "MARKET"),
                s.get("expires_at")
            )
        )
        c.commit()
        sig_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    await q.message.reply_text(f"✅ Signal #{sig_id} publié sur le canal.")
    return ConversationHandler.END


@subscription_only
async def cmd_ouverts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lister les signaux ouverts du client (automatiquement isolé)."""
    with tenant_db(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM signals WHERE status='OPEN' ORDER BY created_at DESC LIMIT 10",
            # Automatiquement: WHERE client_id = CLIENT_ID
        ).fetchall()

    if not rows:
        await update.message.reply_text("📭 Aucun signal ouvert")
        return

    for r in rows:
        view = signal_view(r)
        await update.message.reply_text(
            f"📊 #{r['id']} {r['pair']} {r['direction']} @ {view['date']}"
        )


# ================================================================ démarrage
async def main():
    """Démarrer le bot client."""
    global CLIENT_ID

    # ============ CHANGEMENT #2 : Définir CLIENT_ID ============
    CLIENT_ID = int(os.getenv("CLIENT_ID", "1"))  # Par défaut client 1 en dev

    # Vérifier que le client existe
    manager = ClientManager(DB_PATH)
    client = manager.get_client(CLIENT_ID)
    if not client:
        raise ValueError(f"Client #{CLIENT_ID} introuvable")

    log.info(f"🚀 Démarrage bot client #{CLIENT_ID} (@{client['bot_username']})")

    # ============ CHANGEMENT #3 : set_client_context ============
    set_client_context(CLIENT_ID, DB_PATH)

    # Initialiser la base
    init_db()

    # Créer l'app
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler pour /signal
    signal_conv = ConversationHandler(
        entry_points=[CommandHandler("signal", sig_start)],
        states={
            PAIR: [
                CallbackQueryHandler(sig_pair_cb, pattern="^pair:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sig_pair_text),
            ],
            DIRECTION: [CallbackQueryHandler(sig_direction, pattern="^dir:")],
            ORDER_TYPE: [CallbackQueryHandler(sig_order_type, pattern="^ot:")],
            ENTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_entry)],
            SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_sl)],
            TPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_tps)],
            EXPIRY: [
                CallbackQueryHandler(sig_expiry, pattern="^exp:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sig_expiry_text),
            ],
            PHOTO: [
                CallbackQueryHandler(sig_photo, pattern="^ph:"),
                MessageHandler(filters.PHOTO, sig_photo_recv),
            ],
            NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_note)],
            CONFIRM: [CallbackQueryHandler(sig_confirm, pattern="^sig:")],
        },
        fallbacks=[CommandHandler("annuler", lambda u, c: ConversationHandler.END)],
    )

    app.add_handler(signal_conv)
    app.add_handler(CommandHandler("signal", sig_start))
    app.add_handler(CommandHandler("ouverts", cmd_ouverts))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("verifier", cmd_verifier))
    app.add_handler(CommandHandler("accueil", cmd_accueil))
    app.add_handler(MessageHandler(filters.Filters.forwarded_from, forwarded_from_channel))

    async with app:
        await app.initialize()
        await app.start()
        log.info(f"✅ Bot client #{CLIENT_ID} en écoute")

        import asyncio
        await asyncio.Event().wait()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
