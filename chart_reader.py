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
import re
import shutil
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageOps

import instruments as I


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


def _find_zone(mask: np.ndarray) -> Zone | None:
    h, w = mask.shape
    cols = mask.sum(axis=0)
    if cols.max() < max(8, h * 0.02):
        return None
    xr = _longest_run(cols >= cols.max() * 0.5)          # colonnes de la zone (les bougies sont plus courtes)
    if not xr or xr[1] - xr[0] < w * 0.015:
        return None
    rows = mask[:, xr[0]:xr[1]].mean(axis=1)
    yr = _longest_run(rows >= 0.55)
    if not yr or yr[1] - yr[0] < 6:
        return None
    return Zone(xr[0], xr[1], yr[0], yr[1])


def find_zones(rgb: np.ndarray) -> tuple[Zone, Zone, str]:
    """Renvoie (zone verte, zone rouge, 'dark'|'light')."""
    flat = rgb.reshape(-1, 3)
    bg = np.median(flat[:: max(1, len(flat) // 200000)], axis=0)       # couleur de fond dominante
    theme = "dark" if bg.mean() < 128 else "light"
    d = rgb.astype(np.int16) - bg.astype(np.int16)
    dr, dg, db = d[..., 0], d[..., 1], d[..., 2]
    # teinte LÉGÈRE seulement : les zones sont translucides, alors que les bougies et les étiquettes
    # « Target / Stop » sont de couleur pleine (écart bien plus fort) et ne doivent pas agrandir les zones
    gd, rd = dg - dr, dr - dg
    green = (gd >= 14) & (gd <= 120) & ((dg - db) >= -20)
    red = (rd >= 14) & (rd <= 120) & ((dr - db) >= 4)
    gz, rz = _find_zone(green), _find_zone(red)
    if not gz or not rz:
        raise ChartReadError("je ne trouve pas l'outil de position (zones verte et rouge)")
    if min(gz.x1, rz.x1) - max(gz.x0, rz.x0) < 5:
        raise ChartReadError("les zones verte et rouge ne sont pas alignées")
    return gz, rz, theme


# ---------------------------------------------------------------- 2. reconnaissance de texte
NUM_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")


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


def _num(text: str) -> tuple[float, int] | None:
    t = text.replace(" ", "").replace(" ", "")
    if not NUM_RE.match(t):
        return None
    t = t.replace(",", "")
    dec = len(t.split(".")[1]) if "." in t else 0
    try:
        return float(t), dec
    except ValueError:
        return None


def read_axis(img: Image.Image, theme: str, x_from: int) -> list[tuple[float, float, int]]:
    """Prix lus sur l'échelle de droite : [(y en pixels, prix, décimales)].
    Plusieurs préparations d'image sont combinées ; les lectures fausses sont écartées ensuite par fit_scale."""
    crop = img.crop((x_from, 0, img.width, img.height))
    scale = 3
    cfg = "-c tessedit_char_whitelist=0123456789.,-"
    found = []
    for prepared, psm in ((_prepare(crop, theme, scale), 6), (_prepare_min(crop, scale), 11),
                          (_prepare_white(crop, scale), 11)):
        for wd in _ocr_words(prepared, f"--psm {psm} {cfg}"):
            n = _num(wd["text"])
            if n:
                found.append(((wd["y"] + wd["h"] / 2) / scale, n[0], n[1]))
    return found


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
LABEL_RE = re.compile(r"(target|cible|objectif|take\s*profit|tp|stop(?:\s*loss)?|sl)\s*[:：]?\s*([\d][\d,\s]*(?:\.\d+)?)", re.I)


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
            n = _num(m.group(2).strip().replace(" ", ""))
            if not n:
                continue
            key = "sl" if m.group(1).lower().startswith(("stop", "sl")) else "tp"
            out.setdefault(key, n)
    return out


def _ocr_lines(img: Image.Image) -> list[str]:
    import pytesseract
    return [ln for ln in pytesseract.image_to_string(img, config="--psm 11").splitlines() if ln.strip()]


def read_pair(img: Image.Image, theme: str) -> str | None:
    """Nom de l'actif dans l'en-tête (ex : « XAUUSD · 1h · OANDA »)."""
    h = max(60, int(img.height * 0.08))
    crop = img.crop((0, 0, int(img.width * 0.7), min(img.height, h)))
    text = " ".join(_ocr_lines(_prepare(crop, theme, 3)))
    for tok in re.split(r"[^A-Za-z0-9!./]+", text):
        tok = tok.strip("./!")
        if len(tok) >= 3 and I.is_known(tok):
            return I.normalize(tok)
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
    gz, rz, theme = find_zones(rgb)

    if gz.y1 <= rz.y0 + 4 and abs(gz.y1 - rz.y0) <= 6:           # vert au-dessus → BUY
        direction, y_entry, y_tp, y_sl = "BUY", (gz.y1 + rz.y0) / 2, gz.y0, rz.y1
    elif rz.y1 <= gz.y0 + 4 and abs(rz.y1 - gz.y0) <= 6:         # vert en dessous → SELL
        direction, y_entry, y_tp, y_sl = "SELL", (rz.y1 + gz.y0) / 2, gz.y1, rz.y0
    else:
        raise ChartReadError("les zones verte et rouge ne se touchent pas")

    right = max(gz.x1, rz.x1)
    axis_from = max(right + 3, int(img.width * 0.80))
    axis = read_axis(img, theme, axis_from)
    a, b, decimals, axis_pts = fit_scale(axis)
    price = lambda y: a * y + b                                   # noqa: E731
    levels = {"entry": (y_entry, price(y_entry)), "tp": (y_tp, price(y_tp)), "sl": (y_sl, price(y_sl))}

    reading = ChartReading(direction, levels["entry"][1], levels["sl"][1], levels["tp"][1], decimals)
    px = abs(a)                                                    # prix d'un pixel
    # a) prix écrit sur l'échelle pile à la hauteur d'un niveau (étiquette colorée de l'outil sélectionné,
    #    ou graduation qui tombe exactement dessus) : on prend ce prix. Tolérance : 1,5 pixel.
    for key, (y, est) in levels.items():
        near = [(abs(yy - y), p) for yy, p in axis_pts if abs(yy - y) <= 4 and abs(p - est) <= px * 1.5]
        if near:
            setattr(reading, key, min(near)[1])
            reading.exact[key] = True
    # b) étiquettes « Target / Stop » autour de l'outil
    top, bottom = min(gz.y0, rz.y0) - 45, max(gz.y1, rz.y1) + 45
    labels = read_tool_labels(img, theme, (min(gz.x0, rz.x0) - 10, top, axis_from, bottom))
    for key in ("tp", "sl"):
        if key in labels and abs(labels[key][0] - levels[key][1]) <= px * 3:
            setattr(reading, key, labels[key][0])
            reading.exact[key] = True
            reading.decimals = max(reading.decimals, labels[key][1])

    for key in ("entry", "tp", "sl"):
        v = round(getattr(reading, key), reading.decimals)
        if not reading.exact.get(key):
            v = _snap(v, reading.decimals, px)
        setattr(reading, key, v)
    reading.pair = read_pair(img, theme)
    return reading
