"""
CATALOGUE DES ACTIFS
--------------------
Tous les actifs que le robot connaît, rangés par catégorie, avec la taille de leur pip
(utilisée pour calculer les résultats et les bilans) et leurs autres noms.

👉 Pour ajouter un actif : ajoute une ligne dans la bonne catégorie.
   Le robot accepte aussi les symboles hors catalogue, mais il calcule alors les pips
   comme du Forex (0.0001), ce qui peut fausser les bilans.
"""
import re

FX_CCY = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]
FX_EXOTIC = ["USDTRY", "USDZAR", "USDMXN", "USDSGD", "USDHKD", "USDNOK", "USDSEK", "USDDKK", "USDPLN",
             "USDHUF", "USDCZK", "USDCNH", "EURTRY", "EURZAR", "EURNOK", "EURSEK", "EURPLN", "EURHUF",
             "GBPZAR", "GBPNOK", "GBPSEK", "GBPSGD"]


def _forex() -> dict:
    out = {}
    for i, a in enumerate(FX_CCY):
        for b in FX_CCY[i + 1:]:
            pair = a + b
            out[pair] = 0.01 if b == "JPY" else 0.0001
    for p in FX_EXOTIC:
        out[p] = 0.01 if p.endswith("HUF") else 0.0001
    return out


# symbole -> taille d'un pip, par catégorie (l'ordre est celui des boutons)
CATEGORIES = {
    "forex": ("💱 Forex", _forex()),
    "metaux": ("🥇 Métaux", {"XAUUSD": 0.1, "XAGUSD": 0.01, "XAUEUR": 0.1, "XPTUSD": 0.1, "XPDUSD": 0.1,
                            "XAGEUR": 0.01, "COPPER": 0.001}),
    "indices": ("📈 Indices", {"US30": 1.0, "NAS100": 1.0, "US500": 0.1, "US2000": 0.1, "GER40": 1.0,
                              "UK100": 1.0, "FRA40": 1.0, "EU50": 1.0, "ESP35": 1.0, "ITA40": 1.0,
                              "JPN225": 1.0, "HK50": 1.0, "AUS200": 1.0, "CHINA50": 1.0, "VIX": 0.01, "DXY": 0.01}),
    "energies": ("🛢 Énergies", {"USOIL": 0.01, "UKOIL": 0.01, "NGAS": 0.001}),
    "crypto": ("₿ Crypto", {"BTCUSD": 1.0, "ETHUSD": 0.1, "SOLUSD": 0.01, "BNBUSD": 0.1, "XRPUSD": 0.0001,
                           "ADAUSD": 0.0001, "DOGEUSD": 0.00001, "LTCUSD": 0.01, "DOTUSD": 0.001,
                           "AVAXUSD": 0.01, "LINKUSD": 0.001, "TONUSD": 0.001, "TRXUSD": 0.00001,
                           "POLUSD": 0.0001, "SHIBUSD": 0.00000001, "BCHUSD": 0.1, "XLMUSD": 0.00001,
                           "UNIUSD": 0.001, "ATOMUSD": 0.001, "NEARUSD": 0.001, "SUIUSD": 0.0001,
                           "PEPEUSD": 0.0000000001, "BTCEUR": 1.0, "ETHEUR": 0.1, "ETHBTC": 0.00001}),
}

# autres noms (courtiers, langage courant) -> symbole du catalogue
ALIASES = {
    "GOLD": "XAUUSD", "SILVER": "XAGUSD", "PLATINUM": "XPTUSD", "PALLADIUM": "XPDUSD",
    "DOW": "US30", "DJ30": "US30", "DJI": "US30", "WS30": "US30", "DOWJONES": "US30", "US30CASH": "US30",
    "NAS": "NAS100", "NASDAQ": "NAS100", "NDX": "NAS100", "US100": "NAS100", "USTEC": "NAS100", "NQ": "NAS100",
    "SP500": "US500", "SPX": "US500", "SPX500": "US500", "SNP500": "US500", "ES": "US500",
    "RUSSELL": "US2000", "RUT": "US2000", "US2K": "US2000",
    "DAX": "GER40", "DAX40": "GER40", "DE40": "GER40", "GER30": "GER40", "DE30": "GER40",
    "FTSE": "UK100", "FTSE100": "UK100", "CAC": "FRA40", "CAC40": "FRA40", "FR40": "FRA40",
    "STOXX50": "EU50", "EUSTX50": "EU50", "SX5E": "EU50", "IBEX": "ESP35", "IBEX35": "ESP35",
    "NIKKEI": "JPN225", "NIK225": "JPN225", "JP225": "JPN225", "HSI": "HK50", "HANGSENG": "HK50",
    "ASX200": "AUS200", "DOLLARINDEX": "DXY", "USDX": "DXY",
    "WTI": "USOIL", "OIL": "USOIL", "CRUDE": "USOIL", "XTIUSD": "USOIL", "CL": "USOIL",
    "BRENT": "UKOIL", "XBRUSD": "UKOIL", "NATGAS": "NGAS", "XNGUSD": "NGAS", "GAS": "NGAS",
    "BITCOIN": "BTCUSD", "BTC": "BTCUSD", "XBTUSD": "BTCUSD", "ETHEREUM": "ETHUSD", "ETH": "ETHUSD",
    "SOLANA": "SOLUSD", "SOL": "SOLUSD", "RIPPLE": "XRPUSD", "XRP": "XRPUSD", "MATICUSD": "POLUSD",
}

PIP_SIZES = {sym: pip for _, (_, items) in CATEGORIES.items() for sym, pip in items.items()}
_CRYPTO_BASES = {s[:-3]: pip for s, pip in CATEGORIES["crypto"][1].items() if s.endswith("USD")}
_BROKER_SUFFIX = re.compile(r"(?:[._-]?(?:M|MINI|MICRO|PRO|RAW|ECN|STD|CASH|SPOT|C|I|X|Z)|\+|#)$")


def normalize(symbol: str) -> str:
    """'gold', 'EUR/USD', 'XAUUSD.m', 'NASDAQ', 'BTCUSDT' -> 'XAUUSD', 'EURUSD', 'XAUUSD', 'NAS100', 'BTCUSDT'."""
    s = str(symbol or "").upper().strip()
    s = s.replace("S&P", "SP")
    s = re.sub(r"\.[A-Z0-9]{1,5}$", "", s)        # suffixe courtier après un point : XAUUSD.m, EURUSD.pro
    s = re.sub(r"[^A-Z0-9]", "", s)[:15]
    for cand in (s, _BROKER_SUFFIX.sub("", s)):
        if cand in PIP_SIZES:
            return cand
        if cand in ALIASES:
            return ALIASES[cand]
    return s


def is_known(symbol: str) -> bool:
    return pip_size(symbol) is not None


def pip_size(symbol: str) -> float | None:
    """Taille du pip si l'actif est connu, sinon None."""
    s = normalize(symbol)
    if s in PIP_SIZES:
        return PIP_SIZES[s]
    m = re.fullmatch(r"([A-Z]{2,6}?)(USDT|USDC|USD|EUR)", s)      # crypto en USDT/USDC : même pip que la base
    if m and m.group(1) in _CRYPTO_BASES:
        return _CRYPTO_BASES[m.group(1)]
    return None


def category_symbols(key: str) -> list[str]:
    return list(CATEGORIES[key][1])
