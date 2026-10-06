"""
=====================================================================
  MODÈLES DES MESSAGES — ANONYMETRADER VIP
  👉 C'est LE fichier à modifier pour changer le style de tes posts.
  Formatage : HTML Telegram (<b>gras</b>, <i>italique</i>, <code>code</code>)
=====================================================================
"""
import os
from html import escape as _html_escape


def escape(text) -> str:
    return _html_escape(str(text), quote=False)

# --- Identité du canal ------------------------------------------------
# BRAND peut être changé par la variable d'environnement BRAND (utilisé par l'usine à robots)
BRAND = os.getenv("BRAND", "").strip() or "ANONYMETRADER VIP"
SEPARATOR = "━━━━━━━━━━━━━━━━━━"
WATERMARK_TEXT = BRAND   # texte ajouté en bas des photos

# Rappel de gestion du risque ajouté sous chaque signal
FOOTER = (
    "⚠️ <i>Risque max 1–2 % du capital par trade.\n"
    "🔒 SL au point d'entrée (BE) dès que le TP1 est touché.</i>"
)

# --- Taille d'un pip par actif (utilisé pour calculer les résultats) --
# La clé est le début du symbole. Ajoute tes actifs ici si besoin.
PIP_SIZES = {
    "XAU": 0.1,      # Or : 1 pip = 0.10 $
    "XAG": 0.01,     # Argent
    "BTC": 1.0,      # Bitcoin : 1 "pip" = 1 $
    "ETH": 0.1,
    "US30": 1.0, "NAS100": 1.0, "US100": 1.0, "GER40": 1.0, "DE40": 1.0,
    "US500": 0.1, "SPX": 0.1,
}
DEFAULT_PIP = 0.0001   # paires Forex classiques (EURUSD, GBPUSD...)
JPY_PIP = 0.01         # paires en JPY


# Libellés des types d'ordre
ORDER_LABELS = {
    ("BUY", "MARKET"): "🟢 <b>ACHAT (BUY)</b> au marché",
    ("SELL", "MARKET"): "🔴 <b>VENTE (SELL)</b> au marché",
    ("BUY", "LIMIT"): "🟢 <b>BUY LIMIT</b> (ordre en attente)",
    ("SELL", "LIMIT"): "🔴 <b>SELL LIMIT</b> (ordre en attente)",
    ("BUY", "STOP"): "🟢 <b>BUY STOP</b> (ordre en attente)",
    ("SELL", "STOP"): "🔴 <b>SELL STOP</b> (ordre en attente)",
}


def _kind(s: dict) -> str:
    ot = s.get("order_type") or "MARKET"
    return s["direction"] if ot == "MARKET" else f"{s['direction']} {ot}"


def _sign(p: float) -> str:
    return f"+{p:g}" if p > 0 else f"{p:g}"


# =====================================================================
#  1) LE SIGNAL
# =====================================================================
def signal_post(s: dict) -> str:
    """s contient : id, pair, direction, entry_text, sl_text, sl_pips,
    tps (liste de dicts {text, pips}), rr, note, date"""
    ot = s.get("order_type") or "MARKET"
    arrow = ORDER_LABELS[(s["direction"], ot)]
    pending = ot != "MARKET"

    lines = [
        f"💎 <b>{BRAND}</b>",
        f"📊 <b>SIGNAL #{s['id']}</b>",
        SEPARATOR,
        "",
        f"{arrow} — <b>{escape(s['pair'])}</b>",
        "",
        f"📍 <b>{'Prix de l’ordre' if pending else 'Entrée'} :</b> <code>{s['entry_text']}</code>",
        f"🛑 <b>Stop Loss :</b> <code>{s['sl_text']}</code>  <i>({_sign(-abs(s['sl_pips']))} pips)</i>",
        "",
    ]
    for i, tp in enumerate(s["tps"], 1):
        lines.append(f"🎯 <b>TP{i} :</b> <code>{tp['text']}</code>  <i>({_sign(tp['pips'])} pips)</i>")
    lines.append("")
    if pending:
        lines.append(f"⏳ <b>Validité :</b> {'jusqu’au ' + s['expires'] if s.get('expires') else 'jusqu’à annulation'}")
    if s.get("rr"):
        lines.append(f"⚖️ <b>Ratio R:R :</b> 1:{s['rr']}")
    if s.get("note"):
        lines.append(f"📝 <b>Analyse :</b> {escape(s['note'])}")
    lines += ["", FOOTER, SEPARATOR, f"🕒 {s['date']}"]
    return "\n".join(lines)


# =====================================================================
#  2) LES MISES À JOUR (postées en réponse au signal d'origine)
# =====================================================================
def order_triggered(s: dict) -> str:
    return (
        f"⚡ <b>ORDRE DÉCLENCHÉ</b>\n"
        f"<b>{escape(s['pair'])} {_kind(s)}</b> — Signal #{s['id']}\n"
        f"Entrée à <code>{s['entry_text']}</code> : le trade est actif, SL et TP en place ✅"
    )


def order_cancelled(s: dict, expired: bool = False) -> str:
    why = "Ordre expiré sans être déclenché" if expired else "Ordre retiré avant déclenchement"
    return (
        f"🚫 <b>ORDRE ANNULÉ</b>\n"
        f"<b>{escape(s['pair'])} {_kind(s)}</b> — Signal #{s['id']}\n"
        f"{why} : supprimez-le de votre plateforme. Aucun impact sur les résultats."
    )


def tp_hit(s: dict, n: int, pips: float, pct: float, gained: float, total: float,
           remaining: float, next_tp: dict | None, be_now: bool, closed: bool) -> str:
    """pips = gain du TP sur 100 % · gained = part sécurisée (pct %) · total = cumul sécurisé."""
    txt = (
        f"🎯✅ <b>TP{n} ATTEINT !</b>  <b>{_sign(pips)} pips</b>\n"
        f"<b>{escape(s['pair'])} {s['direction']}</b> — Signal #{s['id']}\n\n"
    )
    if closed:
        if pct >= 99.99:
            txt += f"💰 Position fermée à 100 % → <b>{_sign(gained)} pips</b>\n"
        else:
            txt += f"💰 Les {pct:g} % restants sont fermés → <b>{_sign(gained)} pips</b>\n"
        txt += f"\n🏆 <b>Trade clôturé.</b> Résultat total : <b>{_sign(total)} pips</b>"
        return txt
    txt += (
        f"💰 <b>{pct:g} %</b> de la position clôturés → <b>{_sign(gained)} pips</b> sécurisés\n"
        f"📊 Total sécurisé : <b>{_sign(total)} pips</b>\n"
    )
    if be_now:
        txt += "🔒 SL déplacé au point d'entrée (BE) : le reste est sans risque\n"
    if next_tp:
        txt += (f"\n🚀 On continue vers <b>TP{next_tp['n']}</b> (<code>{next_tp['text']}</code> · "
                f"{_sign(next_tp['pips'])} pips) avec les <b>{remaining:g} %</b> restants")
    return txt


def be_moved(s: dict) -> str:
    return (
        f"🔒 <b>SÉCURISEZ LA POSITION</b>\n"
        f"<b>{escape(s['pair'])} {s['direction']}</b> — Signal #{s['id']}\n"
        f"Stop Loss déplacé au point d'entrée ({s['entry_text']}). Trade sans risque ✅"
    )


def closed_after_tp(s: dict, tp_n: int, remaining: float, at_be: bool, rest_pips: float, total: float) -> str:
    how = "au point d'entrée (BE)" if at_be else "au Stop Loss"
    icon = "✅" if total > 0 else ("⚪" if total == 0 else "❌")
    return (
        f"🏁 <b>TRADE CLÔTURÉ</b> {icon}\n"
        f"<b>{escape(s['pair'])} {s['direction']}</b> — Signal #{s['id']}\n"
        f"Les {remaining:g} % restants sont sortis {how} ({_sign(rest_pips)} pips) après le TP{tp_n}.\n"
        f"💰 Résultat total : <b>{_sign(total)} pips</b>"
    )


def closed_at_be(s: dict) -> str:
    return (
        f"⚪ <b>CLÔTURÉ AU BREAK-EVEN</b>\n"
        f"<b>{escape(s['pair'])} {s['direction']}</b> — Signal #{s['id']}\n"
        f"Aucune perte, capital protégé 🛡️"
    )


def sl_hit(s: dict, pips: float) -> str:
    return (
        f"❌ <b>STOP LOSS TOUCHÉ</b>\n"
        f"<b>{escape(s['pair'])} {s['direction']}</b> — Signal #{s['id']}\n"
        f"Résultat : <b>{_sign(pips)} pips</b>\n\n"
        f"<i>Les pertes font partie du trading. Gestion du risque respectée, "
        f"on reste discipliné 💪</i>"
    )


def manual_close(s: dict, price_text: str, pips: float, remaining: float = 100, total: float | None = None) -> str:
    total = pips if total is None else total
    emoji = "✅" if total > 0 else ("⚪" if total == 0 else "❌")
    part = "" if remaining >= 99.99 else f" (les {remaining:g} % restants)"
    return (
        f"✋ <b>CLÔTURE MANUELLE</b> {emoji}\n"
        f"<b>{escape(s['pair'])} {s['direction']}</b> — Signal #{s['id']}\n"
        f"Fermé à <code>{price_text}</code>{part} → {_sign(pips)} pips\n"
        f"💰 Résultat total : <b>{_sign(total)} pips</b>"
    )


# =====================================================================
#  3) LES BILANS
# =====================================================================
PERIOD_TITLES = {
    "jour": "📅 BILAN DU JOUR",
    "semaine": "📆 BILAN DE LA SEMAINE",
    "mois": "🗓️ BILAN DU MOIS",
    "annee": "🏆 BILAN DE L'ANNÉE",
    "periode": "📊 BILAN DE LA PÉRIODE",
}
MAX_TRADE_LINES = 25   # au-delà, la liste détaillée est raccourcie (limite Telegram)


def report(period: str, label: str, trades: list, stats: dict) -> str:
    lines = [f"💎 <b>{BRAND}</b>", f"<b>{PERIOD_TITLES.get(period, PERIOD_TITLES['periode'])}</b> — {label}",
             SEPARATOR, ""]
    shown = trades if len(trades) <= MAX_TRADE_LINES else trades[-MAX_TRADE_LINES:]
    if len(shown) < len(trades):
        lines.append(f"<i>… {len(trades) - len(shown)} trades plus anciens non détaillés</i>")
    for t in shown:
        icon = "✅" if t["result_pips"] > 0 else ("⚪" if t["result_pips"] == 0 else "❌")
        lines.append(
            f"{icon} #{t['id']} {escape(t['pair'])} {t['direction']} → "
            f"<b>{_sign(t['r'])}R</b> ({_sign(t['result_pips'])} pips) <i>{t['outcome']}</i>"
        )
    lines += ["", "<b>Par actif</b>"]
    for a in stats["assets"]:
        lines.append(f"• {escape(a['pair'])} : {a['n']} trade{'s' if a['n'] > 1 else ''} → "
                     f"<b>{_sign(a['pips'])} pips</b> ({_sign(a['r'])}R)")
    lines += [
        "",
        SEPARATOR,
        f"📈 Trades : <b>{stats['total']}</b>  |  ✅ {stats['wins']}  ❌ {stats['losses']}  ⚪ {stats['be']}",
        f"🎯 Taux de réussite : <b>{stats['winrate']} %</b>",
        f"💰 Performance : <b>{_sign(stats['r'])}R</b>",
    ]
    if stats.get("profit_factor"):
        lines.append(f"⚖️ Profit factor : <b>{stats['profit_factor']}</b>")
    if stats["total"] > 1:
        b, w = stats["best"], stats["worst"]
        lines.append(f"🥇 Meilleur : #{b['id']} {escape(b['pair'])} {_sign(b['r'])}R · "
                     f"📉 Pire : #{w['id']} {escape(w['pair'])} {_sign(w['r'])}R")
    if stats.get("cancelled"):
        lines.append(f"🚫 Ordres annulés/expirés (non comptés) : {stats['cancelled']}")
    lines += [
        "<i>1R = le risque pris sur un trade (distance au Stop Loss). Les pips sont pondérés par les "
        "clôtures partielles.</i>",
        "",
        "<i>Résultats passés ≠ résultats futurs. Tradez avec un risque maîtrisé.</i>",
    ]
    return "\n".join(lines)


# =====================================================================
#  ANNONCES ÉCONOMIQUES (/news)
# =====================================================================
DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
IMPACT = {"fort": "🔴 FORT", "moyen": "🟠 MOYEN", "faible": "🟡 FAIBLE"}
NEWS_ADVICE = {
    "fort": "Pas de nouvelle position 15 min avant et après l'annonce. Sécurisez vos trades ouverts "
            "(SL au BE ou prise de profits partielle). Spreads et volatilité très élevés.",
    "moyen": "Prudence autour de l'annonce : réduisez la taille des positions et évitez d'entrer juste avant.",
    "faible": "Impact limité attendu, mais restez attentifs aux mouvements brusques.",
}


def news_post(n: dict) -> str:
    lines = [
        f"📰 <b>ANNONCE ÉCONOMIQUE</b> — {BRAND}",
        SEPARATOR,
        "",
        f"🗓️ <b>{escape(n['title'])}</b>",
        f"🕒 {n['when']}",
        f"💥 Impact : <b>{IMPACT.get(n['impact'], n['impact'])}</b>",
        f"💱 Concerné : {escape(n['assets'])}",
    ]
    if n.get("forecast") or n.get("previous"):
        lines.append("")
        if n.get("forecast"):
            lines.append(f"📊 Prévision : <b>{escape(n['forecast'])}</b>")
        if n.get("previous"):
            lines.append(f"📁 Précédent : {escape(n['previous'])}")
    lines += ["", f"⚠️ <i>{escape(n.get('note') or NEWS_ADVICE.get(n['impact'], NEWS_ADVICE['moyen']))}</i>",
              SEPARATOR]
    return "\n".join(lines)


def news_reminder(n: dict, minutes: int) -> str:
    return (
        f"⏰ <b>DANS {minutes} MIN : {escape(n['title'])}</b> ({n['hour']})\n"
        f"💥 Impact {IMPACT.get(n['impact'], n['impact'])} — {escape(n['assets'])}\n"
        f"🔒 Sécurisez vos positions, pas de nouvelle entrée avant la sortie du chiffre."
    )


def news_result(n: dict, actual: str, verdict: str | None, comment: str | None) -> str:
    lines = [f"📢 <b>RÉSULTAT : {escape(n['title'])}</b>", "",
             f"✅ Réel : <b>{escape(actual)}</b>"]
    if n.get("forecast"):
        lines.append(f"📊 Prévision : {escape(n['forecast'])}")
    if n.get("previous"):
        lines.append(f"📁 Précédent : {escape(n['previous'])}")
    if verdict:
        lines += ["", f"➡️ Chiffre <b>{verdict}</b>"]
    if comment:
        lines += ["", f"📝 {escape(comment)}"]
    lines += ["", "<i>Attendez que la volatilité retombe avant de prendre position.</i>"]
    return "\n".join(lines)


# =====================================================================
#  4) MESSAGE D'ACCUEIL (épinglé avec /accueil)
# =====================================================================
WELCOME = f"""💎 <b>BIENVENUE DANS {BRAND}</b> 💎
{SEPARATOR}

Tu fais maintenant partie du cercle privé 🔐

📊 <b>Ce que tu reçois ici</b>
• Signaux de scalping sur l'<b>Or (XAUUSD)</b> et le <b>Bitcoin (BTCUSD)</b>
• Entrée, Stop Loss et objectifs (TP) clairs à chaque signal
• Suivi en direct : TP atteints, BE, SL — gains <u>et</u> pertes
• Bilan quotidien et hebdomadaire transparent

📌 <b>Comment lire un signal</b>
🟢 BUY = achat · 🔴 SELL = vente
⚡ Au marché = on entre tout de suite
📥 LIMIT / 🚀 STOP = ordre en attente, à placer sur ta plateforme
📍 Entrée · 🛑 Stop Loss · 🎯 Objectifs (TP1, TP2, TP3)

🛡️ <b>Règles de gestion du risque</b>
1️⃣ Ne risque jamais plus de <b>1 à 2 %</b> de ton capital par trade
2️⃣ Place <b>toujours</b> ton Stop Loss
3️⃣ Au TP1 : prends une partie des gains et mets le SL au BE
4️⃣ Pas de signal ? Pas de trade. La patience paie.

⚠️ <i>Avertissement : le trading comporte un risque élevé de perte en capital.
Les signaux partagés ici sont à but éducatif et ne constituent pas un conseil
en investissement personnalisé. Tu restes seul responsable de tes décisions.</i>

{SEPARATOR}
Bon trading à tous 🚀"""
