"""
ANONYMETRADER VIP — Robot de publication de signaux Telegram
--------------------------------------------------------------
Tu parles au robot en privé (/signal), il te pose les questions,
te montre un aperçu, puis publie le signal dans ton canal VIP
avec la photo et le modèle défini dans templates.py.
"""
import hmac
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

import templates as T

load_dotenv()

# ---------------------------------------------------------------- réglages
# Nettoie les erreurs de copier-coller fréquentes : espaces, guillemets, "BOT_TOKEN=" collé dans la valeur
BOT_TOKEN = re.sub(r"^\s*BOT_TOKEN\s*=\s*", "", os.getenv("BOT_TOKEN", "")).strip().strip("'\"<> ").replace(" ", "")
_ch = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_ID = int(_ch) if re.fullmatch(r"-?\d+", _ch or "x") else _ch
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.strip().isdigit()}
TZ = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
DB_PATH = os.getenv("DB_PATH", "signals.db")
QUICK_PAIRS = [p.strip().upper() for p in os.getenv("QUICK_PAIRS", "XAUUSD,BTCUSD,EURUSD,GBPUSD").split(",") if p.strip()]
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "22:00").strip()   # vide = désactivé
WEEKLY_REPORT_DAY = os.getenv("WEEKLY_REPORT_DAY", "vendredi").strip().lower()
WATERMARK = os.getenv("WATERMARK", "true").lower() in ("1", "true", "oui", "yes")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("anonymetrader")

PAIR, DIRECTION, ORDER_TYPE, ENTRY, SL, TPS, EXPIRY, PHOTO, NOTE, CONFIRM = range(10)
PENDING_TYPES = ("LIMIT", "STOP")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


# ================================================================ base de données
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    global DB_PATH
    folder = os.path.dirname(os.path.abspath(DB_PATH))
    try:
        os.makedirs(folder, exist_ok=True)
        sqlite3.connect(DB_PATH).close()
    except Exception as e:
        log.warning("⚠️ Impossible d'utiliser %s (%s). Base locale 'signals.db' utilisée à la place : "
                    "l'historique sera PERDU à chaque redéploiement. Ajoute un volume monté sur %s.",
                    DB_PATH, e, folder)
        DB_PATH = "signals.db"
    if os.getenv("RAILWAY_ENVIRONMENT") and not os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
        log.warning("⚠️ Aucun volume Railway détecté : l'historique sera perdu à chaque redéploiement.")
    log.info("Base de données : %s", os.path.abspath(DB_PATH))
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS signals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT, pair TEXT, direction TEXT,
                entry REAL, entry_text TEXT, sl REAL, sl_text TEXT,
                tps TEXT, tps_text TEXT, note TEXT, photo TEXT,
                msg_id INTEGER, status TEXT DEFAULT 'OPEN',
                tp_hit INTEGER DEFAULT 0, be INTEGER DEFAULT 0,
                outcome TEXT, result_pips REAL, closed_at TEXT)"""
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(signals)")}
        if "ref" not in cols:
            c.execute("ALTER TABLE signals ADD COLUMN ref TEXT")
        if "source" not in cols:
            c.execute("ALTER TABLE signals ADD COLUMN source TEXT DEFAULT 'manuel'")
        if "order_type" not in cols:
            c.execute("ALTER TABLE signals ADD COLUMN order_type TEXT DEFAULT 'MARKET'")
        if "expires_at" not in cols:
            c.execute("ALTER TABLE signals ADD COLUMN expires_at TEXT")


def get_signal(sig_id: int):
    with db() as c:
        return c.execute("SELECT * FROM signals WHERE id=?", (sig_id,)).fetchone()


def update_signal(sig_id: int, **fields):
    keys = ", ".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE signals SET {keys} WHERE id=?", (*fields.values(), sig_id))


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
    """'2h', '90min', '18:00', '25/09 18:00' -> date UTC 'YYYY-MM-DD HH:MM:SS' (None si illisible)."""
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
    """Ajoute un bandeau de marque en bas de la photo. `file_id` = identifiant Telegram ou octets bruts."""
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
        d.rectangle([0, h, w, h + 3], fill=(212, 175, 55))  # liseré doré
        d.text((band * 0.4, h + band / 2 + 1), T.WATERMARK_TEXT, font=font, fill=(212, 175, 55), anchor="lm")
        d.text((w - band * 0.4, h + band / 2 + 1), label, font=font, fill=(235, 235, 235), anchor="rm")
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=92)
        out.seek(0)
        return out
    except Exception as e:  # en cas de souci, on envoie la photo d'origine
        log.warning("Filigrane impossible : %s", e)
        return file_id


# ================================================================ sécurité
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        user = update.effective_user
        if not user or user.id not in ADMIN_IDS:
            log.warning("Accès refusé à l'utilisateur %s (admins autorisés : %s)", user.id if user else "?", ADMIN_IDS)
            if update.callback_query:
                await update.callback_query.answer("⛔ Accès réservé", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    f"⛔ Ce robot est privé.\nTon identifiant : <code>{user.id if user else '?'}</code>",
                    parse_mode=ParseMode.HTML)
            return ConversationHandler.END
        return await func(update, context, *a, **kw)
    return wrapper


# ================================================================ commandes simples
HELP = """🤖 <b>Robot ANONYMETRADER VIP</b>

<b>Signaux</b>
/signal — créer et publier un nouveau signal
/ouverts — signaux en cours + boutons de suivi
/cloture <code>ID PRIX</code> — clôture manuelle (ex : <code>/cloture 12 2655.3</code>)

<b>Bilans</b>
/bilan — bilan du jour (aperçu + bouton publier)
/bilan semaine · /bilan mois

<b>Canal</b>
/accueil — publie et épingle le message d'accueil
/verifier — vérifie que le robot peut publier dans le canal
/id — affiche ton identifiant Telegram
/annuler — annule la saisie en cours

💡 Pour trouver l'ID du canal : transfère-moi n'importe quel message du canal."""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ADMIN_IDS:
        await update.message.reply_text(
            f"👋 Configuration initiale.\nTon identifiant est : <code>{uid}</code>\n"
            f"Mets-le dans ADMIN_IDS puis redémarre le robot.", parse_mode=ParseMode.HTML)
        return
    if uid not in ADMIN_IDS:
        await update.message.reply_text(
            f"⛔ Ce robot est privé.\nTon identifiant : <code>{uid}</code>\n"
            f"(Si c'est toi l'admin : mets ce nombre dans ADMIN_IDS sur Railway.)",
            parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Ton identifiant : <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)


async def forwarded_from_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    origin = update.message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        await update.message.reply_text(
            f"📡 Canal : <b>{origin.chat.title}</b>\nID : <code>{origin.chat.id}</code>\n\n"
            f"Copie cet ID dans CHANNEL_ID.", parse_mode=ParseMode.HTML)


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
            f"Statut du robot : {member.status}\n"
            f"Publier : {'✅' if can_post else '❌'}  |  Modifier/épingler : {'✅' if can_edit else '❌'}",
            parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Problème d'accès au canal : {e}\nAjoute le robot comme administrateur du canal.")


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
async def sig_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sig"] = {}
    rows = [[InlineKeyboardButton(p, callback_data=f"pair:{p}") for p in QUICK_PAIRS[i:i + 2]]
            for i in range(0, len(QUICK_PAIRS), 2)]
    await update.message.reply_text(
        "📊 <b>Nouveau signal</b>\n\n1️⃣ Choisis l'actif ou tape-le (ex : USDJPY) :",
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
    hint = ("LIMIT = acheter plus bas que le prix actuel · STOP = acheter sur cassure, plus haut" if d == "BUY"
            else "LIMIT = vendre plus haut que le prix actuel · STOP = vendre sur cassure, plus bas")
    await q.message.reply_text(
        f"✅ {'🟢 BUY' if d == 'BUY' else '🔴 SELL'}\n\n3️⃣ Type d'ordre ?\n<i>{hint}</i>",
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
        f"✅ {label}\n\n4️⃣ {question}\n"
        f"(un prix <code>2650.5</code> ou une zone <code>2650 - 2652</code>)", parse_mode=ParseMode.HTML)
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
        await update.message.reply_text(f"❗ Pour un {s['direction']}, le SL doit être {side} de l'entrée. Réessaie.")
        return SL
    s["sl"], s["sl_text"] = sl, nums[0][0]
    await update.message.reply_text(
        "✅ SL noté.\n\n6️⃣ Objectifs (TP) ? Sépare-les par des espaces.\n"
        "ex : <code>2655 2660 2670</code>", parse_mode=ParseMode.HTML)
    return TPS


async def sig_tps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nums = parse_nums(update.message.text)
    s = context.user_data["sig"]
    if not 1 <= len(nums) <= 5:
        await update.message.reply_text("❗ Entre 1 et 5 objectifs, séparés par des espaces.")
        return TPS
    buy = s["direction"] == "BUY"
    bad = [t for t, v in nums if (buy and v <= s["entry"]) or (not buy and v >= s["entry"])]
    if bad:
        await update.message.reply_text(f"❗ TP incohérent(s) avec un {s['direction']} : {', '.join(bad)}. Réessaie.")
        return TPS
    nums.sort(key=lambda x: x[1], reverse=not buy)  # TP1 = le plus proche
    s["tps"] = [v for _, v in nums]
    s["tps_text"] = [t for t, _ in nums]
    if s.get("order_type", "MARKET") in PENDING_TYPES:
        await update.message.reply_text(
            "✅ Objectifs notés.\n\n⏳ Validité de l'ordre ?\n"
            "ex : <code>2h</code>, <code>90min</code>, <code>18:00</code>, <code>26/09 12:00</code>\n"
            "(ou /passer = valable jusqu'à annulation)", parse_mode=ParseMode.HTML)
        return EXPIRY
    return await _ask_photo(update.message)


async def _ask_photo(msg):
    await msg.reply_text("7️⃣ Envoie la <b>photo</b> du graphique\n(ou /passer)", parse_mode=ParseMode.HTML)
    return PHOTO


async def sig_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.lower() == "/passer":
        context.user_data["sig"]["expires_at"] = None
    else:
        exp = parse_expiry(txt)
        if not exp:
            await update.message.reply_text("❗ Format non reconnu. Ex : 2h, 90min, 18:00 ou 26/09 12:00 (ou /passer)")
            return EXPIRY
        context.user_data["sig"]["expires_at"] = exp
    return await _ask_photo(update.message)


async def sig_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sig"]["photo"] = update.message.photo[-1].file_id
    return await _ask_note(update)


async def sig_photo_missing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Envoie une photo (pas en fichier) ou tape /passer")
    return PHOTO


async def sig_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sig"]["photo"] = None
    return await _ask_note(update)


async def _ask_note(update: Update):
    await update.message.reply_text(
        "📝 Un commentaire / analyse ? (ex : <i>Cassure résistance H1, RSI > 50</i>)\nou /passer",
        parse_mode=ParseMode.HTML)
    return NOTE


def _next_id() -> int:
    with db() as c:
        r = c.execute("SELECT seq FROM sqlite_sequence WHERE name='signals'").fetchone()
    return (r[0] if r else 0) + 1


async def _send_post(bot, chat_id, s: dict, sig_id: int, markup=None):
    data = {**s, "id": sig_id, "created_at": s.get("created_at") or utc_now_str(),
            "tps": s["tps"], "tps_text": s["tps_text"]}
    text = T.signal_post(signal_view(data))
    src = s.get("photo_bytes") or s.get("photo")
    if src:
        photo = await branded_photo(bot, src, f"{s['pair']} {s['direction']}  #{sig_id}")
        if isinstance(photo, (bytes, bytearray)):
            photo = io.BytesIO(photo)
        msg = await bot.send_photo(chat_id, photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        if s.get("photo_bytes") and msg.photo and chat_id == CHANNEL_ID:  # photo publiée : on garde son identifiant
            s["photo"], s["photo_bytes"] = msg.photo[-1].file_id, None
        return msg
    return await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def publish_signal(bot, s: dict) -> int:
    """Enregistre le signal, le publie dans le canal et renvoie son numéro. Lève une exception si échec."""
    created = utc_now_str()
    with db() as c:
        cur = c.execute(
            """INSERT INTO signals(created_at,pair,direction,entry,entry_text,sl,sl_text,tps,tps_text,note,photo,ref,source,
                                  order_type,expires_at,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (created, s["pair"], s["direction"], s["entry"], s["entry_text"], s["sl"], s["sl_text"],
             json.dumps(s["tps"]), json.dumps(s["tps_text"]), s.get("note"), s.get("photo"),
             s.get("ref"), s.get("source", "manuel"), s.get("order_type") or "MARKET", s.get("expires_at"),
             "PENDING" if s.get("order_type") in PENDING_TYPES else "OPEN"))
        sig_id = cur.lastrowid
    try:
        s["created_at"] = created
        msg = await _send_post(bot, CHANNEL_ID, s, sig_id)
    except Exception:
        with db() as c:
            c.execute("DELETE FROM signals WHERE id=?", (sig_id,))
        raise
    update_signal(sig_id, msg_id=msg.message_id, photo=s.get("photo"))
    return sig_id


async def sig_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    context.user_data["sig"]["note"] = None if txt.strip().lower() == "/passer" else txt.strip()[:300]
    s = context.user_data["sig"]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Publier", callback_data="ok:publish"),
                                InlineKeyboardButton("❌ Annuler", callback_data="ok:cancel")]])
    await update.message.reply_text("👀 <b>Aperçu</b> — voici ce qui sera publié :", parse_mode=ParseMode.HTML)
    await _send_post(context.bot, update.effective_chat.id, s, _next_id(), markup=kb)
    return CONFIRM


async def sig_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(None)
    if q.data == "ok:cancel":
        context.user_data.pop("sig", None)
        await q.message.reply_text("🗑️ Signal annulé.")
        return ConversationHandler.END

    s = context.user_data.pop("sig")
    try:
        sig_id = await publish_signal(context.bot, s)
    except Exception as e:
        await q.message.reply_text(f"❌ Publication impossible : {e}\nVérifie avec /verifier.")
        return ConversationHandler.END
    await q.message.reply_text(
        f"🚀 <b>Signal #{sig_id} publié !</b>\nUtilise les boutons ci-dessous pour le suivi :",
        parse_mode=ParseMode.HTML, reply_markup=control_kb(get_signal(sig_id)))
    return ConversationHandler.END


async def sig_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("sig", None)
    await update.message.reply_text("🗑️ Saisie annulée.")
    return ConversationHandler.END


# ================================================================ suivi TP / SL / BE
def control_kb(row) -> InlineKeyboardMarkup | None:
    if row["status"] == "PENDING":
        sid = row["id"]
        return InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Ordre déclenché", callback_data=f"u:{sid}:activate"),
                                      InlineKeyboardButton("🚫 Annuler l'ordre", callback_data=f"u:{sid}:cancel")]])
    if row["status"] != "OPEN":
        return None
    sid, n_tps = row["id"], len(json.loads(row["tps"]))
    tp_btns = [InlineKeyboardButton(f"🎯 TP{i}", callback_data=f"u:{sid}:tp{i}")
               for i in range(row["tp_hit"] + 1, n_tps + 1)]
    rows = [tp_btns[i:i + 3] for i in range(0, len(tp_btns), 3)]
    second = []
    if not row["be"]:
        second.append(InlineKeyboardButton("🔒 SL → BE", callback_data=f"u:{sid}:be"))
    second.append(InlineKeyboardButton("❌ SL / BE touché", callback_data=f"u:{sid}:sl"))
    rows.append(second)
    rows.append([InlineKeyboardButton("✋ Clôture manuelle", callback_data=f"u:{sid}:manual")])
    return InlineKeyboardMarkup(rows)


async def _reply_in_channel(bot, row, text):
    rp = ReplyParameters(message_id=row["msg_id"], allow_sending_without_reply=True) if row["msg_id"] else None
    await bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML, reply_parameters=rp)


async def apply_action(bot, row, action: str, price: float | None = None, price_text: str | None = None) -> str:
    """Applique tp1..tp5 / be / sl / close à un signal ouvert et publie la mise à jour. Renvoie un résumé."""
    s = dict(row)
    if s["status"] == "PENDING":
        if action == "activate":
            update_signal(s["id"], status="OPEN")
            await _reply_in_channel(bot, row, T.order_triggered(s))
            return "Déclenchement publié ⚡"
        if action in ("cancel", "expire"):
            update_signal(s["id"], status="CANCELLED", outcome="Annulé", closed_at=utc_now_str())
            await _reply_in_channel(bot, row, T.order_cancelled(s, expired=action == "expire"))
            return "Ordre annulé 🚫" if action == "cancel" else "Ordre expiré ⌛"
        raise ValueError("ordre pas encore déclenché : utilise « déclenché » ou « annuler »")
    if s["status"] != "OPEN":
        raise ValueError(f"le signal #{s['id']} est déjà clôturé")
    if action in ("activate", "cancel", "expire"):
        raise ValueError("ce trade est déjà actif (utilise SL, BE ou clôture)")
    tps = json.loads(s["tps"])
    pair, d, entry = s["pair"], s["direction"], s["entry"]

    if action.startswith("tp"):
        n = int(action[2:] or 0)
        if not 1 <= n <= len(tps):
            raise ValueError(f"ce signal n'a que {len(tps)} TP")
        if n <= s["tp_hit"]:
            raise ValueError(f"TP{n} déjà annoncé")
        pips = calc_pips(pair, d, entry, tps[n - 1])
        final = n == len(tps)
        fields = {"tp_hit": n}
        if final:
            fields.update(status="CLOSED", outcome=f"TP{n}", result_pips=pips, closed_at=utc_now_str())
        update_signal(s["id"], **fields)
        await _reply_in_channel(bot, row, T.tp_hit(s, n, pips, final))
        return f"TP{n} publié ✅"

    if action == "be":
        if s["be"]:
            raise ValueError("BE déjà annoncé")
        update_signal(s["id"], be=1)
        await _reply_in_channel(bot, row, T.be_moved(s))
        return "BE publié 🔒"

    if action == "sl":
        if s["tp_hit"] > 0:
            pips = calc_pips(pair, d, entry, tps[s["tp_hit"] - 1])
            outcome, text = f"TP{s['tp_hit']}", T.closed_at_be_after_tp(s, s["tp_hit"], pips)
        elif s["be"]:
            pips, outcome, text = 0.0, "BE", T.closed_at_be(s)
        else:
            pips = calc_pips(pair, d, entry, s["sl"])
            outcome, text = "SL", T.sl_hit(s, pips)
        update_signal(s["id"], status="CLOSED", outcome=outcome, result_pips=pips, closed_at=utc_now_str())
        await _reply_in_channel(bot, row, text)
        return "Clôture publiée"

    if action == "close":
        if price is None:
            raise ValueError("prix de clôture manquant")
        price_text = price_text or fmt_num(price)
        pips = calc_pips(pair, d, entry, price)
        update_signal(s["id"], status="CLOSED", outcome="Manuel", result_pips=pips, closed_at=utc_now_str())
        await _reply_in_channel(bot, row, T.manual_close(s, price_text, pips))
        return f"Clôturé à {price_text} ({pips:+g} pips)"

    raise ValueError(f"action inconnue : {action}")


@admin_only
async def on_update_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, sid, action = q.data.split(":")
    row = get_signal(int(sid))
    if not row or row["status"] not in ("OPEN", "PENDING"):
        await q.answer("Ce signal est déjà clôturé.", show_alert=True)
        await q.edit_message_reply_markup(None)
        return
    if action == "manual":
        await q.answer()
        await q.message.reply_text(f"Envoie : <code>/cloture {sid} PRIX</code>", parse_mode=ParseMode.HTML)
        return
    try:
        await q.answer(await apply_action(context.bot, row, action))
    except ValueError as e:
        await q.answer(str(e), show_alert=True)

    new_row = get_signal(int(sid))
    try:
        if new_row["status"] in ("OPEN", "PENDING"):
            await q.edit_message_reply_markup(control_kb(new_row))
        else:
            res = "" if new_row["result_pips"] is None else f" ({new_row['result_pips']:+g} pips)"
            await q.edit_message_text(f"🏁 Signal #{sid} terminé — {new_row['outcome']}{res}")
    except Exception:
        pass


@admin_only
async def cmd_cloture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2 or not args[0].isdigit() or not parse_nums(args[1]):
        await update.message.reply_text("Usage : /cloture ID PRIX   (ex : /cloture 12 2655.3)")
        return
    row = get_signal(int(args[0]))
    if not row or row["status"] != "OPEN":
        await update.message.reply_text("Signal introuvable, pas encore déclenché, ou déjà clôturé.")
        return
    price_text, price = parse_nums(args[1])[0]
    summary = await apply_action(context.bot, row, "close", price, price_text)
    await update.message.reply_text(f"✅ Signal #{row['id']} : {summary}")


@admin_only
async def cmd_ouverts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("SELECT * FROM signals WHERE status IN ('OPEN','PENDING') ORDER BY id").fetchall()
    if not rows:
        await update.message.reply_text("Aucun signal en cours.")
        return
    for r in rows:
        extra = (f" · TP{r['tp_hit']} ✅" if r["tp_hit"] else "") + (" · BE 🔒" if r["be"] else "")
        ot = r["order_type"] or "MARKET"
        kind = "" if ot == "MARKET" else f" {ot}"
        state = " · ⏳ en attente" if r["status"] == "PENDING" else ""
        await update.message.reply_text(
            f"📊 #{r['id']} {r['pair']} {r['direction']}{kind} @ {r['entry_text']}{state}{extra}",
            reply_markup=control_kb(r))


# ================================================================ bilans
def period_bounds(period: str):
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "semaine":
        start -= timedelta(days=start.weekday())
        label = f"du {start:%d/%m} au {now:%d/%m/%Y}"
    elif period == "mois":
        start = start.replace(day=1)
        label = f"{now:%m/%Y}"
    else:
        label = f"{now:%d/%m/%Y}"
    to_utc = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return to_utc(start), to_utc(now + timedelta(seconds=1)), label


def compute_report(period: str):
    start, end, label = period_bounds(period)
    with db() as c:
        trades = [dict(r) for r in c.execute(
            "SELECT * FROM signals WHERE status='CLOSED' AND closed_at>=? AND closed_at<? ORDER BY closed_at",
            (start, end)).fetchall()]
    if not trades:
        return None
    for t in trades:  # résultat en R (multiple du risque) : comparable entre actifs
        risk = abs(calc_pips(t["pair"], t["direction"], t["entry"], t["sl"])) or 1
        t["r"] = round(t["result_pips"] / risk, 2)
    wins = sum(t["result_pips"] > 0 for t in trades)
    losses = sum(t["result_pips"] < 0 for t in trades)
    stats = {
        "total": len(trades), "wins": wins, "losses": losses, "be": len(trades) - wins - losses,
        "winrate": round(100 * wins / (wins + losses)) if wins + losses else 100,
        "pips": round(sum(t["result_pips"] for t in trades), 1),
        "r": round(sum(t["r"] for t in trades), 2),
    }
    return T.report(period, label, trades, stats)


@admin_only
async def cmd_bilan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    period = (context.args[0].lower() if context.args else "jour")
    if period not in ("jour", "semaine", "mois"):
        period = "jour"
    text = compute_report(period)
    if not text:
        await update.message.reply_text("Aucun trade clôturé sur cette période.")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Publier dans le canal", callback_data=f"rep:{period}")]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@admin_only
async def on_report_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    period = q.data.split(":")[1]
    text = compute_report(period)
    if text:
        await context.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML)
        await q.answer("Bilan publié ✅")
    else:
        await q.answer("Rien à publier.")
    await q.edit_message_reply_markup(None)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        log.error("⚠️ CONFLIT : un AUTRE programme utilise le même jeton en ce moment "
                  "(autre service/projet Railway, ancien déploiement, ou ton PC). "
                  "Arrête-le, ou régénère le jeton dans @BotFather (/revoke).")
        return
    if isinstance(err, NetworkError):
        log.warning("Réseau instable (%s) — nouvelle tentative automatique.", err)
        return
    log.error("Erreur inattendue", exc_info=err)


async def job_expire_orders(context: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("SELECT * FROM signals WHERE status='PENDING' AND expires_at IS NOT NULL AND expires_at<=?",
                         (utc_now_str(),)).fetchall()
    for r in rows:
        try:
            await apply_action(context.bot, r, "expire")
            await _notify_admins(context.bot, f"⌛ Ordre #{r['id']} {r['pair']} {r['direction']} {r['order_type']} expiré et annulé.")
        except Exception as e:
            log.warning("Expiration #%s impossible : %s", r["id"], e)


async def job_report(context: ContextTypes.DEFAULT_TYPE):
    period = context.job.data
    text = compute_report(period)
    if text:
        await context.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML)
        log.info("Bilan %s publié automatiquement", period)


# ================================================================ API pour l'IA de trading
# Ton IA envoie ses signaux par HTTP (les robots Telegram ne peuvent pas se lire entre eux).
API_KEY = os.getenv("API_KEY", "").strip()
API_MODE = os.getenv("API_MODE", "validation").strip().lower()      # "validation" ou "auto"
API_MAX_AGE_MIN = int(os.getenv("API_MAX_AGE_MIN", "5") or 5)        # durée de validité d'un signal en attente
PORT = int(os.getenv("PORT", "8080") or 8080)
PENDING: dict[str, dict] = {}   # signaux IA en attente de validation
_APP = None                     # référence vers l'application Telegram

DIR_WORDS = {"BUY": "BUY", "LONG": "BUY", "ACHAT": "BUY", "ACHETER": "BUY",
             "SELL": "SELL", "SHORT": "SELL", "VENTE": "SELL", "VENDRE": "SELL"}


def _num(v) -> tuple[str, float]:
    if isinstance(v, bool) or v is None:
        raise ValueError("prix manquant")
    if isinstance(v, (int, float)):
        return fmt_num(float(v)), float(v)
    n = parse_nums(str(v))
    if len(n) != 1:
        raise ValueError(f"prix invalide : {v!r}")
    return n[0]


def parse_text_signal(txt: str) -> dict:
    """Format texte libre : 'XAUUSD BUY 2650 SL 2640 TP 2660 2670' (ou TP1 2660 TP2 2670)."""
    toks = re.findall(r"[A-Za-zÀ-ÿ]+\d*|\d+(?:\.\d+)?", txt.upper())
    data, section, entry, tps, sl = {}, None, [], [], []
    for t in toks:
        if t in DIR_WORDS and "direction" not in data:
            data["direction"], section = t, "entry"
        elif t in ("LIMIT", "LIMITE", "STOP") and "direction" in data and "order_type" not in data and section == "entry" and not entry:
            data["order_type"] = t
        elif re.fullmatch(r"(SL|STOP|STOPLOSS)\d*", t):
            section = "sl"
        elif re.fullmatch(r"(TP|TARGET|OBJECTIF)\d*", t):
            section = "tp"
        elif re.fullmatch(r"(ENTRY|ENTREE|ENTRÉE|PRICE|PRIX|AT)", t):
            section = "entry"
        elif re.fullmatch(r"\d+(?:\.\d+)?", t):
            {"entry": entry, "sl": sl, "tp": tps}.get(section, []).append(t)
        elif "pair" not in data and len(t) >= 3 and t not in DIR_WORDS:
            data["pair"] = t
    data.update(entry=entry, sl=sl[0] if sl else None, tp=tps)
    return data


def build_signal(d: dict) -> dict:
    """Transforme les données reçues de l'IA en signal validé (mêmes contrôles que /signal)."""
    pair = re.sub(r"[^A-Z0-9./]", "", str(d.get("pair") or d.get("symbol") or d.get("ticker") or "").upper())[:15]
    if not pair:
        raise ValueError("champ 'pair' manquant (ex : XAUUSD)")
    raw_dir = str(d.get("direction") or d.get("side") or d.get("action") or "").strip().upper().replace("_", " ")
    parts = raw_dir.split()
    direction = DIR_WORDS.get(parts[0]) if parts else None
    if not direction:
        raise ValueError("champ 'direction' invalide (BUY ou SELL)")
    ot = str(d.get("order_type") or d.get("type") or (parts[1] if len(parts) > 1 else "MARKET")).strip().upper()
    ot = {"MARKET": "MARKET", "MARCHE": "MARKET", "MARCHÉ": "MARKET", "LIMIT": "LIMIT", "LIMITE": "LIMIT",
          "STOP": "STOP"}.get(ot)
    if not ot:
        raise ValueError("champ 'order_type' invalide (market, limit ou stop)")
    expires_at = None
    if ot in PENDING_TYPES:
        if d.get("expiry_minutes") not in (None, ""):
            expires_at = parse_expiry(f"{float(d['expiry_minutes']):g}min")
        elif d.get("valid_until") not in (None, ""):
            expires_at = parse_expiry(str(d["valid_until"]))
            if not expires_at:
                raise ValueError("'valid_until' illisible (ex : 18:00 ou 26/09 12:00)")

    e = d.get("entry", d.get("price"))
    if isinstance(e, str) and len(parse_nums(e)) == 2:
        e = [x[1] for x in parse_nums(e)]
    if isinstance(e, (list, tuple)) and len(e) == 1:
        e = e[0]
    if isinstance(e, (list, tuple)):
        if len(e) != 2:
            raise ValueError("'entry' : un prix ou une zone [min, max]")
        lo, hi = sorted((_num(e[0]), _num(e[1])), key=lambda x: x[1])
        entry, entry_text = (lo[1] + hi[1]) / 2, f"{lo[0]} – {hi[0]}"
    else:
        entry_text, entry = _num(e)

    sl_text, sl = _num(d.get("sl", d.get("stop_loss")))
    if (direction == "BUY" and sl >= entry) or (direction == "SELL" and sl <= entry):
        raise ValueError(f"SL incohérent pour un {direction} (entrée {entry_text}, SL {sl_text})")

    raw_tps = d.get("tp", d.get("tps", d.get("take_profit")))
    if raw_tps is None:
        raw_tps = [d[k] for k in ("tp1", "tp2", "tp3", "tp4", "tp5") if d.get(k) is not None]
    if isinstance(raw_tps, str):
        raw_tps = [t for t, _ in parse_nums(raw_tps)]
    if not isinstance(raw_tps, (list, tuple)):
        raw_tps = [raw_tps]
    tps = [_num(t) for t in raw_tps]
    if not 1 <= len(tps) <= 5:
        raise ValueError("il faut entre 1 et 5 TP")
    buy = direction == "BUY"
    bad = [t for t, v in tps if (buy and v <= entry) or (not buy and v >= entry)]
    if bad:
        raise ValueError(f"TP incohérent(s) pour un {direction} : {', '.join(bad)}")
    tps.sort(key=lambda x: x[1], reverse=not buy)

    note = d.get("note") or d.get("comment") or d.get("analyse")
    return {
        "pair": pair, "direction": direction, "entry": entry, "entry_text": entry_text,
        "sl": sl, "sl_text": sl_text, "tps": [v for _, v in tps], "tps_text": [t for t, _ in tps],
        "note": str(note).strip()[:300] if note else None, "photo": None, "photo_bytes": None,
        "ref": str(d["ref"])[:64] if d.get("ref") not in (None, "") else None, "source": "IA",
        "order_type": ot, "expires_at": expires_at,
    }


async def _load_photo(d: dict) -> bytes | None:
    if d.get("photo_base64"):
        import base64
        b64 = str(d["photo_base64"])
        return base64.b64decode(b64.split(",", 1)[1] if b64.startswith("data:") else b64)
    if d.get("photo_url"):
        import asyncio, urllib.request
        def fetch():
            req = urllib.request.Request(str(d["photo_url"]), headers={"User-Agent": "AnonymetraderBot"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read(10_000_000)
        return await asyncio.get_running_loop().run_in_executor(None, fetch)
    return None


async def _notify_admins(bot, text, markup=None):
    for uid in ADMIN_IDS:
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            log.warning("Notification admin %s impossible : %s", uid, e)


def _find_by_ref(ref: str):
    with db() as c:
        return c.execute("SELECT * FROM signals WHERE ref=? ORDER BY id DESC LIMIT 1", (ref,)).fetchone()


async def api_new_signal(bot, d: dict) -> tuple[int, dict]:
    s = build_signal(d)
    if s["ref"] and (old := _find_by_ref(s["ref"])):
        return 200, {"ok": True, "status": "duplicate", "id": old["id"],
                     "message": "signal déjà reçu avec ce 'ref'"}
    if s["ref"] and any(p.get("ref") == s["ref"] for p in PENDING.values()):
        return 200, {"ok": True, "status": "duplicate", "message": "signal déjà en attente de validation"}
    try:
        s["photo_bytes"] = await _load_photo(d)
    except Exception as e:
        log.warning("Photo IA ignorée : %s", e)

    if API_MODE == "auto":
        sig_id = await publish_signal(bot, s)
        await _notify_admins(bot, f"🤖 <b>Signal IA #{sig_id} publié</b> ({s['pair']} {s['direction']}{'' if s['order_type'] == 'MARKET' else ' ' + s['order_type']})",
                             control_kb(get_signal(sig_id)))
        return 200, {"ok": True, "status": "published", "id": sig_id}

    import secrets, time as _t
    token = secrets.token_hex(6)
    s["received"] = _t.time()
    PENDING[token] = s
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Publier", callback_data=f"api:ok:{token}"),
                                InlineKeyboardButton("❌ Refuser", callback_data=f"api:no:{token}")]])
    await _notify_admins(bot, f"🤖 <b>Nouveau signal de l'IA</b> — à valider sous {API_MAX_AGE_MIN} min :")
    for uid in ADMIN_IDS:
        try:
            await _send_post(bot, uid, s, _next_id(), markup=kb)   # l'aperçu garde la photo (file_id)
        except Exception as e:
            log.warning("Aperçu admin %s impossible : %s", uid, e)
    return 200, {"ok": True, "status": "pending_validation", "token": token}


@admin_only
async def on_api_validation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import time as _t
    q = update.callback_query
    _, decision, token = q.data.split(":")
    s = PENDING.pop(token, None)
    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass
    if not s:
        await q.answer("Signal déjà traité ou expiré.", show_alert=True)
        return
    if decision == "no":
        await q.answer("Signal refusé")
        await q.message.reply_text("🗑️ Signal IA refusé.")
        return
    age = (_t.time() - s["received"]) / 60
    if age > API_MAX_AGE_MIN:
        await q.answer("Trop tard", show_alert=True)
        await q.message.reply_text(f"⌛ Signal expiré ({age:.0f} min) : non publié, le prix a pu bouger.")
        return
    try:
        sig_id = await publish_signal(context.bot, s)
    except Exception as e:
        await q.answer("Échec", show_alert=True)
        await q.message.reply_text(f"❌ Publication impossible : {e}")
        return
    await q.answer("Publié ✅")
    await q.message.reply_text(f"🚀 <b>Signal IA #{sig_id} publié !</b>", parse_mode=ParseMode.HTML,
                               reply_markup=control_kb(get_signal(sig_id)))


EVENT_ALIASES = {"breakeven": "be", "break_even": "be", "stop": "sl", "stoploss": "sl", "stop_loss": "sl",
                 "triggered": "activate", "filled": "activate", "declenche": "activate", "déclenché": "activate",
                 "cancelled": "cancel", "canceled": "cancel", "annule": "cancel", "annulé": "cancel", "annuler": "cancel",
                 "cloture": "close", "clôture": "close", "exit": "close"}


async def api_update(bot, d: dict) -> tuple[int, dict]:
    row = None
    if d.get("id") not in (None, ""):
        row = get_signal(int(d["id"]))
    elif d.get("ref") not in (None, ""):
        row = _find_by_ref(str(d["ref"]))
    if not row:
        return 404, {"ok": False, "error": "signal introuvable (donne 'id' ou 'ref')"}
    ev = str(d.get("event") or d.get("action") or "").strip().lower().replace(" ", "")
    ev = EVENT_ALIASES.get(ev, ev)
    if not re.fullmatch(r"tp[1-5]|be|sl|close|activate|cancel", ev):
        return 400, {"ok": False, "error": "event doit être tp1..tp5, be, sl, close, activate ou cancel"}
    price_text = price = None
    if ev == "close":
        price_text, price = _num(d.get("price"))
    summary = await apply_action(bot, row, ev, price, price_text)
    await _notify_admins(bot, f"🤖 Signal #{row['id']} : {summary}")
    new = get_signal(row["id"])
    return 200, {"ok": True, "id": row["id"], "status": new["status"], "message": summary}


async def api_route(method: str, path: str, headers: dict, query: dict, body: bytes) -> tuple[int, dict]:
    path = path.rstrip("/") or "/"
    if method == "GET" and path in ("/", "/health"):
        return 200, {"ok": True, "service": "anonymetrader-bot", "api": bool(API_KEY), "mode": API_MODE}
    if method != "POST" or path not in ("/api/signal", "/api/update"):
        return 404, {"ok": False, "error": "route inconnue (POST /api/signal ou /api/update)"}

    text = body.decode("utf-8", "replace").strip()
    try:
        d = json.loads(text) if text else {}
        if not isinstance(d, dict):
            raise ValueError
    except ValueError:
        d = parse_text_signal(text) if path == "/api/signal" else {}
        m = re.search(r"(?:KEY|CLE|CLÉ)\s*[:=]\s*(\S+)", text, re.I)
        if m:
            d["key"] = m.group(1)

    given = headers.get("x-api-key") or query.get("key") or str(d.pop("key", "") or d.pop("api_key", ""))
    if auth := headers.get("authorization", ""):
        given = given or auth.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), API_KEY.encode()):
        log.warning("API : clé refusée")
        return 401, {"ok": False, "error": "clé API invalide"}

    try:
        if path == "/api/signal":
            return await api_new_signal(_APP.bot, d)
        return await api_update(_APP.bot, d)
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    except Exception as e:
        log.exception("Erreur API")
        return 500, {"ok": False, "error": f"erreur interne : {e}"}


async def _handle_http(reader, writer):
    import asyncio, urllib.parse
    status, payload = 400, {"ok": False, "error": "requête invalide"}
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = b""
            while True:
                size = int((await asyncio.wait_for(reader.readline(), 15)).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    await reader.readline()
                    break
                body += await reader.readexactly(size)
                await reader.readline()
                if len(body) > 15_000_000:
                    raise ValueError("trop gros")
        else:
            n = int(headers.get("content-length", "0") or 0)
            if n > 15_000_000:
                raise ValueError("trop gros")
            body = await asyncio.wait_for(reader.readexactly(n), 30) if n else b""
        path, _, qs = target.partition("?")
        status, payload = await api_route(method.upper(), path, headers, dict(urllib.parse.parse_qsl(qs)), body)
    except Exception as e:
        log.warning("Requête HTTP rejetée : %s", e)
    reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 500: "Internal Server Error"}
    data = json.dumps(payload, ensure_ascii=False).encode()
    try:
        writer.write(f"HTTP/1.1 {status} {reasons.get(status, 'OK')}\r\nContent-Type: application/json; charset=utf-8\r\n"
                     f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        await writer.drain()
    finally:
        writer.close()


async def start_api(app):
    global _APP
    _APP = app
    if not API_KEY:
        log.info("API désactivée (ajoute API_KEY pour que ton IA puisse envoyer des signaux).")
        return
    import asyncio
    app.bot_data["api_server"] = await asyncio.start_server(_handle_http, "0.0.0.0", PORT)
    log.info("🌐 API active sur le port %s — mode %s", PORT, API_MODE)


async def stop_api(app):
    srv = app.bot_data.get("api_server")
    if srv:
        srv.close()
        await srv.wait_closed()


# ================================================================ démarrage
def main():
    if not BOT_TOKEN:
        seen = sorted(k for k in os.environ if "TOKEN" in k.upper() or "BOT" in k.upper())
        raise SystemExit(
            "❌ BOT_TOKEN introuvable. Vérifie que la variable s'appelle exactement BOT_TOKEN "
            f"(majuscules, avec le _) et que les changements Railway sont bien déployés. "
            f"Variables ressemblantes trouvées : {seen or 'aucune'}")
    masked = f"{BOT_TOKEN[:4]}…{BOT_TOKEN[-4:]} ({len(BOT_TOKEN)} caractères)"
    if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
        raise SystemExit(
            f"❌ BOT_TOKEN mal formé : {masked}. Un jeton ressemble à 7412345678:AAH3k… "
            "(chiffres, deux-points, ~35 caractères). Recopie-le depuis @BotFather sans guillemets ni espaces.")
    log.info("Jeton chargé : %s", masked)
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS vide : envoie /start au robot pour connaître ton identifiant.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(start_api).post_shutdown(stop_api).build()
    private = filters.ChatType.PRIVATE

    conv = ConversationHandler(
        entry_points=[CommandHandler("signal", sig_start, filters=private)],
        states={
            PAIR: [CallbackQueryHandler(sig_pair_cb, pattern=r"^pair:"),
                   MessageHandler(filters.TEXT & ~filters.COMMAND, sig_pair_text)],
            DIRECTION: [CallbackQueryHandler(sig_direction, pattern=r"^dir:")],
            ORDER_TYPE: [CallbackQueryHandler(sig_order_type, pattern=r"^ot:")],
            ENTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_entry)],
            SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_sl)],
            TPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_tps)],
            EXPIRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_expiry), CommandHandler("passer", sig_expiry)],
            PHOTO: [MessageHandler(filters.PHOTO, sig_photo), CommandHandler("passer", sig_skip_photo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, sig_photo_missing)],
            NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_note), CommandHandler("passer", sig_note)],
            CONFIRM: [CallbackQueryHandler(sig_confirm, pattern=r"^ok:")],
        },
        fallbacks=[CommandHandler("annuler", sig_cancel)],
        conversation_timeout=15 * 60,
    )
    app.add_handler(conv)
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("aide", cmd_start, filters=private))
    app.add_handler(CommandHandler("id", cmd_id, filters=private))
    app.add_handler(CommandHandler("verifier", cmd_verifier, filters=private))
    app.add_handler(CommandHandler("accueil", cmd_accueil, filters=private))
    app.add_handler(CommandHandler("cloture", cmd_cloture, filters=private))
    app.add_handler(CommandHandler("ouverts", cmd_ouverts, filters=private))
    app.add_handler(CommandHandler("bilan", cmd_bilan, filters=private))
    app.add_handler(CallbackQueryHandler(on_update_button, pattern=r"^u:"))
    app.add_handler(CallbackQueryHandler(on_report_button, pattern=r"^rep:"))
    app.add_handler(CallbackQueryHandler(on_api_validation, pattern=r"^api:"))
    app.add_handler(MessageHandler(private & filters.FORWARDED, forwarded_from_channel))

    # Bilans automatiques (PTB : 0 = dimanche ... 6 = samedi)
    if app.job_queue:
        app.job_queue.run_repeating(job_expire_orders, interval=60, first=10, name="expire_orders")
    if DAILY_REPORT_TIME and app.job_queue:
        h, m = map(int, DAILY_REPORT_TIME.split(":"))
        app.job_queue.run_daily(job_report, time(h, m, tzinfo=TZ), days=(1, 2, 3, 4, 5), data="jour", name="daily")
        days = {"dimanche": 0, "lundi": 1, "mardi": 2, "mercredi": 3, "jeudi": 4, "vendredi": 5, "samedi": 6}
        wd = days.get(WEEKLY_REPORT_DAY, 5)
        weekly_at = (datetime(2000, 1, 1, h, m) + timedelta(minutes=5)).time()
        app.job_queue.run_daily(job_report, weekly_at.replace(tzinfo=TZ), days=(wd,), data="semaine", name="weekly")
        log.info("Bilans auto : quotidien %s, hebdo le %s", DAILY_REPORT_TIME, WEEKLY_REPORT_DAY)

    log.info("🤖 Robot démarré — canal %s, admins %s", CHANNEL_ID, ADMIN_IDS)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        raise SystemExit(
            f"❌ Telegram refuse ce jeton ({masked}). Il a peut-être été régénéré ou révoqué : "
            "dans @BotFather → /mybots → ton robot → API Token, recopie le jeton actuel.")


if __name__ == "__main__":
    main()
