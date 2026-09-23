"""
ANONYMETRADER VIP — Robot de publication de signaux Telegram
--------------------------------------------------------------
Tu parles au robot en privé (/signal), il te pose les questions,
te montre un aperçu, puis publie le signal dans ton canal VIP
avec la photo et le modèle défini dans templates.py.
"""
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
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

PAIR, DIRECTION, ENTRY, SL, TPS, PHOTO, NOTE, CONFIRM = range(8)
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


# ================================================================ base de données
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
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


def parse_nums(text: str):
    return [(m.replace(",", "."), float(m.replace(",", "."))) for m in NUM_RE.findall(text or "")]


def local_date(utc_str: str | None = None) -> str:
    dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if utc_str else datetime.now(timezone.utc)
    dt = dt.astimezone(TZ)
    off = int(dt.utcoffset().total_seconds() // 3600)
    return dt.strftime("%d/%m/%Y • %H:%M ") + ("GMT" if off == 0 else f"GMT{off:+d}")


def signal_view(row_or_dict) -> dict:
    """Prépare les données pour templates.signal_post"""
    s = dict(row_or_dict)
    tps = json.loads(s["tps"]) if isinstance(s["tps"], str) else s["tps"]
    tps_text = json.loads(s["tps_text"]) if isinstance(s["tps_text"], str) else s["tps_text"]
    sl_pips = calc_pips(s["pair"], s["direction"], s["entry"], s["sl"])
    tp_list = [{"text": t, "pips": calc_pips(s["pair"], s["direction"], s["entry"], v)} for t, v in zip(tps_text, tps)]
    rr = round(tp_list[0]["pips"] / abs(sl_pips), 2) if sl_pips else None
    return {**s, "tps": tp_list, "sl_pips": sl_pips, "rr": rr, "date": local_date(s.get("created_at"))}


# ================================================================ photo + filigrane
async def branded_photo(bot, file_id: str, label: str):
    """Ajoute un bandeau de marque en bas de la photo. Retourne bytes ou file_id."""
    if not WATERMARK:
        return file_id
    try:
        from PIL import Image, ImageDraw, ImageFont

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
            if update.callback_query:
                await update.callback_query.answer("⛔ Accès réservé", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("⛔ Ce robot est privé.")
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
        await update.message.reply_text("⛔ Ce robot est privé.")
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
        "📊 <b>Nouveau signal</b>\n\n1/6 — Choisis l'actif ou tape-le (ex : USDJPY) :",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    return PAIR


async def _ask_direction(msg, pair):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🟢 BUY", callback_data="dir:BUY"),
                                InlineKeyboardButton("🔴 SELL", callback_data="dir:SELL")]])
    await msg.reply_text(f"✅ {pair}\n\n2/6 — Direction ?", reply_markup=kb)


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
    await q.message.reply_text(
        f"✅ {'🟢 BUY' if d == 'BUY' else '🔴 SELL'}\n\n3/6 — Prix d'entrée ?\n"
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
    await update.message.reply_text("✅ Entrée notée.\n\n4/6 — Stop Loss ?")
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
        "✅ SL noté.\n\n5/6 — Objectifs (TP) ? Sépare-les par des espaces.\n"
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
    await update.message.reply_text("✅ Objectifs notés.\n\n6/6 — Envoie la <b>photo</b> du graphique\n(ou /passer)", parse_mode=ParseMode.HTML)
    return PHOTO


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
    if s.get("photo"):
        photo = await branded_photo(bot, s["photo"], f"{s['pair']} {s['direction']}  #{sig_id}")
        return await bot.send_photo(chat_id, photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)


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
    created = utc_now_str()
    with db() as c:
        cur = c.execute(
            """INSERT INTO signals(created_at,pair,direction,entry,entry_text,sl,sl_text,tps,tps_text,note,photo)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (created, s["pair"], s["direction"], s["entry"], s["entry_text"], s["sl"], s["sl_text"],
             json.dumps(s["tps"]), json.dumps(s["tps_text"]), s.get("note"), s.get("photo")))
        sig_id = cur.lastrowid
    try:
        s["created_at"] = created
        msg = await _send_post(context.bot, CHANNEL_ID, s, sig_id)
    except Exception as e:
        with db() as c:
            c.execute("DELETE FROM signals WHERE id=?", (sig_id,))
        await q.message.reply_text(f"❌ Publication impossible : {e}\nVérifie avec /verifier.")
        return ConversationHandler.END
    update_signal(sig_id, msg_id=msg.message_id)
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


async def _reply_in_channel(context, row, text):
    rp = ReplyParameters(message_id=row["msg_id"], allow_sending_without_reply=True) if row["msg_id"] else None
    await context.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML, reply_parameters=rp)


@admin_only
async def on_update_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, sid, action = q.data.split(":")
    row = get_signal(int(sid))
    if not row or row["status"] != "OPEN":
        await q.answer("Ce signal est déjà clôturé.", show_alert=True)
        await q.edit_message_reply_markup(None)
        return
    s = dict(row)
    tps = json.loads(s["tps"])
    pair, d, entry = s["pair"], s["direction"], s["entry"]

    if action == "manual":
        await q.answer()
        await q.message.reply_text(f"Envoie : <code>/cloture {sid} PRIX</code>", parse_mode=ParseMode.HTML)
        return

    if action.startswith("tp"):
        n = int(action[2:])
        pips = calc_pips(pair, d, entry, tps[n - 1])
        final = n == len(tps)
        fields = {"tp_hit": n}
        if final:
            fields.update(status="CLOSED", outcome=f"TP{n}", result_pips=pips, closed_at=utc_now_str())
        update_signal(s["id"], **fields)
        await _reply_in_channel(context, row, T.tp_hit(s, n, pips, final))
        await q.answer(f"TP{n} publié ✅")

    elif action == "be":
        update_signal(s["id"], be=1)
        await _reply_in_channel(context, row, T.be_moved(s))
        await q.answer("BE publié 🔒")

    elif action == "sl":
        if s["tp_hit"] > 0:
            pips = calc_pips(pair, d, entry, tps[s["tp_hit"] - 1])
            outcome, text = f"TP{s['tp_hit']}", T.closed_at_be_after_tp(s, s["tp_hit"], pips)
        elif s["be"]:
            pips, outcome, text = 0.0, "BE", T.closed_at_be(s)
        else:
            pips = calc_pips(pair, d, entry, s["sl"])
            outcome, text = "SL", T.sl_hit(s, pips)
        update_signal(s["id"], status="CLOSED", outcome=outcome, result_pips=pips, closed_at=utc_now_str())
        await _reply_in_channel(context, row, text)
        await q.answer("Clôture publiée")

    new_row = get_signal(s["id"])
    try:
        if new_row["status"] == "OPEN":
            await q.edit_message_reply_markup(control_kb(new_row))
        else:
            await q.edit_message_text(
                f"🏁 Signal #{sid} clôturé — {new_row['outcome']} ({new_row['result_pips']:+g} pips)")
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
        await update.message.reply_text("Signal introuvable ou déjà clôturé.")
        return
    price_text, price = parse_nums(args[1])[0]
    pips = calc_pips(row["pair"], row["direction"], row["entry"], price)
    update_signal(row["id"], status="CLOSED", outcome="Manuel", result_pips=pips, closed_at=utc_now_str())
    await _reply_in_channel(context, row, T.manual_close(dict(row), price_text, pips))
    await update.message.reply_text(f"✅ Signal #{row['id']} clôturé à {price_text} ({pips:+g} pips).")


@admin_only
async def cmd_ouverts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("SELECT * FROM signals WHERE status='OPEN' ORDER BY id").fetchall()
    if not rows:
        await update.message.reply_text("Aucun signal en cours.")
        return
    for r in rows:
        extra = (f" · TP{r['tp_hit']} ✅" if r["tp_hit"] else "") + (" · BE 🔒" if r["be"] else "")
        await update.message.reply_text(
            f"📊 #{r['id']} {r['pair']} {r['direction']} @ {r['entry_text']}{extra}",
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


async def job_report(context: ContextTypes.DEFAULT_TYPE):
    period = context.job.data
    text = compute_report(period)
    if text:
        await context.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML)
        log.info("Bilan %s publié automatiquement", period)


# ================================================================ démarrage
def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN manquant (voir .env.example)")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS vide : envoie /start au robot pour connaître ton identifiant.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    private = filters.ChatType.PRIVATE

    conv = ConversationHandler(
        entry_points=[CommandHandler("signal", sig_start, filters=private)],
        states={
            PAIR: [CallbackQueryHandler(sig_pair_cb, pattern=r"^pair:"),
                   MessageHandler(filters.TEXT & ~filters.COMMAND, sig_pair_text)],
            DIRECTION: [CallbackQueryHandler(sig_direction, pattern=r"^dir:")],
            ENTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_entry)],
            SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_sl)],
            TPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_tps)],
            PHOTO: [MessageHandler(filters.PHOTO, sig_photo), CommandHandler("passer", sig_skip_photo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, sig_photo_missing)],
            NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sig_note), CommandHandler("passer", sig_note)],
            CONFIRM: [CallbackQueryHandler(sig_confirm, pattern=r"^ok:")],
        },
        fallbacks=[CommandHandler("annuler", sig_cancel)],
        conversation_timeout=15 * 60,
    )
    app.add_handler(conv)
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
    app.add_handler(MessageHandler(private & filters.FORWARDED, forwarded_from_channel))

    # Bilans automatiques (PTB : 0 = dimanche ... 6 = samedi)
    if DAILY_REPORT_TIME and app.job_queue:
        h, m = map(int, DAILY_REPORT_TIME.split(":"))
        app.job_queue.run_daily(job_report, time(h, m, tzinfo=TZ), days=(1, 2, 3, 4, 5), data="jour", name="daily")
        days = {"dimanche": 0, "lundi": 1, "mardi": 2, "mercredi": 3, "jeudi": 4, "vendredi": 5, "samedi": 6}
        wd = days.get(WEEKLY_REPORT_DAY, 5)
        weekly_at = (datetime(2000, 1, 1, h, m) + timedelta(minutes=5)).time()
        app.job_queue.run_daily(job_report, weekly_at.replace(tzinfo=TZ), days=(wd,), data="semaine", name="weekly")
        log.info("Bilans auto : quotidien %s, hebdo le %s", DAILY_REPORT_TIME, WEEKLY_REPORT_DAY)

    log.info("🤖 Robot démarré — canal %s, admins %s", CHANNEL_ID, ADMIN_IDS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
