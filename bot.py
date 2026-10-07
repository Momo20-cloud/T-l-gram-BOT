"""
ANONYMETRADER VIP — Robot de publication de signaux Telegram
--------------------------------------------------------------
Tu parles au robot en privé (/signal), il te pose les questions,
te montre un aperçu, puis publie le signal dans ton canal VIP
avec la photo et le modèle défini dans templates.py.
"""
import asyncio
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

import instruments as I
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
        if "closed_pct" not in cols:
            c.execute("ALTER TABLE signals ADD COLUMN closed_pct REAL DEFAULT 0")
        if "realized_pips" not in cols:
            c.execute("ALTER TABLE signals ADD COLUMN realized_pips REAL DEFAULT 0")


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
    p = I.normalize(pair)
    known = I.pip_size(p)
    if known is not None:
        return known
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
HELP = f"""🤖 <b>Robot {T.escape(T.BRAND)}</b>

<b>Signaux</b>
/signal — créer et publier un nouveau signal
⚡ Ou envoie directement : <code>XAUUSD BUY 2650 SL 2645 TP 2655 2660</code>
📷 Ou une <b>capture TradingView</b> avec l'outil Position longue/courte : je lis l'entrée, le SL et le TP
   (avec la photo + ce texte en légende, c'est encore plus rapide)
/ouverts — signaux en cours + boutons de suivi
🎯 Au TP, le robot te demande quel % clôturer (25 %, 50 %…)
/cloture <code>ID PRIX</code> — clôture manuelle (ex : <code>/cloture 12 2655.3</code>)

<b>Bilans</b> (aperçu + bouton publier)
/bilan — aujourd'hui · /bilan hier
/bilan semaine · /bilan mois · /bilan mois dernier
/bilan septembre · /bilan 09/2026 · /bilan annee
/bilan <code>01/09 15/09</code> — période au choix

<b>Annonces économiques</b>
/news — annonce (NFP, CPI, FOMC…) + rappel avant + chiffre réel

<b>Canal</b>
/reglages — favoris, fuseau horaire, heure des bilans, bandeau photo…
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
    context.user_data["_flow"] = True
    rows = _pairs_kb_rows()
    await update.message.reply_text(
        "📊 <b>Nouveau signal</b>\n\n"
        "⚡ <b>Mode rapide</b> : envoie tout en un message, ex :\n"
        "<code>XAUUSD BUY 2650 SL 2645 TP 2655 2660 2670</code>\n"
        "<code>XAUUSD SELL LIMIT 2670 SL 2676 TP 2660 EXP 2h</code>\n"
        "(ajoute ton analyse sur une 2e ligne si tu veux)\n\n"
        "🐢 <b>Pas à pas</b> : choisis l'actif ou tape-le (ex : USDJPY) :",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    return PAIR


def _pairs_kb_rows():
    """Actifs favoris (QUICK_PAIRS) + un bouton par catégorie du catalogue."""
    fav = [I.normalize(p) for p in QUICK_PAIRS]
    rows = [[InlineKeyboardButton(p, callback_data=f"pair:{p}") for p in fav[i:i + 2]] for i in range(0, len(fav), 2)]
    cats = [InlineKeyboardButton(label, callback_data=f"cat:{key}") for key, (label, _) in I.CATEGORIES.items()]
    return rows + [cats[i:i + 3] for i in range(0, len(cats), 3)]


async def sig_pair_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":", 1)[1]
    if key == "back" or key not in I.CATEGORIES:
        await q.edit_message_reply_markup(InlineKeyboardMarkup(_pairs_kb_rows()))
        return PAIR
    syms = I.category_symbols(key)
    rows = [[InlineKeyboardButton(p, callback_data=f"pair:{p}") for p in syms[i:i + 4]] for i in range(0, len(syms), 4)]
    rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="cat:back")])
    await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
    return PAIR


def unknown_pair_warning(pair: str) -> str:
    if I.is_known(pair):
        return ""
    return (f"\n⚠️ <b>{escape(pair)}</b> n'est pas dans le catalogue : les pips seront calculés comme du Forex "
            "(0.0001). Vérifie l'orthographe, ou ajoute l'actif dans instruments.py.")


async def _ask_direction(msg, pair):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🟢 BUY", callback_data="dir:BUY"),
                                InlineKeyboardButton("🔴 SELL", callback_data="dir:SELL")]])
    await msg.reply_text(f"✅ {escape(pair)}{unknown_pair_warning(pair)}\n\n2️⃣ Direction ?", reply_markup=kb,
                         parse_mode=ParseMode.HTML)


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
    pair = I.normalize(update.message.text)
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
    if context.user_data["sig"].get("quick"):
        return await _show_preview(update, context)
    return await _ask_note(update)


async def sig_photo_missing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Envoie une photo (pas en fichier) ou tape /passer")
    return PHOTO


async def sig_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sig"]["photo"] = None
    if context.user_data["sig"].get("quick"):
        return await _show_preview(update, context)
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
    return await _show_preview(update, context)


async def _show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = context.user_data["sig"]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Publier", callback_data="ok:publish"),
                                InlineKeyboardButton("❌ Annuler", callback_data="ok:cancel")],
                               [InlineKeyboardButton("✏️ Modifier", callback_data="ok:edit")]])
    await update.effective_message.reply_text("👀 <b>Aperçu</b> — voici ce qui sera publié :", parse_mode=ParseMode.HTML)
    await _send_post(context.bot, update.effective_chat.id, s, _next_id(), markup=kb)
    return CONFIRM


# ---------------------------------------------------------------- mode rapide (tout en un message)
QUICK_RE = re.compile(r"(?i)\b(buy|sell|achat|vente|long|short)\b.*\bsl\b.*\btp", re.S)
QUICK_HELP = ("Format : <code>PAIRE BUY|SELL [LIMIT|STOP] ENTRÉE SL prix TP prix prix… [EXP 2h]</code>\n"
              "ex : <code>XAUUSD BUY 2650 SL 2645 TP 2655 2660 2670</code>")


def parse_quick_signal(text: str) -> dict:
    """Signal complet en un message -> signal validé (lève ValueError avec un message clair)."""
    text = (text or "").strip()
    lines = text.splitlines()
    first, note = lines[0], " ".join(l.strip() for l in lines[1:] if l.strip()) or None
    m = re.search(r"(?i)\b(?:note|analyse|comment(?:aire)?)\s*:?\s*(.+)$", first)
    if m:
        note = (m.group(1) + (" " + note if note else "")).strip()
        first = first[:m.start()]
    expiry = None
    m = re.search(r"(?i)\b(?:exp(?:ire)?|valid(?:e|ité|ite)?|jusqu\S*)\s*:?\s*"
                  r"((?:\d{1,2}/\d{1,2}\s+)?\d{1,2}[:h]\d{2}|\d+(?:[.,]\d+)?\s*(?:h|heures?|min|minutes?|m)\b)", first)
    if m:
        expiry = m.group(1).replace(" ", "")
        first = first[:m.start()] + first[m.end():]
    if re.search(r"(?i)\b(?:exp(?:ire)?|validit[ée]|jusqu)", first):
        raise ValueError("validité non comprise (ex : EXP 2h, EXP 90min, EXP 18:00, EXP 10/10 14:30)")
    # virgules décimales : 2650,5 ou 1,0850 -> point
    first = re.sub(r"\b(\d),(\d+)\b", r"\1.\2", first)
    first = re.sub(r"(\d),(\d{1,2})\b", r"\1.\2", first)
    d = parse_text_signal(first)
    if not d.get("entry"):
        raise ValueError("prix d'entrée manquant")
    if not d.get("tp"):
        raise ValueError("aucun TP trouvé (écris TP suivi des prix)")
    if note:
        d["note"] = note
    if expiry:
        d["valid_until"] = expiry
    sig = build_signal(d)
    if sig["order_type"] in PENDING_TYPES and expiry and not sig.get("expires_at"):
        raise ValueError(f"validité « {expiry} » non comprise (ex : EXP 2h, EXP 90min, EXP 18:00)")
    sig["source"] = "manuel"
    sig["quick"] = True
    return sig


@admin_only
async def quick_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.caption if msg.photo else msg.text
    try:
        s = parse_quick_signal(text)
    except ValueError as e:
        await msg.reply_text(f"❗ {escape(str(e))}\n\n{QUICK_HELP}", parse_mode=ParseMode.HTML)
        return PAIR if context.user_data.get("_flow") else ConversationHandler.END
    context.user_data["sig"] = s
    context.user_data["_flow"] = False
    if msg.photo:  # photo + légende : tout est là, on passe à l'aperçu
        s["photo"] = msg.photo[-1].file_id
        return await _show_preview(update, context)
    kind = s["direction"] if s["order_type"] == "MARKET" else f"{s['direction']} {s['order_type']}"
    await msg.reply_text(
        f"✅ Compris : <b>{escape(s['pair'])} {kind}</b> @ {s['entry_text']} · SL {s['sl_text']} · "
        f"TP {' / '.join(s['tps_text'])}{unknown_pair_warning(s['pair'])}\n\n📸 Envoie la <b>photo</b> du graphique, ou /passer",
        parse_mode=ParseMode.HTML)
    return PHOTO


# ---------------------------------------------------------------- lecture d'une capture TradingView
CHART_PAIR = 30
CHART_HELP = ("💡 Il me faut une capture <b>TradingView</b> avec l'outil <b>Position longue / courte</b> visible en entier "
              "et l'échelle de prix à droite.\nSinon, envoie la photo avec le signal en légende : "
              "<code>XAUUSD BUY 2650 SL 2645 TP 2660</code>")


def _pairs_in(text: str) -> tuple[str | None, str]:
    """Actif trouvé dans une légende + le reste du texte (utilisé comme analyse)."""
    pair, rest = None, []
    for tok in (text or "").split():
        if pair is None and len(tok) >= 3 and I.is_known(tok):
            pair = I.normalize(tok)
        else:
            rest.append(tok)
    return pair, " ".join(rest).strip()


@admin_only
async def chart_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capture TradingView sans légende de signal : on lit l'outil de position."""
    msg = update.message
    try:
        import chart_reader as CR
    except ImportError:
        CR = None
    if CR is None or not CR.available():
        await msg.reply_text("📷 La lecture des captures n'est pas activée sur ce serveur.\n" + CHART_HELP,
                             parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    if msg.photo:
        f, photo_id = await msg.photo[-1].get_file(), msg.photo[-1].file_id
    else:
        f, photo_id = await msg.document.get_file(), None
    raw = bytes(await f.download_as_bytearray())
    wait = await msg.reply_text("🔎 Je lis ta capture…")
    try:
        r = await asyncio.to_thread(CR.read_position_tool, raw)
    except CR.ChartReadError as e:
        await wait.edit_text(f"❗ Je n'ai pas pu lire la capture : {escape(str(e))}.\n\n{CHART_HELP}", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    except Exception as e:  # image illisible, format inattendu…
        log.warning("Lecture de capture impossible : %s", e)
        await wait.edit_text(f"❗ Image illisible.\n\n{CHART_HELP}", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    cap_pair, note = _pairs_in(msg.caption or "")
    context.user_data["chart"] = {
        "direction": r.direction, "entry": r.fmt(r.entry), "sl": r.fmt(r.sl),
        "tp": [r.fmt(t) for t in (r.tps or [r.tp])], "guessed": r.direction_guessed,
        "estimated": [k for k in ("entry", "sl", "tp") if not r.exact.get(k)], "note": note or None,
        "photo": photo_id, "photo_bytes": None if photo_id else raw,
    }
    await wait.delete()
    pair = cap_pair or r.pair
    if not pair:
        await msg.reply_text(f"🔎 Lu : <b>{r.direction}</b> @ {r.fmt(r.entry)} · SL {r.fmt(r.sl)} · "
                             f"TP {' / '.join(context.user_data['chart']['tp'])}\n\n"
                             "❓ Je n'ai pas trouvé le nom de l'actif. Lequel est-ce ? (ex : XAUUSD)", parse_mode=ParseMode.HTML)
        return CHART_PAIR
    return await _chart_finish(update, context, pair)


async def chart_pair_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pair = I.normalize(update.message.text)
    if not pair or "chart" not in context.user_data:
        await update.message.reply_text("Tape le symbole de l'actif, ex : XAUUSD (ou /annuler)")
        return CHART_PAIR
    return await _chart_finish(update, context, pair)


async def _chart_finish(update: Update, context: ContextTypes.DEFAULT_TYPE, pair: str):
    c = context.user_data.pop("chart")
    try:
        s = build_signal({"pair": pair, "direction": c["direction"], "entry": c["entry"], "sl": c["sl"],
                          "tp": c["tp"], "note": c["note"]})
    except ValueError as e:
        await update.message.reply_text(f"❗ {escape(str(e))}\n\n{CHART_HELP}", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    s.update(photo=c["photo"], photo_bytes=c["photo_bytes"], quick=True, source="capture")
    context.user_data["sig"] = s
    context.user_data["_flow"] = False
    warn = ("\n⚠️ Valeurs <b>estimées</b> d'après leur position sur le graphique : vérifie-les bien."
            if c["estimated"] else "")
    if c.get("guessed"):
        warn += "\n⚠️ Je n'ai pas pu dire avec certitude quelle zone est le profit : vérifie le <b>sens</b>."
    await update.message.reply_text(
        f"🔎 <b>Lu sur ta capture</b> : <b>{escape(s['pair'])} {s['direction']}</b> @ {s['entry_text']} · "
        f"SL {s['sl_text']} · TP {' / '.join(s['tps_text'])}{warn}{unknown_pair_warning(s['pair'])}\n\n"
        f"✏️ Une erreur ? Appuie sur <b>Modifier</b> sous l'aperçu, ou envoie la version corrigée, ex :\n"
        f"<code>{escape(s['pair'])} {s['direction']} {s['entry_text']} SL {s['sl_text']} TP {' '.join(s['tps_text'])}</code>",
        parse_mode=ParseMode.HTML)
    return await _show_preview(update, context)


@admin_only
async def sig_correct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """À l'étape de l'aperçu : un signal tapé remplace le précédent, en gardant sa photo."""
    old = context.user_data.get("sig") or {}
    try:
        s = parse_quick_signal(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"❗ {escape(str(e))}\n\n{QUICK_HELP}", parse_mode=ParseMode.HTML)
        return CONFIRM
    s.update(photo=old.get("photo"), photo_bytes=old.get("photo_bytes"), source=old.get("source", "manuel"))
    if not s.get("note") and old.get("note"):
        s["note"] = old["note"]
    context.user_data["sig"] = s
    await update.message.reply_text("✏️ Corrigé.")
    return await _show_preview(update, context)


# ---------------------------------------------------------------- modifier le signal avant de publier
EDIT_VALUE = 31
EDIT_FIELDS = {
    "entry": ("Entrée", "le prix d'entrée (ou une zone : <code>2648 2652</code>)"),
    "sl": ("SL", "le nouveau SL"),
    "tp": ("TP", "les TP séparés par des espaces (1 à 5), ex : <code>2660 2670 2680</code>"),
    "dir": ("Sens", "le sens : <code>BUY</code> ou <code>SELL</code> (le SL et le TP sont échangés)"),
    "pair": ("Actif", "l'actif, ex : <code>XAUUSD</code>"),
    "note": ("Analyse", "l'analyse (ou <code>-</code> pour l'enlever)"),
}
EDIT_MENU = InlineKeyboardMarkup(
    [[InlineKeyboardButton(EDIT_FIELDS[k][0], callback_data=f"edit:{k}") for k in ("entry", "sl", "tp")],
     [InlineKeyboardButton(EDIT_FIELDS[k][0], callback_data=f"edit:{k}") for k in ("dir", "pair", "note")],
     [InlineKeyboardButton("↩️ Retour à l'aperçu", callback_data="edit:back")]])


def edit_signal(s: dict, field: str, value: str) -> dict:
    """Signal modifié sur un champ, revalidé (lève ValueError). Photo, type d'ordre et validité sont gardés."""
    value = re.sub(r"\b(\d),(\d+)\b", r"\1.\2", value.strip())
    d = {"pair": s["pair"], "direction": s["direction"], "order_type": s.get("order_type") or "MARKET",
         "entry": s["entry_text"].replace("–", " "), "sl": s["sl_text"], "tp": list(s["tps_text"]), "note": s.get("note")}
    if field == "dir":
        word = DIR_WORDS.get(value.split()[0].upper()) if value.split() else None
        if not word:
            raise ValueError("écris BUY ou SELL")
        if word != s["direction"]:              # zones inversées : le SL devient le TP et inversement
            if len(d["tp"]) != 1:
                raise ValueError("avec plusieurs TP, renvoie le signal complet, ex : XAUUSD SELL 2650 SL 2655 TP 2640 2630")
            d["direction"], d["sl"], d["tp"] = word, d["tp"][0], [d["sl"]]
    elif field == "tp":
        d["tp"] = value
    elif field == "note":
        d["note"] = None if value in ("-", "/passer") else value[:300]
    else:
        d[field] = value
    new = build_signal(d)
    for k in ("photo", "photo_bytes", "expires_at", "source", "quick", "ref"):
        if k in s:
            new[k] = s[k]
    return new


async def sig_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(None)
    field = q.data.split(":", 1)[1]
    if field == "back" or "sig" not in context.user_data:
        return await _show_preview(update, context)
    context.user_data["edit_field"] = field
    await q.message.reply_text(f"✏️ Envoie {EDIT_FIELDS[field][1]}", parse_mode=ParseMode.HTML)
    return EDIT_VALUE


@admin_only
async def sig_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    s = context.user_data.get("sig")
    if not field or not s:
        return ConversationHandler.END
    try:
        new = edit_signal(s, field, update.message.text or "")
    except ValueError as e:
        await update.message.reply_text(f"❗ {escape(str(e))}\nRéessaie, ou choisis un autre champ :",
                                        reply_markup=EDIT_MENU, parse_mode=ParseMode.HTML)
        return EDIT_VALUE
    context.user_data["sig"] = new
    context.user_data.pop("edit_field", None)
    await update.message.reply_text(f"✅ {EDIT_FIELDS[field][0]} modifié.")
    return await _show_preview(update, context)


async def sig_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(None)
    if q.data == "ok:edit":
        await q.message.reply_text("✏️ Que veux-tu modifier ?", reply_markup=EDIT_MENU)
        return CONFIRM
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
AUTO_BE_AFTER_TP1 = os.getenv("AUTO_BE_AFTER_TP1", "true").lower() in ("1", "true", "oui", "yes")
TP_DEFAULT_PCT = float(os.getenv("TP_DEFAULT_PCT", "50") or 50)   # % clôturé par défaut (API) aux TP intermédiaires


def _remaining(row) -> float:
    return round(100 - float(row["closed_pct"] or 0), 2)


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


def pct_kb(row, n: int) -> InlineKeyboardMarkup:
    """Choix du % de la position à clôturer au TPn."""
    sid, rem = row["id"], _remaining(row)
    opts = [p for p in (25, 33, 50, 75) if p < rem]
    btns = [InlineKeyboardButton(f"{p} %", callback_data=f"p:{sid}:{n}:{p}") for p in opts]
    rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    rows.append([InlineKeyboardButton(f"💯 Tout le reste ({rem:g} %)", callback_data=f"p:{sid}:{n}:{rem:g}")])
    rows.append([InlineKeyboardButton("✏️ Autre %", callback_data=f"p:{sid}:{n}:autre"),
                 InlineKeyboardButton("↩️ Retour", callback_data=f"p:{sid}:{n}:retour")])
    return InlineKeyboardMarkup(rows)


def status_line(row) -> str:
    """Résumé d'un trade pour le panneau de contrôle admin."""
    r = row
    head = f"📊 #{r['id']} {r['pair']} {r['direction']}"
    if (r["order_type"] or "MARKET") != "MARKET":
        head += f" {r['order_type']}"
    head += f" @ {r['entry_text']}"
    if r["status"] == "PENDING":
        return head + " · ⏳ en attente"
    if r["status"] != "OPEN":
        res = "" if r["result_pips"] is None else f" → {r['result_pips']:+g} pips"
        return f"🏁 Signal #{r['id']} terminé — {r['outcome']}{res}"
    parts = [head]
    if r["tp_hit"]:
        parts.append(f"TP{r['tp_hit']} ✅")
    if r["closed_pct"]:
        parts.append(f"{r['closed_pct']:g} % fermé ({(r['realized_pips'] or 0):+g} pips sécurisés)")
        parts.append(f"reste {_remaining(r):g} %")
    if r["be"]:
        parts.append("BE 🔒")
    return " · ".join(parts)


async def _reply_in_channel(bot, row, text):
    rp = ReplyParameters(message_id=row["msg_id"], allow_sending_without_reply=True) if row["msg_id"] else None
    await bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML, reply_parameters=rp)


async def apply_action(bot, row, action: str, price: float | None = None, price_text: str | None = None,
                       pct: float | None = None) -> str:
    """Applique activate/cancel/expire, tp1..tp5 (+ % clôturé), be, sl, close. Publie et renvoie un résumé.

    Les résultats sont « pondérés » : chaque morceau fermé compte pour sa part de la position.
    Ex. 50 % fermés au TP1 (+100 pips) puis le reste au BE  →  +50 pips sur la position complète.
    """
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

    tps, tps_text = json.loads(s["tps"]), json.loads(s["tps_text"])
    pair, d, entry = s["pair"], s["direction"], s["entry"]
    remaining = _remaining(row)
    realized = float(s["realized_pips"] or 0)

    if action.startswith("tp"):
        n = int(action[2:] or 0)
        if not 1 <= n <= len(tps):
            raise ValueError(f"ce signal n'a que {len(tps)} TP")
        if n <= s["tp_hit"]:
            raise ValueError(f"TP{n} déjà annoncé")
        final = n == len(tps)
        if final or pct is None and remaining <= TP_DEFAULT_PCT:
            pct = remaining
        elif pct is None:
            pct = TP_DEFAULT_PCT
        pct = round(max(1.0, min(float(pct), remaining)), 2)
        tp_pips = calc_pips(pair, d, entry, tps[n - 1])
        gained = round(tp_pips * pct / 100, 1)
        realized = round(realized + gained, 1)
        closed = round(100 - remaining + pct, 2)
        fully = closed >= 99.99
        fields = {"tp_hit": n, "closed_pct": min(closed, 100), "realized_pips": realized}
        be_now = False
        if not fully and AUTO_BE_AFTER_TP1 and not s["be"]:
            fields["be"], be_now = 1, True
        if fully:
            fields.update(status="CLOSED", outcome=f"TP{n}", result_pips=realized, closed_at=utc_now_str())
        update_signal(s["id"], **fields)
        nxt = None
        if not fully and n < len(tps):
            nxt = {"n": n + 1, "text": tps_text[n], "pips": calc_pips(pair, d, entry, tps[n])}
        await _reply_in_channel(bot, row, T.tp_hit(
            s, n=n, pips=tp_pips, pct=pct, gained=gained, total=realized,
            remaining=round(100 - closed, 2), next_tp=nxt, be_now=be_now, closed=fully))
        return f"TP{n} : {pct:g} % fermé ({gained:+g} pips)" + (" — trade clôturé" if fully else "")

    if action == "be":
        if s["be"]:
            raise ValueError("BE déjà annoncé")
        update_signal(s["id"], be=1)
        await _reply_in_channel(bot, row, T.be_moved(s))
        return "BE publié 🔒"

    if action == "sl":
        exit_pips = 0.0 if s["be"] else calc_pips(pair, d, entry, s["sl"])
        rest = round(exit_pips * remaining / 100, 1)
        total = round(realized + rest, 1)
        if s["tp_hit"] > 0:
            outcome = f"TP{s['tp_hit']}+{'BE' if s['be'] else 'SL'}"
            text = T.closed_after_tp(s, s["tp_hit"], remaining, bool(s["be"]), rest, total)
        elif s["be"]:
            outcome, text = "BE", T.closed_at_be(s)
        else:
            outcome, text = "SL", T.sl_hit(s, total)
        update_signal(s["id"], status="CLOSED", outcome=outcome, result_pips=total, closed_at=utc_now_str())
        await _reply_in_channel(bot, row, text)
        return f"Clôture publiée ({total:+g} pips)"

    if action == "close":
        if price is None:
            raise ValueError("prix de clôture manquant")
        price_text = price_text or fmt_num(price)
        px_pips = calc_pips(pair, d, entry, price)
        total = round(realized + px_pips * remaining / 100, 1)
        update_signal(s["id"], status="CLOSED", outcome="Manuel", result_pips=total, closed_at=utc_now_str())
        await _reply_in_channel(bot, row, T.manual_close(s, price_text, px_pips, remaining, total))
        return f"Clôturé à {price_text} ({px_pips:+g} pips sur {remaining:g} %, total {total:+g} pips)"

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
    if action.startswith("tp") and row["status"] == "OPEN":
        n = int(action[2:])
        if n < len(json.loads(row["tps"])) and _remaining(row) > 0:
            await q.answer()
            await q.edit_message_text(
                f"{status_line(row)}\n\n🎯 <b>TP{n} atteint</b> — quel pourcentage de la position clôturer ?",
                parse_mode=ParseMode.HTML, reply_markup=pct_kb(row, n))
            return
    try:
        await q.answer(await apply_action(context.bot, row, action))
    except ValueError as e:
        await q.answer(str(e), show_alert=True)
    await _refresh_panel(q, int(sid))


async def _refresh_panel(q, sid: int):
    new_row = get_signal(sid)
    try:
        await q.edit_message_text(status_line(new_row), reply_markup=control_kb(new_row))
    except Exception:
        pass


@admin_only
async def on_pct_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, sid, n, choice = q.data.split(":")
    row = get_signal(int(sid))
    if not row or row["status"] != "OPEN":
        await q.answer("Ce signal n'est plus actif.", show_alert=True)
        return
    if choice == "retour":
        await q.answer()
        await _refresh_panel(q, int(sid))
        return
    if choice == "autre":
        await q.answer()
        context.user_data["await"] = ("pct", int(sid), int(n))
        await q.edit_message_text(
            f"{status_line(row)}\n\n✏️ Tape le pourcentage à clôturer au TP{n} "
            f"(entre 1 et {_remaining(row):g}), ex : <code>40</code>", parse_mode=ParseMode.HTML)
        return
    try:
        await q.answer(await apply_action(context.bot, row, f"tp{n}", pct=float(choice)))
    except ValueError as e:
        await q.answer(str(e), show_alert=True)
    await _refresh_panel(q, int(sid))


@admin_only
async def on_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponses tapées hors formulaire : % personnalisé, chiffre réel d'une annonce éco."""
    wait = context.user_data.pop("await", None)
    if not wait:
        await update.message.reply_text("Tape /aide pour voir les commandes.")
        return
    txt = update.message.text.strip()
    if wait[0] == "pct":
        _, sid, n = wait
        row = get_signal(sid)
        nums = parse_nums(txt)
        if not row or row["status"] != "OPEN":
            await update.message.reply_text("Ce signal n'est plus actif.")
            return
        if len(nums) != 1 or not 0 < nums[0][1] <= _remaining(row):
            context.user_data["await"] = wait
            await update.message.reply_text(f"❗ Tape un nombre entre 1 et {_remaining(row):g}.")
            return
        try:
            summary = await apply_action(context.bot, row, f"tp{n}", pct=nums[0][1])
        except ValueError as e:
            summary = f"❗ {e}"
        new_row = get_signal(sid)
        await update.message.reply_text(f"{summary}\n\n{status_line(new_row)}", reply_markup=control_kb(new_row))
    elif wait[0] == "news_result":
        await news_publish_result(update, context, wait[1], txt)
    elif wait[0] == "setting":
        await setting_typed(update, context, wait[1], txt)


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
        await update.message.reply_text(status_line(r), reply_markup=control_kb(r))


# ================================================================ bilans
MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre"]
_MONTH_KEYS = {m.replace("é", "e").replace("û", "u"): i + 1 for i, m in enumerate(MONTHS_FR)}

BILAN_HELP = (
    "Exemples :\n"
    "<code>/bilan</code> — aujourd'hui · <code>/bilan hier</code>\n"
    "<code>/bilan semaine</code> · <code>/bilan semaine derniere</code>\n"
    "<code>/bilan mois</code> · <code>/bilan mois dernier</code>\n"
    "<code>/bilan septembre</code> · <code>/bilan 09/2026</code>\n"
    "<code>/bilan 01/09 15/09</code> — période au choix\n"
    "<code>/bilan 01/09/2026 30/09/2026</code> · <code>/bilan annee</code>"
)


def _norm(t: str) -> str:
    return t.lower().replace("é", "e").replace("è", "e").replace("ê", "e").replace("û", "u").replace("'", "")


def _month_range(y: int, m: int):
    from datetime import date
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def _parse_day(tok: str, default_year: int):
    from datetime import date
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", tok)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", tok)
        if not m:
            return None
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else default_year
        y += 2000 if y < 100 else 0
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def resolve_period(args: list[str]):
    """Renvoie (type, date_début, date_fin incluse, libellé) ou None si la demande est illisible."""
    today = datetime.now(TZ).date()
    words = [_norm(a) for a in args if a.strip()]
    joined = " ".join(words)

    if not words or joined in ("jour", "aujourdhui", "today"):
        return "jour", today, today, f"{today:%d/%m/%Y}"
    if joined == "hier":
        d = today - timedelta(days=1)
        return "jour", d, d, f"{d:%d/%m/%Y}"
    if joined == "semaine":
        start = today - timedelta(days=today.weekday())
        return "semaine", start, start + timedelta(days=6), f"du {start:%d/%m} au {start + timedelta(days=6):%d/%m/%Y}"
    if joined in ("semaine derniere", "semaine-derniere", "semaine passee", "semaine precedente"):
        start = today - timedelta(days=today.weekday() + 7)
        end = start + timedelta(days=6)
        return "semaine", start, end, f"du {start:%d/%m} au {end:%d/%m/%Y}"
    if joined == "mois":
        a, b = _month_range(today.year, today.month)
        return "mois", a, b, f"{MONTHS_FR[a.month - 1].capitalize()} {a.year}"
    if joined in ("mois dernier", "mois-dernier", "mois passe", "mois precedent"):
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        a, b = _month_range(y, m)
        return "mois", a, b, f"{MONTHS_FR[m - 1].capitalize()} {y}"
    if joined in ("annee", "an", "year"):
        from datetime import date
        a = date(today.year, 1, 1)
        return "annee", a, date(today.year, 12, 31), f"Année {today.year}"
    if re.fullmatch(r"\d{4}", joined):
        from datetime import date
        y = int(joined)
        return "annee", date(y, 1, 1), date(y, 12, 31), f"Année {y}"

    # Mois : "septembre", "septembre 2026", "09/2026", "2026-09"
    m = re.fullmatch(r"([a-z]+)(?: (\d{4}))?", joined)
    if m and m.group(1) in _MONTH_KEYS:
        mo = _MONTH_KEYS[m.group(1)]
        y = int(m.group(2)) if m.group(2) else (today.year if mo <= today.month else today.year - 1)
        a, b = _month_range(y, mo)
        return "mois", a, b, f"{MONTHS_FR[mo - 1].capitalize()} {y}"
    m = re.fullmatch(r"(\d{1,2})[/-](\d{4})|(\d{4})-(\d{1,2})", joined)
    if m:
        mo, y = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))
        if 1 <= mo <= 12:
            a, b = _month_range(y, mo)
            return "mois", a, b, f"{MONTHS_FR[mo - 1].capitalize()} {y}"

    # Dates : "01/09" ou "01/09 15/09" (avec "au" / "-" acceptés entre les deux)
    toks = [w for w in re.split(r"\s+-\s+|\s+|\s*(?:au|a|->)\s*", joined) if w]
    if 1 <= len(toks) <= 2:
        days = [_parse_day(t, today.year) for t in toks]
        if all(days):
            a, b = days[0], days[-1]
            if b < a:
                a, b = b, a
            label = f"{a:%d/%m/%Y}" if a == b else f"du {a:%d/%m/%Y} au {b:%d/%m/%Y}"
            return ("jour" if a == b else "periode"), a, b, label
    return None


def compute_report(kind: str, start_day, end_day, label: str):
    to_utc = lambda d: datetime.combine(d, time(0, 0), tzinfo=TZ).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    start, end = to_utc(start_day), to_utc(end_day + timedelta(days=1))
    with db() as c:
        trades = [dict(r) for r in c.execute(
            "SELECT * FROM signals WHERE status='CLOSED' AND closed_at>=? AND closed_at<? ORDER BY closed_at",
            (start, end)).fetchall()]
        cancelled = c.execute(
            "SELECT COUNT(*) FROM signals WHERE status='CANCELLED' AND closed_at>=? AND closed_at<?",
            (start, end)).fetchone()[0]
    if not trades:
        return None
    assets: dict[str, dict] = {}
    for t in trades:  # résultat en R (multiple du risque) : comparable entre actifs
        risk = abs(calc_pips(t["pair"], t["direction"], t["entry"], t["sl"])) or 1
        t["r"] = round(t["result_pips"] / risk, 2)
        a = assets.setdefault(t["pair"], {"pair": t["pair"], "n": 0, "pips": 0.0, "r": 0.0})
        a["n"] += 1
        a["pips"] = round(a["pips"] + t["result_pips"], 1)
        a["r"] = round(a["r"] + t["r"], 2)
    wins = sum(t["result_pips"] > 0 for t in trades)
    losses = sum(t["result_pips"] < 0 for t in trades)
    gross_win = sum(t["r"] for t in trades if t["r"] > 0)
    gross_loss = -sum(t["r"] for t in trades if t["r"] < 0)
    stats = {
        "total": len(trades), "wins": wins, "losses": losses, "be": len(trades) - wins - losses,
        "winrate": round(100 * wins / (wins + losses)) if wins + losses else 100,
        "pips": round(sum(t["result_pips"] for t in trades), 1),
        "r": round(sum(t["r"] for t in trades), 2),
        "best": max(trades, key=lambda t: t["r"]), "worst": min(trades, key=lambda t: t["r"]),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "assets": sorted(assets.values(), key=lambda a: -a["r"]),
        "cancelled": cancelled,
    }
    return T.report(kind, label, trades, stats)


def _report_kb(kind, a, b):
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📢 Publier dans le canal", callback_data=f"rep:{a:%Y%m%d}:{b:%Y%m%d}:{kind}")]])


@admin_only
async def cmd_bilan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = resolve_period(context.args or [])
    if not res:
        await update.message.reply_text("❗ Période non reconnue.\n\n" + BILAN_HELP, parse_mode=ParseMode.HTML)
        return
    kind, a, b, label = res
    text = compute_report(kind, a, b, label)
    if not text:
        await update.message.reply_text(f"Aucun trade clôturé sur cette période ({label}).\n\n" + BILAN_HELP,
                                        parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_report_kb(kind, a, b))


@admin_only
async def on_report_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    if len(parts) == 4:
        a = datetime.strptime(parts[1], "%Y%m%d").date()
        b = datetime.strptime(parts[2], "%Y%m%d").date()
        kind = parts[3]
        if kind == "mois":
            label = f"{MONTHS_FR[a.month - 1].capitalize()} {a.year}"
        elif kind == "annee":
            label = f"Année {a.year}"
        elif a == b:
            label = f"{a:%d/%m/%Y}"
        else:
            label = f"du {a:%d/%m/%Y} au {b:%d/%m/%Y}"
        text = compute_report(kind, a, b, label)
    else:  # ancien format de bouton
        res = resolve_period([parts[1]])
        text = compute_report(*res) if res else None
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
    kind = context.job.data
    today = datetime.now(TZ).date()
    if kind == "mois":  # bilan mensuel : publié le dernier jour du mois
        if (today + timedelta(days=1)).day != 1:
            return
    res = resolve_period([kind])
    text = compute_report(*res) if res else None
    if text:
        await context.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML)
        log.info("Bilan %s publié automatiquement", kind)


# ================================================================ annonces économiques
N_TITLE, N_TIME, N_IMPACT, N_ASSETS, N_FIGS, N_NOTE, N_CONFIRM = range(20, 27)
NEWS_REMINDER_MIN = int(os.getenv("NEWS_REMINDER_MIN", "15") or 0)   # rappel X min avant (0 = désactivé)
NEWS_PRESETS = [
    ("NFP", "NFP — Emplois non agricoles US", "USD — XAUUSD, BTCUSD, indices"),
    ("CPI", "CPI — Inflation US", "USD — XAUUSD, BTCUSD, indices"),
    ("FOMC", "FOMC — Décision de taux de la Fed", "USD — tous les marchés"),
    ("POWELL", "Discours de Jerome Powell (Fed)", "USD — XAUUSD, BTCUSD"),
    ("PPI", "PPI — Prix à la production US", "USD — XAUUSD"),
    ("CLAIMS", "Inscriptions hebdo au chômage US", "USD — XAUUSD"),
    ("PIB", "PIB US (croissance)", "USD — XAUUSD, indices"),
    ("PMI", "PMI ISM", "USD — XAUUSD, indices"),
    ("BCE", "BCE — Décision de taux", "EUR — EURUSD, XAUUSD"),
]


def init_news_table():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS news(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, title TEXT, event_at TEXT,
            impact TEXT, assets TEXT, forecast TEXT, previous TEXT, note TEXT,
            msg_id INTEGER, reminded INTEGER DEFAULT 0, actual TEXT)""")


def news_view(n: dict) -> dict:
    v = dict(n)
    dt = datetime.strptime(v["event_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(TZ)
    off = int(dt.utcoffset().total_seconds() // 3600)
    v["when"] = f"{T.DAYS_FR[dt.weekday()].capitalize()} {dt:%d/%m} à {dt:%H:%M} " + ("GMT" if off == 0 else f"GMT{off:+d}")
    v["hour"] = f"{dt:%H:%M}"
    return v


@admin_only
async def news_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["news"] = {}
    btns = [InlineKeyboardButton(k, callback_data=f"nt:{i}") for i, (k, _, _) in enumerate(NEWS_PRESETS)]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    await update.message.reply_text(
        "📰 <b>Nouvelle annonce économique</b>\n\n1️⃣ Quel événement ? Choisis ou tape son nom :",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    return N_TITLE


async def _news_ask_time(msg, title):
    await msg.reply_text(
        f"✅ {escape(title)}\n\n2️⃣ Heure de l'annonce ?\nex : <code>14:30</code> ou <code>10/10 14:30</code> "
        f"(heure {TZ.key})", parse_mode=ParseMode.HTML)


async def news_title_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, title, assets = NEWS_PRESETS[int(q.data.split(":")[1])]
    context.user_data["news"].update(title=title, default_assets=assets)
    await q.edit_message_reply_markup(None)
    await _news_ask_time(q.message, title)
    return N_TIME


async def news_title_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()[:120]
    context.user_data["news"].update(title=title, default_assets="USD — XAUUSD, BTCUSD")
    await _news_ask_time(update.message, title)
    return N_TIME


async def news_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    at = parse_expiry(update.message.text)
    if not at:
        await update.message.reply_text("❗ Heure non reconnue (ou déjà passée). Ex : 14:30 ou 10/10 14:30")
        return N_TIME
    context.user_data["news"]["event_at"] = at
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔴 Fort", callback_data="ni:fort"),
                                InlineKeyboardButton("🟠 Moyen", callback_data="ni:moyen"),
                                InlineKeyboardButton("🟡 Faible", callback_data="ni:faible")]])
    await update.message.reply_text("3️⃣ Impact attendu ?", reply_markup=kb)
    return N_IMPACT


async def news_impact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["news"]["impact"] = q.data.split(":")[1]
    await q.edit_message_reply_markup(None)
    d = context.user_data["news"]["default_assets"]
    await q.message.reply_text(
        f"4️⃣ Devise / actifs concernés ?\nPar défaut : <i>{escape(d)}</i>\n(tape ton texte ou /passer)",
        parse_mode=ParseMode.HTML)
    return N_ASSETS


async def news_assets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = context.user_data["news"]
    txt = update.message.text.strip()
    n["assets"] = n["default_assets"] if txt.lower() == "/passer" else txt[:120]
    await update.message.reply_text(
        "5️⃣ Prévision / précédent ?\nex : <code>180K / 175K</code> ou <code>3,1 % / 3,2 %</code> (ou /passer)",
        parse_mode=ParseMode.HTML)
    return N_FIGS


async def news_figs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = context.user_data["news"]
    txt = update.message.text.strip()
    if txt.lower() != "/passer":
        parts = [p.strip() for p in re.split(r"\s*[/|;]\s*", txt, maxsplit=1)]
        n["forecast"] = parts[0][:40] or None
        n["previous"] = parts[1][:40] if len(parts) > 1 else None
    await update.message.reply_text(
        "6️⃣ Ton conseil pour les membres ? (ou /passer pour le conseil standard)\n"
        "ex : <i>Pas de nouvelle position 15 min avant et après. Volatilité forte attendue sur l'or.</i>",
        parse_mode=ParseMode.HTML)
    return N_NOTE


async def news_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = context.user_data["news"]
    txt = update.message.text.strip()
    n["note"] = None if txt.lower() == "/passer" else txt[:400]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Publier", callback_data="nok:publish"),
                                InlineKeyboardButton("❌ Annuler", callback_data="nok:cancel")]])
    await update.message.reply_text("👀 <b>Aperçu</b> :", parse_mode=ParseMode.HTML)
    await update.message.reply_text(T.news_post(news_view(n)), parse_mode=ParseMode.HTML, reply_markup=kb)
    return N_CONFIRM


async def news_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(None)
    n = context.user_data.pop("news", {})
    if q.data == "nok:cancel":
        await q.message.reply_text("🗑️ Annonce annulée.")
        return ConversationHandler.END
    try:
        msg = await context.bot.send_message(CHANNEL_ID, T.news_post(news_view(n)), parse_mode=ParseMode.HTML)
    except Exception as e:
        await q.message.reply_text(f"❌ Publication impossible : {e}")
        return ConversationHandler.END
    with db() as c:
        cur = c.execute(
            "INSERT INTO news(created_at,title,event_at,impact,assets,forecast,previous,note,msg_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (utc_now_str(), n["title"], n["event_at"], n["impact"], n["assets"], n.get("forecast"),
             n.get("previous"), n.get("note"), msg.message_id))
        nid = cur.lastrowid
    rappel = f"\n⏰ Rappel automatique {NEWS_REMINDER_MIN} min avant." if NEWS_REMINDER_MIN else ""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Publier le chiffre réel", callback_data=f"nr:{nid}")]])
    await q.message.reply_text(f"📢 Annonce #{nid} publiée.{rappel}\n"
                               f"Après la sortie du chiffre, appuie sur le bouton ci-dessous.", reply_markup=kb)
    return ConversationHandler.END


async def news_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("news", None)
    await update.message.reply_text("🗑️ Saisie annulée.")
    return ConversationHandler.END


@admin_only
async def on_news_result_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    nid = int(q.data.split(":")[1])
    await q.answer()
    context.user_data["await"] = ("news_result", nid)
    await q.message.reply_text(
        "✏️ Tape le chiffre réel, éventuellement suivi d'un commentaire.\n"
        "ex : <code>210K Emploi plus fort que prévu, dollar en hausse</code>", parse_mode=ParseMode.HTML)


def _num_from(text: str | None):
    m = re.search(r"-?\d+(?:[.,]\d+)?", text or "")
    return float(m.group().replace(",", ".")) if m else None


async def news_publish_result(update: Update, context: ContextTypes.DEFAULT_TYPE, nid: int, txt: str):
    with db() as c:
        row = c.execute("SELECT * FROM news WHERE id=?", (nid,)).fetchone()
    if not row:
        await update.message.reply_text("Annonce introuvable.")
        return
    m = re.match(r"\s*(\S+)\s*(.*)", txt)
    actual, comment = m.group(1), m.group(2).strip() or None
    a, f = _num_from(actual), _num_from(row["forecast"])
    verdict = None
    if a is not None and f is not None:
        verdict = "au-dessus des attentes" if a > f else ("en dessous des attentes" if a < f else "conforme aux attentes")
    rp = ReplyParameters(message_id=row["msg_id"], allow_sending_without_reply=True) if row["msg_id"] else None
    await context.bot.send_message(CHANNEL_ID, T.news_result(news_view(row), actual, verdict, comment),
                                   parse_mode=ParseMode.HTML, reply_parameters=rp)
    with db() as c:
        c.execute("UPDATE news SET actual=? WHERE id=?", (actual, nid))
    await update.message.reply_text("✅ Chiffre réel publié dans le canal.")


async def job_news_reminders(context: ContextTypes.DEFAULT_TYPE):
    if not NEWS_REMINDER_MIN:
        return
    now = datetime.now(timezone.utc)
    soon = (now + timedelta(minutes=NEWS_REMINDER_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        rows = c.execute("SELECT * FROM news WHERE reminded=0 AND event_at<=? AND event_at>?",
                         (soon, now.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    for r in rows:
        with db() as c:
            c.execute("UPDATE news SET reminded=1 WHERE id=?", (r["id"],))
        mins = max(1, round((datetime.strptime(r["event_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                             - now).total_seconds() / 60))
        rp = ReplyParameters(message_id=r["msg_id"], allow_sending_without_reply=True) if r["msg_id"] else None
        try:
            await context.bot.send_message(CHANNEL_ID, T.news_reminder(news_view(r), mins),
                                           parse_mode=ParseMode.HTML, reply_parameters=rp)
        except Exception as e:
            log.warning("Rappel annonce #%s impossible : %s", r["id"], e)


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
    txt = re.sub(r"(?i)\bS&P", "SP", txt)
    txt = re.sub(r"\b([A-Za-z]{3,5})[/-]([A-Za-z]{3,4})\b", r"\1\2", txt)   # EUR/USD, BTC-USD -> EURUSD, BTCUSD
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
    pair = I.normalize(d.get("pair") or d.get("symbol") or d.get("ticker") or "")
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
    pct = None
    if ev.startswith("tp") and d.get("close_pct") not in (None, ""):
        pct = float(d["close_pct"])
    summary = await apply_action(bot, row, ev, price, price_text, pct=pct)
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


BOT_COMMANDS = [("signal", "Nouveau signal"), ("ouverts", "Signaux en cours"), ("bilan", "Bilan / statistiques"),
                ("news", "Annonce économique"), ("reglages", "Réglages du robot"), ("accueil", "Message d'accueil du canal"),
                ("verifier", "Vérifier l'accès au canal"), ("aide", "Toutes les commandes")]


async def start_api(app):
    global _APP
    _APP = app
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        log.warning("Menu des commandes non mis à jour : %s", e)
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


# ================================================================ réglages (/reglages)
# Chaque admin règle son robot depuis Telegram. Les valeurs sont gardées dans la base et
# remplacent celles des variables d'environnement (qui restent les valeurs par défaut).
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
TZ_CHOICES = ["UTC", "Europe/Paris", "Africa/Casablanca", "Africa/Algiers", "Africa/Tunis",
              "Africa/Dakar", "Africa/Abidjan", "Europe/Brussels", "America/Montreal", "Asia/Dubai"]
TRUE_WORDS = ("1", "true", "oui", "yes")


def _env_defaults() -> dict:
    return {
        "favoris": os.getenv("QUICK_PAIRS", "XAUUSD,BTCUSD,EURUSD,GBPUSD"),
        "fuseau": os.getenv("TIMEZONE", "UTC"),
        "bilan_heure": os.getenv("DAILY_REPORT_TIME", "22:00").strip(),
        "bilan_jour": os.getenv("WEEKLY_REPORT_DAY", "vendredi").strip().lower(),
        "filigrane": os.getenv("WATERMARK", "true"),
        "be_auto": os.getenv("AUTO_BE_AFTER_TP1", "true"),
        "tp_pct": os.getenv("TP_DEFAULT_PCT", "50") or "50",
        "rappel_news": os.getenv("NEWS_REMINDER_MIN", "15") or "0",
    }


def _check_setting(key: str, raw: str) -> str:
    """Valide une valeur saisie et la renvoie normalisée (lève ValueError avec un message clair)."""
    raw = (raw or "").strip()
    if key == "favoris":
        syms = [I.normalize(p) for p in re.split(r"[,\s;]+", raw) if p.strip()]
        syms = list(dict.fromkeys(s for s in syms if s))
        if not 1 <= len(syms) <= 8:
            raise ValueError("donne entre 1 et 8 actifs, séparés par des virgules (ex : XAUUSD, US30, BTCUSD)")
        return ",".join(syms)
    if key == "fuseau":
        try:
            return ZoneInfo(raw).key
        except Exception:
            raise ValueError("fuseau inconnu (ex : Europe/Paris, Africa/Casablanca, UTC)")
    if key == "bilan_heure":
        if raw.lower() in ("off", "non", "aucun", "désactivé", "desactive", ""):
            return ""
        m = re.fullmatch(r"(\d{1,2})[:h](\d{2})", raw.lower())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError("heure invalide (ex : 22:00, ou « off » pour désactiver)")
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    if key == "bilan_jour":
        if raw.lower() not in WEEKDAYS:
            raise ValueError("jour invalide")
        return raw.lower()
    if key in ("filigrane", "be_auto"):
        return "true" if raw.lower() in TRUE_WORDS else "false"
    if key == "tp_pct":
        v = float(raw.replace(",", "."))
        if not 1 <= v <= 100:
            raise ValueError("pourcentage entre 1 et 100")
        return f"{v:g}"
    if key == "rappel_news":
        v = int(float(raw))
        if not 0 <= v <= 240:
            raise ValueError("minutes entre 0 et 240")
        return str(v)
    raise ValueError("réglage inconnu")


def get_settings() -> dict:
    vals = _env_defaults()
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        stored = dict(c.execute("SELECT key, value FROM settings").fetchall())
    for k, v in stored.items():
        if k in vals:
            vals[k] = v
    for k, v in list(vals.items()):          # une valeur d'environnement invalide ne doit pas bloquer le robot
        try:
            vals[k] = _check_setting(k, v)
        except (ValueError, TypeError):
            vals[k] = _check_setting(k, {"favoris": "XAUUSD,BTCUSD,EURUSD,GBPUSD", "fuseau": "UTC",
                                         "bilan_heure": "22:00", "bilan_jour": "vendredi", "filigrane": "true",
                                         "be_auto": "true", "tp_pct": "50", "rappel_news": "15"}[k])
    return vals


def save_setting(key: str, raw: str) -> str:
    value = _check_setting(key, raw)
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))
    return value


def apply_settings():
    """Recharge les réglages dans les variables utilisées partout dans le robot."""
    global QUICK_PAIRS, TZ, DAILY_REPORT_TIME, WEEKLY_REPORT_DAY, WATERMARK, AUTO_BE_AFTER_TP1, TP_DEFAULT_PCT
    global NEWS_REMINDER_MIN
    s = get_settings()
    QUICK_PAIRS = s["favoris"].split(",")
    TZ = ZoneInfo(s["fuseau"])
    DAILY_REPORT_TIME = s["bilan_heure"]
    WEEKLY_REPORT_DAY = s["bilan_jour"]
    WATERMARK = s["filigrane"] == "true"
    AUTO_BE_AFTER_TP1 = s["be_auto"] == "true"
    TP_DEFAULT_PCT = float(s["tp_pct"])
    NEWS_REMINDER_MIN = int(s["rappel_news"])
    return s


def schedule_reports(job_queue):
    """(Re)programme les bilans automatiques avec les réglages actuels."""
    if not job_queue:
        return
    for name in ("daily", "weekly", "monthly"):
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    if not DAILY_REPORT_TIME:
        log.info("Bilans automatiques désactivés")
        return
    # PTB : 0 = dimanche ... 6 = samedi
    h, m = map(int, DAILY_REPORT_TIME.split(":"))
    job_queue.run_daily(job_report, time(h, m, tzinfo=TZ), days=(1, 2, 3, 4, 5), data="jour", name="daily")
    days = {"dimanche": 0, "lundi": 1, "mardi": 2, "mercredi": 3, "jeudi": 4, "vendredi": 5, "samedi": 6}
    wd = days.get(WEEKLY_REPORT_DAY, 5)
    weekly_at = (datetime(2000, 1, 1, h, m) + timedelta(minutes=5)).time()
    job_queue.run_daily(job_report, weekly_at.replace(tzinfo=TZ), days=(wd,), data="semaine", name="weekly")
    monthly_at = (datetime(2000, 1, 1, h, m) + timedelta(minutes=10)).time()
    job_queue.run_daily(job_report, monthly_at.replace(tzinfo=TZ), data="mois", name="monthly")
    log.info("Bilans auto : quotidien %s (%s), hebdo le %s, mensuel le dernier jour du mois",
             DAILY_REPORT_TIME, TZ.key, WEEKLY_REPORT_DAY)


def _onoff(v: str) -> str:
    return "✅ activé" if v == "true" else "❌ désactivé"


def settings_text(s: dict) -> str:
    return (
        "⚙️ <b>Réglages du robot</b>\n\n"
        f"⭐ <b>Actifs favoris</b> : {escape(' · '.join(s['favoris'].split(',')))}\n"
        f"🕒 <b>Fuseau horaire</b> : {escape(s['fuseau'])}\n"
        f"📊 <b>Bilan automatique</b> : {('à ' + s['bilan_heure'] + ' (lun→ven)') if s['bilan_heure'] else '❌ désactivé'}\n"
        f"📅 <b>Bilan de la semaine</b> : le {s['bilan_jour']}\n"
        f"🖼 <b>Bandeau sur les photos</b> : {_onoff(s['filigrane'])}\n"
        f"🔒 <b>SL au BE après TP1</b> : {_onoff(s['be_auto'])}\n"
        f"🎯 <b>% clôturé par défaut aux TP</b> : {s['tp_pct']} %\n"
        f"📰 <b>Rappel avant une annonce</b> : {(s['rappel_news'] + ' min') if s['rappel_news'] != '0' else '❌ désactivé'}\n\n"
        "Touche un bouton pour modifier :")


def settings_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup([
        [b("⭐ Favoris", callback_data="set:ask:favoris"), b("🕒 Fuseau", callback_data="set:menu:fuseau")],
        [b("📊 Heure du bilan", callback_data="set:ask:bilan_heure"), b("📅 Jour hebdo", callback_data="set:menu:bilan_jour")],
        [b("🖼 Bandeau photo", callback_data="set:toggle:filigrane"), b("🔒 BE auto", callback_data="set:toggle:be_auto")],
        [b("🎯 % aux TP", callback_data="set:menu:tp_pct"), b("📰 Rappel news", callback_data="set:menu:rappel_news")],
        [b("✅ Terminé", callback_data="set:done")],
    ])


SETTING_MENUS = {
    "fuseau": TZ_CHOICES,
    "bilan_jour": WEEKDAYS,
    "tp_pct": ["25", "33", "50", "75", "100"],
    "rappel_news": ["0", "5", "15", "30", "60"],
}
SETTING_PROMPTS = {
    "favoris": "⭐ Envoie tes actifs favoris (1 à 8), séparés par des virgules.\nEx : <code>XAUUSD, US30, BTCUSD, EURUSD</code>",
    "bilan_heure": "📊 À quelle heure publier le bilan du jour ? Ex : <code>22:00</code>\n(ou <code>off</code> pour désactiver)",
    "fuseau": "🕒 Envoie ton fuseau horaire. Ex : <code>Europe/Paris</code>, <code>Africa/Casablanca</code>",
}


def _menu_label(key: str, v: str) -> str:
    if key == "tp_pct":
        return f"{v} %"
    if key == "rappel_news":
        return "Aucun" if v == "0" else f"{v} min"
    return v


@admin_only
async def cmd_reglages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(settings_text(get_settings()), parse_mode=ParseMode.HTML, reply_markup=settings_kb())


async def _after_change(context: ContextTypes.DEFAULT_TYPE, key: str):
    apply_settings()
    if key in ("fuseau", "bilan_heure", "bilan_jour"):
        schedule_reports(context.application.job_queue)


@admin_only
async def on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":", 3)
    action = parts[1]
    if action == "done":
        await q.answer("Réglages enregistrés ✅")
        await q.edit_message_reply_markup(None)
        return
    key = parts[2] if len(parts) > 2 else ""
    if action == "toggle":
        save_setting(key, "false" if get_settings()[key] == "true" else "true")
        await _after_change(context, key)
    elif action == "set":
        save_setting(key, parts[3])
        await _after_change(context, key)
    elif action == "menu":
        rows, opts = [], SETTING_MENUS[key]
        for i in range(0, len(opts), 2 if key == "fuseau" else 3):
            rows.append([InlineKeyboardButton(_menu_label(key, o), callback_data=f"set:set:{key}:{o}")
                         for o in opts[i:i + (2 if key == "fuseau" else 3)]])
        if key == "fuseau":
            rows.append([InlineKeyboardButton("✍️ Autre fuseau", callback_data="set:ask:fuseau")])
        rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="set:back")])
        await q.answer()
        await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
        return
    elif action == "ask":
        context.user_data["await"] = ("setting", key)
        await q.answer()
        await q.message.reply_text(SETTING_PROMPTS[key], parse_mode=ParseMode.HTML)
        return
    await q.answer("✅ Enregistré" if action != "back" else None)
    await q.edit_message_text(settings_text(get_settings()), parse_mode=ParseMode.HTML, reply_markup=settings_kb())


async def setting_typed(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, txt: str):
    try:
        save_setting(key, txt)
    except ValueError as e:
        context.user_data["await"] = ("setting", key)
        await update.message.reply_text(f"❗ {escape(str(e))}\nRéessaie, ou /reglages pour revenir au menu.",
                                        parse_mode=ParseMode.HTML)
        return
    await _after_change(context, key)
    await update.message.reply_text("✅ Enregistré.\n\n" + settings_text(get_settings()),
                                    parse_mode=ParseMode.HTML, reply_markup=settings_kb())


# ================================================================ démarrage
def main():
    if not BOT_TOKEN and os.getenv("USINE_BOT_TOKEN", "").strip():
        # Service de l'usine démarré avec « python bot.py » (commande Railway) : on bascule sur l'usine.
        import sys
        log.info("USINE_BOT_TOKEN trouvé sans BOT_TOKEN : démarrage de l'usine à robots (usine.py)")
        os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "usine.py")])
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
    init_news_table()
    apply_settings()
    try:
        import chart_reader
        log.info("📷 Lecture des captures TradingView : %s",
                 "activée" if chart_reader.available() else "désactivée (installe tesseract-ocr sur le serveur)")
    except ImportError:
        log.info("📷 Lecture des captures TradingView : désactivée (numpy / pytesseract absents)")
    app = Application.builder().token(BOT_TOKEN).post_init(start_api).post_shutdown(stop_api).build()
    private = filters.ChatType.PRIVATE

    conv = ConversationHandler(
        entry_points=[CommandHandler("signal", sig_start, filters=private),
                      MessageHandler(private & filters.TEXT & ~filters.COMMAND & filters.Regex(QUICK_RE), quick_signal),
                      MessageHandler(private & filters.PHOTO & filters.CaptionRegex(QUICK_RE), quick_signal),
                      MessageHandler(private & ~filters.FORWARDED & (filters.PHOTO | filters.Document.IMAGE), chart_signal)],
        states={
            PAIR: [CallbackQueryHandler(sig_pair_cb, pattern=r"^pair:"),
                   CallbackQueryHandler(sig_pair_category, pattern=r"^cat:"),
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
            CONFIRM: [CallbackQueryHandler(sig_confirm, pattern=r"^ok:"),
                      CallbackQueryHandler(sig_edit_field, pattern=r"^edit:"),
                      MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(QUICK_RE), sig_correct)],
            CHART_PAIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, chart_pair_text)],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_edit_value),
                         CallbackQueryHandler(sig_edit_field, pattern=r"^edit:")],
        },
        fallbacks=[CommandHandler("annuler", sig_cancel)],
        conversation_timeout=15 * 60,
    )
    app.add_handler(conv)
    txt = filters.TEXT & ~filters.COMMAND
    news_conv = ConversationHandler(
        entry_points=[CommandHandler("news", news_start, filters=private)],
        states={
            N_TITLE: [CallbackQueryHandler(news_title_cb, pattern=r"^nt:"), MessageHandler(txt, news_title_text)],
            N_TIME: [MessageHandler(txt, news_time)],
            N_IMPACT: [CallbackQueryHandler(news_impact, pattern=r"^ni:")],
            N_ASSETS: [MessageHandler(txt, news_assets), CommandHandler("passer", news_assets)],
            N_FIGS: [MessageHandler(txt, news_figs), CommandHandler("passer", news_figs)],
            N_NOTE: [MessageHandler(txt, news_note), CommandHandler("passer", news_note)],
            N_CONFIRM: [CallbackQueryHandler(news_confirm, pattern=r"^nok:")],
        },
        fallbacks=[CommandHandler("annuler", news_cancel)],
        conversation_timeout=15 * 60,
    )
    app.add_handler(news_conv)
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("aide", cmd_start, filters=private))
    app.add_handler(CommandHandler("id", cmd_id, filters=private))
    app.add_handler(CommandHandler("verifier", cmd_verifier, filters=private))
    app.add_handler(CommandHandler("accueil", cmd_accueil, filters=private))
    app.add_handler(CommandHandler("cloture", cmd_cloture, filters=private))
    app.add_handler(CommandHandler("ouverts", cmd_ouverts, filters=private))
    app.add_handler(CommandHandler("bilan", cmd_bilan, filters=private))
    app.add_handler(CommandHandler("reglages", cmd_reglages, filters=private))
    app.add_handler(CallbackQueryHandler(on_settings_button, pattern=r"^set:"))
    app.add_handler(CallbackQueryHandler(on_update_button, pattern=r"^u:"))
    app.add_handler(CallbackQueryHandler(on_report_button, pattern=r"^rep:"))
    app.add_handler(CallbackQueryHandler(on_api_validation, pattern=r"^api:"))
    app.add_handler(CallbackQueryHandler(on_pct_button, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(on_news_result_button, pattern=r"^nr:"))
    app.add_handler(MessageHandler(private & filters.FORWARDED, forwarded_from_channel))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_admin_text))

    # Bilans automatiques (PTB : 0 = dimanche ... 6 = samedi)
    if app.job_queue:
        app.job_queue.run_repeating(job_expire_orders, interval=60, first=10, name="expire_orders")
        app.job_queue.run_repeating(job_news_reminders, interval=60, first=15, name="news_reminders")
    schedule_reports(app.job_queue)

    log.info("🤖 Robot démarré — canal %s, admins %s", CHANNEL_ID, ADMIN_IDS)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        raise SystemExit(
            f"❌ Telegram refuse ce jeton ({masked}). Il a peut-être été régénéré ou révoqué : "
            "dans @BotFather → /mybots → ton robot → API Token, recopie le jeton actuel.")


if __name__ == "__main__":
    main()
