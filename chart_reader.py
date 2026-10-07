"""
LECTURE D'UNE CAPTURE TRADINGVIEW (outil « Position longue / courte »)
----------------------------------------------------------------------
Sans service extérieur : traitement d'image + reconnaissance de texte locale (Tesseract).

1. Repère la zone verte (objectif) et la zone rouge (stop) de l'outil de position.
   Vert au-dessus du rouge = BUY, en dessous = SELL. La frontière entre les deux = l'entrée.
2. Lit les prix de l'échelle de droite et en déduit la règle pixel → prix.
3. Convertit les bords des zones en prix. Si les valeurs exactes sont écrites sur l'image
   (étiquettes « Target / Stop » ou prix colorés sur l'échelle), elles sont préférées.
4. Lit le nom de l'actif dans l'en-tête du graphique.

Le résultat est toujours montré en aperçu avant publication : le trader vérifie et corrige.
"""
import io
import os
import re
import shutil
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageOps

import instruments as I


# Tesseract utilise sinon tous les cœurs pour CHAQUE lecture : en parallèle, elles se gêneraient.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")


class ChartReadError(Exception):
    """Message clair pour l'utilisateur."""


def available() -> bool:
    """Tesseract (reconnaissance de texte) est-il installé sur le serveur ?"""
    return shutil.which("tesseract") is not None


@dataclass
class Zone:
    x0: int
    x1: int          # colonnes [x0, x1)
    y0: int
    y1: int          # lignes [y0, y1) : y0 = bord haut, y1 = bord bas

    @property
    def height(self):
        return self.y1 - self.y0


@dataclass
class ChartReading:
    direction: str
    entry: float
    sl: float
    tp: float
    decimals: int
    pair: str | None = None
    exact: dict = field(default_factory=dict)      # niveau -> True si lu tel quel sur l'image
    tps: list = field(default_factory=list)         # tous les TP, du plus proche au plus lointain (le dernier = tp)
    direction_guessed: bool = False                 # couleurs ambiguës et pas de ligne TP : sens supposé

    def fmt(self, v: float) -> str:
        return f"{v:.{self.decimals}f}"


# ---------------------------------------------------------------- 1. zones colorées
def _longest_run(mask_1d: np.ndarray) -> tuple[int, int] | None:
    """Plus longue suite de True : (début, fin exclue)."""
    best, start, best_len = None, None, 0
    for i, v in enumerate(np.append(mask_1d, False)):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start > best_len:
                best, best_len = (start, i), i - start
            start = None
    return best


def _close_gaps(flags: np.ndarray, gap: int, bridgeable: np.ndarray | None = None) -> np.ndarray:
    """Bouche les trous de moins de `gap` éléments (une ligne de prix ou un pointillé qui traverse la zone).
    `bridgeable` : éléments qu'on a le droit de franchir (une bande de fond vide sépare deux objets : on ne la franchit pas)."""
    out = flags.copy()
    idx = np.flatnonzero(flags)
    for a, b in zip(idx[:-1], idx[1:]):
        if 1 < b - a <= gap + 1 and (bridgeable is None or bridgeable[a + 1:b].all()):
            out[a:b] = True
    return out


def _find_zone(mask: np.ndarray, near_bg: np.ndarray | None = None, lum: np.ndarray | None = None) -> Zone | None:
    h, w = mask.shape
    cols = mask.sum(axis=0)
    if cols.max() < max(8, h * 0.02):
        return None
    xr = _longest_run(cols >= cols.max() * 0.5)          # colonnes de la zone (les bougies sont plus courtes)
    if not xr or xr[1] - xr[0] < w * 0.015:
        return None
    rows = mask[:, xr[0]:xr[1]].mean(axis=1)
    bridge = None if near_bg is None else near_bg[:, xr[0]:xr[1]].mean(axis=1) < 0.5   # ligne traversante, pas du fond
    yr = _longest_run(_close_gaps(rows >= 0.55, max(4, h // 150), bridge))
    if not yr or yr[1] - yr[0] < 6:
        return None
    y0, y1 = yr
    if lum is not None:
        # une étiquette pleine (« Target / Stop ») collée au bord est bien plus foncée/claire que la zone : on la rogne
        sub, msk = lum[y0:y1, xr[0]:xr[1]], mask[y0:y1, xr[0]:xr[1]]
        row_lum = np.array([np.median(sub[i][msk[i]]) if msk[i].any() else np.nan for i in range(y1 - y0)])
        core = np.nanmedian(row_lum)
        out = np.nan_to_num(np.abs(row_lum - core), nan=0) > 60        # lignes bien plus foncées/claires que la zone
        edge = min(len(out) // 3, 60)
        n = len(out)
        # près du bord bas : une bande d'au moins 3 lignes « étiquette » → on coupe juste avant (liseré compris)
        for i in range(n - edge, n - 2):
            if out[i:i + 3].all():
                y1 = y0 + i
                break
        for i in range(edge, 2, -1):                                    # même chose près du bord haut
            if out[i - 3:i].all():
                y0 = y0 + i
                break
    return Zone(xr[0], xr[1], y0, y1)


def _wide(mask: np.ndarray, k: int) -> np.ndarray:
    """Ne garde que les pixels qui font partie d'un segment horizontal d'au moins k pixels.
    Élimine bougies, mèches, lignes verticales et textes : il ne reste que les grandes surfaces."""
    h, w = mask.shape
    if w <= k:
        return np.zeros_like(mask)
    c = np.concatenate([np.zeros((h, 1), np.int32), np.cumsum(mask, axis=1, dtype=np.int32)], axis=1)
    full = (c[:, k:] - c[:, :-k]) == k                       # fenêtre [j, j+k) entièrement remplie
    cf = np.concatenate([np.zeros((h, 1), np.int32), np.cumsum(full, axis=1, dtype=np.int32)], axis=1)
    x = np.arange(w)
    lo, hi = np.clip(x - k + 1, 0, full.shape[1]), np.clip(x + 1, 0, full.shape[1])
    return (cf[:, hi] - cf[:, lo]) > 0


def _hue(rgb: np.ndarray) -> np.ndarray:
    """Teinte en degrés (0 = rouge, 120 = vert, 240 = bleu)."""
    f = rgb.astype(np.float32)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    mx, mn = f.max(axis=2), f.min(axis=2)
    d = np.where(mx - mn == 0, 1, mx - mn)
    h = np.where(mx == r, (g - b) / d % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4))
    return h * 60


def _hue_gap(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


GRAY = -1.0     # « teinte » d'une zone grise (sans couleur)


def find_zones(rgb: np.ndarray):
    """Trouve les deux zones de l'outil de position, quelles que soient leurs couleurs.

    On cherche deux grands rectangles translucides, de teintes différentes (ou un coloré et un gris),
    collés l'un au-dessus de l'autre. Renvoie (zone du haut, teinte, zone du bas, teinte, thème) ;
    savoir laquelle est l'objectif se décide ensuite (lignes TP, puis couleurs)."""
    h, w, _ = rgb.shape
    flat = rgb.reshape(-1, 3)
    bg = np.median(flat[:: max(1, len(flat) // 200000)], axis=0)       # couleur de fond dominante
    theme = "dark" if bg.mean() < 128 else "light"
    f = rgb.astype(np.int16)
    dist = np.abs(f - bg.astype(np.int16)).max(axis=2)
    chroma = f.max(axis=2) - f.min(axis=2)
    lum = f.mean(axis=2)
    k = max(12, int(w * 0.012))
    # zones de l'outil = couleur TRANSLUCIDE (teinte modérée). Les bougies et les étiquettes « Target / Stop »
    # sont de couleur pleine (très saturée) : on les écarte pour qu'elles ne collent pas aux zones.
    tinted = _wide((dist >= 12) & (chroma >= 10) & (chroma <= 130), k)
    # zone grise : sans teinte, nettement différente du fond, mais pas noire (cadre, textes, bougies)
    gray = _wide((dist >= 25) & (dist <= 150) & (chroma < 12), k)
    if tinted.sum() + gray.sum() < w * h * 0.002:
        raise ChartReadError("je ne trouve pas l'outil de position (zones colorées)")

    hue = _hue(rgb)
    hist, _ = np.histogram(hue[tinted], bins=36, range=(0, 360))
    centers = []
    for i in np.argsort(hist)[::-1]:
        if hist[i] < max(200, tinted.sum() * 0.03):
            break
        c = i * 10 + 5
        if all(_hue_gap(c, o) >= 40 for o in centers):
            centers.append(c)
    zones = []
    for c in centers[:6]:
        z = _find_zone(tinted & (np.abs((hue - c + 180) % 360 - 180) <= 30), dist < 12, lum)
        if z:
            zones.append((z, float(c)))
    if gray.sum() >= w * h * 0.002:
        z = _find_zone(gray, dist < 12, lum)
        if z:
            zones.append((z, GRAY))

    best = None
    for i in range(len(zones)):
        for j in range(len(zones)):
            if i == j:
                continue
            (za, ha), (zb, hb) = zones[i], zones[j]
            if ha == GRAY and hb == GRAY:
                continue
            if ha != GRAY and hb != GRAY and _hue_gap(ha, hb) < 40:
                continue
            overlap = min(za.x1, zb.x1) - max(za.x0, zb.x0)
            if overlap < 0.6 * min(za.x1 - za.x0, zb.x1 - zb.x0):
                continue
            if abs(za.y1 - zb.y0) > 6:                       # za juste au-dessus de zb
                continue
            area = za.height * (za.x1 - za.x0) + zb.height * (zb.x1 - zb.x0)
            if not best or area > best[0]:
                best = (area, za, ha, zb, hb)
    if not best:
        raise ChartReadError("je ne trouve pas l'outil de position (deux zones collées)")
    _, up, hu, low, hl = best
    return up, hu, low, hl, theme


def find_tp_lines(rgb: np.ndarray, zone: Zone) -> list[float]:
    """Lignes horizontales pleines tracées par le trader (TP1, TP2…) qui traversent le graphique
    et rejoignent la zone. Les pointillés (prix actuel, entrée) et les traits qui s'arrêtent avant
    la zone (niveaux d'analyse) ne comptent pas. Renvoie leurs hauteurs (y) dans la zone."""
    h, w, _ = rgb.shape
    x_lo, x_hi = int(w * 0.02), zone.x0 - 2
    if x_hi - x_lo < w * 0.1:
        return []
    region = rgb[:, x_lo:x_hi].astype(np.int16)
    bg = np.median(region.reshape(-1, 3)[:: max(1, region.shape[0] * region.shape[1] // 100000)], axis=0)
    ink = np.abs(region - bg.astype(np.int16)).max(axis=2) >= 40
    found = []
    for y in range(max(0, zone.y0 - 3), min(h, zone.y1 + 3)):
        row = ink[y]
        if not row[-6:].all():                               # doit rejoindre la zone
            continue
        run = len(row) - (np.flatnonzero(~row)[-1] + 1 if (~row).any() else 0)
        if run >= (x_hi - x_lo) * 0.2:
            found.append(y)
    lines, group = [], []                                     # lignes de plusieurs pixels → une seule
    for y in found:
        if group and y - group[-1] > 2:
            lines.append(sum(group) / len(group))
            group = []
        group.append(y)
    if group:
        lines.append(sum(group) / len(group))
    return lines


# ---------------------------------------------------------------- 2. reconnaissance de texte


def _ocr_words(img: Image.Image, config: str) -> list[dict]:
    import pytesseract
    data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)
    out = []
    for i, txt in enumerate(data["text"]):
        txt = (txt or "").strip()
        if txt:
            out.append({"text": txt, "x": data["left"][i], "y": data["top"][i],
                        "w": data["width"][i], "h": data["height"][i]})
    return out


def _prepare(crop: Image.Image, theme: str, scale: int = 3) -> Image.Image:
    g = ImageOps.grayscale(crop)
    if theme == "dark":
        g = ImageOps.invert(g)                        # Tesseract préfère du texte foncé sur fond clair
    g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
    return ImageOps.autocontrast(g)


def _prepare_min(crop: Image.Image, scale: int = 3) -> Image.Image:
    """Canal le plus faible de chaque pixel : le texte blanc reste clair, les fonds colorés ou sombres
    deviennent foncés. Résiste bien à la compression JPEG de Telegram."""
    a = np.asarray(crop.convert("RGB")).min(axis=2).astype(np.uint8)
    g = ImageOps.invert(Image.fromarray(a))
    g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
    return ImageOps.autocontrast(g, cutoff=1)


def _prepare_white(crop: Image.Image, scale: int = 3) -> Image.Image:
    """Texte blanc sur étiquette colorée (prix des niveaux, « Target / Stop ») → noir sur blanc."""
    a = np.asarray(crop.convert("RGB"))
    white = a.min(axis=2) >= 200
    colored = (a.max(axis=2).astype(int) - a.min(axis=2)) >= 60
    out = np.full(a.shape[:2], 255, np.uint8)
    out[white & ~colored] = 0
    g = Image.fromarray(out)
    return g.resize((g.width * scale, g.height * scale), Image.LANCZOS)


def _num(text: str, decimal_comma: bool | None = None) -> tuple[float, int] | None:
    """« 2,660.00 » → 2660.00 · « 4227,982 » (format français) → 4227.982 · « 63500 » → 63500.
    decimal_comma : None = deviner, True/False = imposé (décidé pour toute l'échelle)."""
    t = text.replace(" ", "").replace("\u2009", "").replace("\u202f", "").replace("\xa0", "")
    if not re.fullmatch(r"-?[\d.,]*\d", t) or not re.search(r"\d", t):
        return None
    if "," in t and "." in t:                       # 2,660.00 (US) ou 2.660,00 (FR/DE)
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        if decimal_comma is None:
            decimal_comma = not re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", t)
        t = t.replace(",", ".") if decimal_comma else t.replace(",", "")
    if t.count(".") > 1:
        return None
    dec = len(t.split(".")[1]) if "." in t else 0
    try:
        return float(t), dec
    except ValueError:
        return None


def _comma_is_decimal(texts: list[str]) -> bool:
    """Sur une échelle de prix, si un seul nombre a une virgule forcément décimale (« 4227,982 »),
    toutes les virgules le sont (format français)."""
    return any(re.fullmatch(r"-?\d+,\d+", t) and not re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", t)
               for t in (x.replace(" ", "") for x in texts))


def _text_lines(crop: Image.Image, min_h: int = 4, max_h: int = 40) -> list[tuple[int, int]]:
    """Lignes de texte d'une colonne (l'échelle de prix), repérées sur l'image : [(y début, y fin)]."""
    a = np.asarray(crop.convert("RGB")).astype(np.int16)
    bg = np.median(a.reshape(-1, 3), axis=0)
    ink = np.abs(a - bg.astype(np.int16)).max(axis=2) >= 50
    keep = ink.mean(axis=0) < 0.6                                  # retire bordures et cadre uni
    rows = ink[:, keep].sum(axis=1) >= 2
    out, y = [], 0
    while y < len(rows):
        if rows[y]:
            y0 = y
            while y < len(rows) and rows[y]:
                y += 1
            if min_h <= y - y0 <= max_h:
                out.append((y0, y))
        y += 1
    return out


def _dot_decimals(piece: Image.Image, n_digits: int) -> int | None:
    """Repère sur l'image le point (ou la virgule) décimal d'un prix : petit glyphe posé sur la ligne de base.
    Renvoie le nombre de chiffres après lui (0 s'il n'y en a pas), ou None si l'image est illisible.
    Sert quand la reconnaissance de texte « avale » le point (« 1.09450 » lu « 109450 »)."""
    g = np.asarray(ImageOps.grayscale(piece).resize((piece.width * 4, piece.height * 4), Image.LANCZOS)).astype(np.int16)
    ink = np.abs(g - np.median(g)) >= 60
    cols = ink.any(axis=0)
    glyphs, start = [], None
    for x in range(len(cols) + 1):
        has = x < len(cols) and cols[x]
        if has and start is None:
            start = x
        elif not has and start is not None:
            ys = np.flatnonzero(ink[:, start:x].any(axis=1))
            glyphs.append((start, x, ys[0], ys[-1]))
            start = None
    if len(glyphs) < 3 or n_digits < 2:
        return None
    heights = np.array([y1 - y0 + 1 for _a, _b, y0, y1 in glyphs])
    h_txt = float(np.median(heights[heights >= 0.5 * heights.max()]))
    # chiffres : hauteur du texte ; on écarte les bords de case ou de cadre (bien plus hauts)
    digits = [gl for gl in glyphs if 0.75 * h_txt <= gl[3] - gl[2] + 1 <= 1.2 * h_txt]
    if len(digits) < 2:
        return None
    top = float(np.median([gl[2] for gl in digits]))
    bottom = float(np.median([gl[3] for gl in digits]))
    first, last = digits[0][0], digits[-1][1]
    dots = [gl for gl in glyphs if first < gl[0] and gl[1] < last
            and gl[3] - gl[2] + 1 <= 0.35 * h_txt and gl[2] >= top + 0.55 * h_txt
            and gl[3] >= bottom - 0.25 * h_txt and gl[1] - gl[0] <= 0.6 * h_txt]
    if not dots:
        return 0
    if len(dots) > 1:
        return None
    steps = [b[0] - a[0] for a, b in zip(digits, digits[1:]) if not (a[1] <= dots[0][0] <= b[0])]
    widths = sorted(b - a for a, b, _y0, _y1 in digits)
    pitch = float(np.median(steps)) if steps else widths[len(widths) // 2] * 1.12
    if pitch <= 0:
        return None
    return max(0, int(round((last - dots[0][1]) / pitch)))


def _vote_number(variants: list[str], comma: bool | None) -> list[tuple[float, int, str]]:
    parsed = []
    for t in variants:
        t = t.strip()
        n = _num(t, comma)
        if n:
            parsed.append((n[0], n[1], t))
    return parsed


def read_axis(img: Image.Image, theme: str, x_from: int) -> tuple[list[tuple[float, float, int]], bool, int]:
    """Prix des graduations de l'échelle de droite : [(y en pixels, prix, décimales)], format de virgule, début de l'échelle.

    Chaque graduation est repérée sur l'image puis lue SÉPARÉMENT, agrandie, de plusieurs façons ; on vote.
    Le nombre de décimales étant le même sur toute l'échelle, un point perdu à la lecture est remis."""
    from concurrent.futures import ThreadPoolExecutor
    crop = img.crop((x_from, 0, img.width, img.height))
    # début réel de l'échelle : une lecture rapide de toute la colonne situe les nombres
    lefts = [wd["x"] / 3 for wd in _ocr_words(_prepare(crop, theme, 3), "--psm 6 -c tessedit_char_whitelist=0123456789.,-")
             if _num(wd["text"]) and wd["w"] / 3 >= 12]
    start = max(0, int(np.median(lefts)) - 6) if lefts else 0
    crop = crop.crop((start, 0, crop.width, crop.height))
    lines = _text_lines(crop)
    cfg = "--psm 7 -c tessedit_char_whitelist=0123456789.,-"

    def read_line(span):
        y0, y1 = span
        piece = crop.crop((0, max(0, y0 - 2), crop.width, min(crop.height, y1 + 2)))
        texts = []
        for prepared in (_prepare(piece, theme, 4), _prepare_min(piece, 5)):
            texts += [wd["text"] for wd in _ocr_words(prepared, cfg)]
        values = {v for v, _d, _t in _vote_number(texts, None)}
        if len(values) != 1 or len(texts) < 2:              # désaccord ou lecture manquante : deux lectures de plus
            for prepared in (_prepare(piece, theme, 6), _prepare_white(piece, 5)):
                texts += [wd["text"] for wd in _ocr_words(prepared, cfg)]
        digits = max((len(re.sub(r"\D", "", t)) for t in texts), default=0)
        return (y0 + y1) / 2, texts, _dot_decimals(piece, digits)

    with ThreadPoolExecutor(max_workers=6) as pool:
        raw3 = list(pool.map(read_line, lines))
    raw = [(y, texts) for y, texts, _d in raw3]
    dots = [d for _y, _t, d in raw3 if d is not None]
    dot_dec = max(set(dots), key=dots.count) if len(dots) >= 3 else None    # décimales vues sur l'image (majorité)
    comma = _comma_is_decimal([t for _y, texts in raw for t in texts])

    # format le plus courant sur l'échelle : décimales et nombre de chiffres
    shapes = {}
    for _y, texts in raw:
        for v, d, t in _vote_number(texts, comma):
            digits = len(re.sub(r"\D", "", t))
            shapes[(d, digits)] = shapes.get((d, digits), 0) + 1
    dec_main, digits_main = max(shapes, key=shapes.get) if shapes else (None, None)
    if dot_dec and dec_main == 0:            # la lecture a perdu le point partout, mais l'image le montre
        dec_main = dot_dec

    found, lefts = [], []
    for y, texts in raw:
        votes = {}
        for v, d, t in _vote_number(texts, comma):
            digits = len(re.sub(r"\D", "", t))
            if dec_main and d == 0 and digits == digits_main:       # point perdu : on le remet
                v, d = v / 10 ** dec_main, dec_main
            if dec_main is not None and (d, digits) != (dec_main, digits_main) and digits != digits_main:
                continue                                              # longueur aberrante (chiffre en trop/en moins)
            votes[(round(v, 8), d)] = votes.get((round(v, 8), d), 0) + 1
        if votes:
            (v, d), n = max(votes.items(), key=lambda kv: kv[1])
            if n >= 2 or len(votes) == 1:                             # lecture confirmée (ou seule lecture)
                found.append((y, v, d))
    axis_x = x_from + start
    return found, comma, max(x_from, axis_x)


def _other_box(c1: np.ndarray, c2: np.ndarray) -> bool:
    """Deux lignes appartiennent-elles à deux cases différentes ? On compare la TEINTE (le texte blanc
    éclaircit une case sans changer sa teinte) ; pour les cases grises, la luminosité."""
    ch1, ch2 = int(c1.max() - c1.min()), int(c2.max() - c2.min())
    if ch1 >= 30 and ch2 >= 30:
        h1, h2 = _hue(np.array([[c1]], np.uint8))[0, 0], _hue(np.array([[c2]], np.uint8))[0, 0]
        return _hue_gap(h1, h2) > 25
    if (ch1 >= 30) != (ch2 >= 30):
        return abs(ch1 - ch2) > 45
    return abs(float(c1.mean()) - float(c2.mean())) > 90


def read_level_boxes(img: Image.Image, x_from: int, comma: bool, decimals: int | None = None) -> list[tuple[float, float, int]]:
    """Cases de prix colorées sur l'échelle (niveaux de l'outil sélectionné, prix actuel…), lues une à une.
    TradingView les décale quand elles se chevauchent : on les associera aux niveaux par leur VALEUR."""
    a = np.asarray(img.crop((x_from, 0, img.width, img.height)).convert("RGB")).astype(np.int16)
    if a.shape[1] < 20:
        return []
    bg = np.median(a.reshape(-1, 3), axis=0)
    lum, bgl = a.mean(axis=2), bg.mean()
    chroma = a.max(axis=2) - a.min(axis=2)
    filled = (chroma >= 50) | (np.abs(lum - bgl) >= 60)
    keep = filled.mean(axis=0) < 0.9                         # retire le cadre uni éventuel (export TradingView)
    if keep.sum() < 15:
        return []
    fk = filled[:, keep]
    rows = _close_gaps(fk.mean(axis=1) >= 0.6, 2)                 # le texte blanc d'une case n'interrompt pas la case
    sub = a[:, keep]
    color = np.array([np.median(sub[y][fk[y]], axis=0) if fk[y].any() else bg for y in range(len(rows))])
    # découpe en cases : une case s'arrête quand la ligne n'est plus remplie OU que la couleur change
    # (TradingView empile souvent plusieurs cases : prix actuel, offre/demande, niveaux de l'outil…)
    segments, y = [], 0
    while y < len(rows):
        if not rows[y]:
            y += 1
            continue
        y0, ref = y, color[y]
        while y < len(rows) and rows[y] and not _other_box(ref, color[y]):
            y += 1
        segments.append((y0, y))
    cfg = "--psm 7 -c tessedit_char_whitelist=0123456789.,"
    left = np.asarray(img.crop((max(0, x_from - 70), 0, max(1, x_from - 3), img.height)).convert("RGB")).astype(np.int16)
    left_filled = ((left.max(axis=2) - left.min(axis=2)) >= 50) | (np.abs(left.mean(axis=2) - bg.mean()) >= 100)
    def read_box(seg):
        y0, y1 = seg
        box = img.crop((x_from, max(0, y0 - 1), img.width, min(img.height, y1 + 1)))
        # plusieurs lectures (tailles et préparations différentes) puis vote : corrige les 8 lus 6, etc.
        votes: dict[float, int] = {}
        for i, prepared in enumerate((lambda: _prepare_min(box, 4), lambda: _prepare_white(box, 4),
                                      lambda: _prepare_min(box, 3), lambda: _prepare_min(box, 5))):
            if i == 2 and len(votes) == 1 and max(votes.values()) >= 2:
                break                                          # deux lectures concordantes : suffisant
            for wd in _ocr_words(prepared(), cfg):
                n = _num(wd["text"], comma)
                if not n:
                    continue
                v, d = n
                if decimals is not None and d > decimals:       # chiffre en trop : l'échelle n'a pas cette précision
                    v = int(v * 10 ** decimals) / 10 ** decimals
                votes[v] = votes.get(v, 0) + 1
        if not votes:
            return None
        v = max(votes, key=lambda k: (votes[k], -abs(k)))
        d = decimals if decimals is not None else (len(repr(v).split(".")[1]) if "." in repr(v) else 0)
        return (y0 + y1) / 2, v, d

    todo = [(y0, y1) for y0, y1 in segments if 8 <= y1 - y0 <= 45 and left_filled[y0:y1].mean() < 0.35]
    # (une case avec une étiquette colorée juste à gauche = prix du marché : « Demande », « Offre », symbole)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as pool:
        out = [r for r in pool.map(read_box, todo) if r]
    return out


def fit_scale(points: list[tuple[float, float, int]]):
    """Droite prix = a·y + b, robuste aux erreurs de lecture (on garde la droite qui explique le plus de points)."""
    pts = sorted(set((round(y, 1), p, d) for y, p, d in points))
    if len(pts) < 3:
        raise ChartReadError("je n'arrive pas à lire l'échelle de prix à droite")
    ys = np.array([p[0] for p in pts])
    ps = np.array([p[1] for p in pts])
    best = None
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            if abs(ys[j] - ys[i]) < 5:
                continue
            a = (ps[j] - ps[i]) / (ys[j] - ys[i])
            if a >= 0:                                  # le prix doit baisser quand on descend
                continue
            b = ps[i] - a * ys[i]
            tol = abs(a) * 4                            # 4 pixels de tolérance
            inl = np.abs(a * ys + b - ps) <= tol
            if best is None or inl.sum() > best[0]:
                best = (inl.sum(), inl)
    if not best or best[0] < 3:
        raise ChartReadError("l'échelle de prix est illisible")
    inl = best[1]
    a, b = np.polyfit(ys[inl], ps[inl], 1)
    decimals = max(pts[k][2] for k in range(len(pts)) if inl[k])
    axis_pts = [(ys[k], ps[k]) for k in range(len(pts)) if inl[k]]
    return a, b, decimals, axis_pts


# ---------------------------------------------------------------- 3. valeurs exactes écrites sur l'image
LABEL_RE = re.compile(r"(target|cible|objectif|take\s*profit|tp|stop(?:\s*loss)?|sl)\s*[:：]?\s*(\d[\d,.\s]*\d)", re.I)


def read_tool_labels(img: Image.Image, theme: str, box: tuple[int, int, int, int]) -> dict:
    """Étiquettes « Target: … » / « Stop: … » affichées par TradingView quand l'outil est sélectionné."""
    x0, y0, x1, y1 = box
    crop = img.crop((max(0, x0), max(0, y0), min(img.width, x1), min(img.height, y1)))
    out = {}
    lines = []
    for prepared in (_prepare_min(crop, 3), _prepare_white(crop, 3), _prepare(crop, theme, 2)):
        lines += _ocr_lines(prepared)
    for line in lines:
        for m in LABEL_RE.finditer(line):
            n = _num(m.group(2).strip().replace(" ", ""), _comma_is_decimal([m.group(2)]) or None)
            if not n:
                continue
            key = "sl" if m.group(1).lower().startswith(("stop", "sl")) else "tp"
            out.setdefault(key, n)
    return out


def _ocr_lines(img: Image.Image) -> list[str]:
    import pytesseract
    return [ln for ln in pytesseract.image_to_string(img, config="--psm 11").splitlines() if ln.strip()]


NAME_CODES = {  # noms affichés par TradingView (français et anglais) → code
    "or": "XAU", "gold": "XAU", "argent": "XAG", "silver": "XAG", "platine": "XPT", "platinum": "XPT",
    "euro": "EUR", "dollar americain": "USD", "u.s. dollar": "USD", "us dollar": "USD", "dollar": "USD",
    "livre sterling": "GBP", "british pound": "GBP", "yen japonais": "JPY", "japanese yen": "JPY",
    "franc suisse": "CHF", "swiss franc": "CHF", "dollar canadien": "CAD", "canadian dollar": "CAD",
    "dollar australien": "AUD", "australian dollar": "AUD", "dollar neo-zelandais": "NZD",
    "new zealand dollar": "NZD", "bitcoin": "BTC", "ethereum": "ETH", "gold spot": "XAU", "silver spot": "XAG",
}


def _plain(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t).strip()


def _pair_from_names(text: str) -> str | None:
    """« Or / Dollar Américain » → XAUUSD ; « Euro / U.S. Dollar » → EURUSD."""
    m = re.search(r"([A-Za-zÀ-ÿ.\- ]{2,30})\s*/\s*([A-Za-zÀ-ÿ.\- ]{2,30})", text)
    if not m:
        return None
    codes = []
    for part in m.groups():
        p = _plain(part)
        code = next((c for name, c in sorted(NAME_CODES.items(), key=lambda kv: -len(kv[0]))
                     if p == name or p.endswith(" " + name) or p.startswith(name + " ")), None)
        if not code:
            return None
        codes.append(code)
    pair = I.normalize("".join(codes))
    return pair if I.is_known(pair) else None


def _known_symbol(text: str) -> str | None:
    for tok in re.split(r"[^A-Za-z0-9!./]+", text):
        tok = tok.strip("./!")
        if len(tok) >= 3 and I.is_known(tok):
            return I.normalize(tok)
    return None


def read_pair(img: Image.Image, theme: str, axis_from: int | None = None) -> str | None:
    """Actif : symbole dans l'en-tête (« XAUUSD · 1h ») ou sur l'échelle de prix (étiquette du dernier
    prix), sinon nom en clair (« Or / Dollar Américain »)."""
    h = max(60, int(img.height * 0.10))
    head = img.crop((0, 0, int(img.width * 0.7), min(img.height, h)))
    head_text = ""
    for prepared in (lambda: _prepare(head, theme, 3), lambda: _prepare_min(head, 3), lambda: _prepare(head, theme, 5)):
        head_text += " " + " ".join(_ocr_lines(prepared()))
        found = _known_symbol(head_text)
        if found:
            return found
    by_name = _pair_from_names(head_text)
    if by_name:
        return by_name
    if axis_from is not None:
        axis = img.crop((axis_from, 0, img.width, img.height))
        for prepared in (lambda: _prepare_min(axis, 3), lambda: _prepare_white(axis, 3)):
            found = _known_symbol(" ".join(_ocr_lines(prepared())))
            if found:
                return found
    return None


def _snap(v: float, decimals: int, px: float) -> float:
    """Niveau estimé : on prend le prix « rond » le plus proche s'il est à moins d'un demi-pixel
    (différence invisible sur la capture). 2649.98 → 2650.00, 1.08299 → 1.08300."""
    for d in range(decimals - 1, -1, -1):
        r = round(v, d)
        if abs(r - v) <= px * 0.5:
            v = r
        else:
            break
    return v


# ---------------------------------------------------------------- 4. assemblage
def read_position_tool(raw: bytes) -> ChartReading:
    if not available():
        raise ChartReadError("la lecture des captures n'est pas activée sur ce serveur (Tesseract manquant)")
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if max(img.size) > 2600:                                     # très grandes captures : on réduit
        k = 2600 / max(img.size)
        img = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
    rgb = np.asarray(img)
    up, hu, low, hl, theme = find_zones(rgb)

    # Laquelle est l'objectif ? 1) celle qui contient les lignes TP tracées ; 2) si les teintes sont nettement
    # différentes, la plus vert-bleu (vert/bleu = objectif, rouge/rose = stop ; une zone grise est neutre) ;
    # 3) sinon (ex : pêche contre gris) la plus grande, car le TP est presque toujours plus loin que le SL
    #    (sens alors signalé comme supposé).
    edge = (up.y1 + low.y0) / 2
    lines_up = [y for y in find_tp_lines(rgb, up) if y < edge - 4]
    lines_low = [y for y in find_tp_lines(rgb, low) if y > edge + 4]
    guessed = False
    if lines_up and not lines_low:
        up_is_profit = True
    elif lines_low and not lines_up:
        up_is_profit = False
    else:
        greenness = lambda hh: 0.0 if hh == GRAY else float(np.cos(np.radians(hh - 160)))  # noqa: E731
        gap = greenness(hu) - greenness(hl)
        if abs(gap) >= 0.8:
            up_is_profit = gap > 0
        else:
            up_is_profit, guessed = up.height >= low.height, True
    if up_is_profit:
        gz, rz, tp_lines = up, low, lines_up
        direction, y_entry, y_tp, y_sl = "BUY", edge, up.y0, low.y1
    else:
        gz, rz, tp_lines = low, up, lines_low
        direction, y_entry, y_tp, y_sl = "SELL", edge, low.y1, up.y0
    # TP intermédiaires : lignes à l'intérieur de la zone objectif (hors bord lointain = dernier TP)
    far = [y for y in tp_lines if abs(y - y_tp) <= 8]
    if far:                                    # la ligne du dernier TP tombe sur le bord : elle est plus précise
        y_tp = min(far, key=lambda y: abs(y - y_tp))
    mid_tps = sorted({round(y) for y in tp_lines if abs(y - y_tp) > 8}, key=lambda y: abs(y - y_entry))

    right = max(gz.x1, rz.x1)
    axis_from = max(right + 3, int(img.width * 0.80))
    axis, comma, axis_x = read_axis(img, theme, axis_from)
    a, b, decimals, axis_pts = fit_scale(axis)
    price = lambda y: a * y + b                                   # noqa: E731
    levels = {"entry": (y_entry, price(y_entry)), "tp": (y_tp, price(y_tp)), "sl": (y_sl, price(y_sl))}

    reading = ChartReading(direction, levels["entry"][1], levels["sl"][1], levels["tp"][1], decimals)
    px = abs(a)                                                    # prix d'un pixel
    tick_prices = [p for _y, p in axis_pts]
    span = max(tick_prices) - min(tick_prices)
    if any(not (min(tick_prices) - span <= v <= max(tick_prices) + span) or v <= 0 for _y, v in levels.values()):
        raise ChartReadError("la capture est trop petite ou floue pour lire les prix (envoie-la en fichier)")
    # 0) cases de prix colorées de l'outil sur l'échelle (même décalées par TradingView)
    boxes = []
    for yy, p, d in read_level_boxes(img, axis_x, comma, decimals):
        # virgule perdue à la lecture (« 417673 » pour « 41767.3 ») : on la remet si ça correspond à l'échelle
        lo_p, hi_p = min(tick_prices) - span, max(tick_prices) + span
        if not lo_p <= p <= hi_p and decimals:
            for k in range(1, decimals + 1):
                if lo_p <= p / 10 ** k <= hi_p:
                    p = round(p / 10 ** k, decimals)
                    break
        boxes.append((yy, p, d))
    for key, (y, est) in levels.items():
        near = [(abs(p - est), p, d) for yy, p, d in boxes if abs(p - est) <= px * 6 and abs(yy - y) <= max(40, img.height * 0.05)]
        if near:
            _, p, d = min(near)
            setattr(reading, key, p)
            reading.exact[key] = True
            reading.decimals = max(reading.decimals, d)
    # a) prix écrit sur l'échelle pile à la hauteur d'un niveau (étiquette colorée de l'outil sélectionné,
    #    ou graduation qui tombe exactement dessus) : on prend ce prix. Tolérance : 1,5 pixel.
    for key, (y, est) in levels.items():
        if reading.exact.get(key):
            continue
        near = [(abs(yy - y), p) for yy, p in axis_pts if abs(yy - y) <= 4 and abs(p - est) <= px * 1.5]
        if near:
            setattr(reading, key, min(near)[1])
            reading.exact[key] = True
    # b) étiquettes « Target / Stop » autour de l'outil
    top, bottom = min(gz.y0, rz.y0) - 45, max(gz.y1, rz.y1) + 45
    labels = {} if reading.exact.get("tp") and reading.exact.get("sl") else \
        read_tool_labels(img, theme, (min(gz.x0, rz.x0) - 10, top, axis_from, bottom))
    for key in ("tp", "sl"):
        if reading.exact.get(key):
            continue
        if key in labels and abs(labels[key][0] - levels[key][1]) <= px * 3:
            setattr(reading, key, labels[key][0])
            reading.exact[key] = True
            reading.decimals = max(reading.decimals, labels[key][1])

    for key in ("entry", "tp", "sl"):
        v = round(getattr(reading, key), reading.decimals)
        if not reading.exact.get(key):
            v = _snap(v, reading.decimals, px)
        setattr(reading, key, v)
    # TP intermédiaires (lignes TP1, TP2… tracées dans la zone objectif) : prix lu dans la case de l'échelle
    # à la même hauteur si elle existe (les lignes horizontales y affichent leur prix), sinon d'après l'échelle
    tps = []
    for y in mid_tps:
        est = price(y)
        near = [(abs(p - est), p) for yy, p, _d in boxes if abs(yy - y) <= 6 and abs(p - est) <= px * 6]
        v = min(near)[1] if near else _snap(round(est, reading.decimals), reading.decimals, px)
        tps.append(round(v, reading.decimals))
    tps.append(reading.tp)
    reading.tps = sorted(set(tps), reverse=(direction == "SELL"))
    reading.direction_guessed = guessed
    reading.pair = read_pair(img, theme, axis_from)
    return reading
