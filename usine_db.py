"""
USINE À ROBOTS — registre des clients
-------------------------------------
Une ligne par client : son jeton, son canal, sa marque et son abonnement.
Les signaux de chaque client ne sont PAS ici : chaque robot client a sa
propre base (DATA_DIR/clients/<id>/signals.db). L'isolation est physique,
aucune requête ne peut lire les données d'un autre client.
"""
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

STATUS_ACTIVE = "ACTIF"        # essai ou abonnement en cours
STATUS_SUSPENDED = "SUSPENDU"  # coupé à la main par le propriétaire de l'usine


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class ClientStore:
    def __init__(self, path: str):
        self.path = path
        with self._db() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS clients(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,          -- identifiant Telegram du client
                    bot_token TEXT UNIQUE NOT NULL,
                    bot_username TEXT,
                    channel_id TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    timezone TEXT DEFAULT 'UTC',
                    status TEXT DEFAULT 'ACTIF',
                    paid_until TEXT NOT NULL,           -- fin de l'essai ou de l'abonnement (UTC)
                    reminded_at TEXT,                   -- dernier rappel d'expiration envoyé
                    created_at TEXT NOT NULL)"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS payments(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER NOT NULL,
                    method TEXT NOT NULL,               -- STARS ou MANUEL
                    amount INTEGER,
                    days INTEGER NOT NULL,
                    charge_id TEXT UNIQUE,              -- évite de créditer deux fois le même paiement
                    created_at TEXT NOT NULL)"""
            )

        with self._db() as c:      # parrainage (ajouté après coup : migration douce)
            cols = {r[1] for r in c.execute("PRAGMA table_info(clients)")}
            if "referred_by" not in cols:
                c.execute("ALTER TABLE clients ADD COLUMN referred_by INTEGER")   # parrain (identifiant Telegram)
            if "price_stars" not in cols:
                c.execute("ALTER TABLE clients ADD COLUMN price_stars INTEGER")   # tarif bloqué (offre fondateurs)
            c.execute("""CREATE TABLE IF NOT EXISTS referral_codes(
                    owner_id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS pending_referrals(
                    user_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, created_at TEXT NOT NULL)""")

    def _db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------ lecture
    def get(self, client_id: int) -> dict | None:
        with self._db() as c:
            r = c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        return dict(r) if r else None

    def by_owner(self, owner_id: int) -> list[dict]:
        with self._db() as c:
            rows = c.execute("SELECT * FROM clients WHERE owner_id=? ORDER BY id", (owner_id,)).fetchall()
        return [dict(r) for r in rows]

    def all(self) -> list[dict]:
        with self._db() as c:
            rows = c.execute("SELECT * FROM clients ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def token_exists(self, token: str) -> bool:
        with self._db() as c:
            return c.execute("SELECT 1 FROM clients WHERE bot_token=?", (token,)).fetchone() is not None

    # ------------------------------------------------------------ écriture
    def add(self, owner_id: int, bot_token: str, bot_username: str, channel_id: str, brand: str,
            trial_days: int, tz: str = "UTC", now: datetime | None = None, referred_by: int | None = None) -> int:
        now = now or utc_now()
        with self._db() as c:
            cur = c.execute(
                """INSERT INTO clients(owner_id, bot_token, bot_username, channel_id, brand, timezone,
                                       paid_until, created_at, referred_by) VALUES(?,?,?,?,?,?,?,?,?)""",
                (owner_id, bot_token, bot_username, str(channel_id), brand, tz,
                 _iso(now + timedelta(days=trial_days)), _iso(now), referred_by))
            return cur.lastrowid

    def extend(self, client_id: int, days: int, method: str, amount: int | None = None,
               charge_id: str | None = None, now: datetime | None = None) -> str | None:
        """Ajoute `days` jours. Si l'abonnement est déjà expiré, on repart d'aujourd'hui.
        Retourne la nouvelle date de fin, ou None si ce paiement a déjà été crédité."""
        now = now or utc_now()
        with self._db() as c:
            if charge_id and c.execute("SELECT 1 FROM payments WHERE charge_id=?", (charge_id,)).fetchone():
                return None
            row = c.execute("SELECT paid_until FROM clients WHERE id=?", (client_id,)).fetchone()
            if not row:
                raise KeyError(client_id)
            start = max(_parse(row["paid_until"]) or now, now)
            until = _iso(start + timedelta(days=days))
            c.execute("UPDATE clients SET paid_until=?, reminded_at=NULL WHERE id=?", (until, client_id))
            c.execute("INSERT INTO payments(client_id, method, amount, days, charge_id, created_at) VALUES(?,?,?,?,?,?)",
                      (client_id, method, amount, days, charge_id, _iso(now)))
        return until

    def set_status(self, client_id: int, status: str):
        with self._db() as c:
            c.execute("UPDATE clients SET status=? WHERE id=?", (status, client_id))

    def set_price(self, client_id: int, price_stars: int | None):
        """Tarif personnel en Stars (None = tarif normal)."""
        with self._db() as c:
            c.execute("UPDATE clients SET price_stars=? WHERE id=?", (price_stars, client_id))

    def mark_reminded(self, client_id: int, now: datetime | None = None):
        with self._db() as c:
            c.execute("UPDATE clients SET reminded_at=? WHERE id=?", (_iso(now or utc_now()), client_id))

    def delete(self, client_id: int):
        with self._db() as c:
            c.execute("DELETE FROM clients WHERE id=?", (client_id,))


    # ------------------------------------------------------------ parrainage
    def referral_code(self, owner_id: int) -> str:
        """Code de parrainage personnel (créé au premier appel)."""
        with self._db() as c:
            r = c.execute("SELECT code FROM referral_codes WHERE owner_id=?", (owner_id,)).fetchone()
            if r:
                return r["code"]
            while True:
                code = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(8))
                try:
                    c.execute("INSERT INTO referral_codes(owner_id, code) VALUES(?, ?)", (owner_id, code))
                    return code
                except sqlite3.IntegrityError:
                    continue

    def referrer_for_code(self, code: str) -> int | None:
        with self._db() as c:
            r = c.execute("SELECT owner_id FROM referral_codes WHERE code=?", (code,)).fetchone()
        return r["owner_id"] if r else None

    def set_pending_referral(self, user_id: int, referrer_id: int, now: datetime | None = None) -> bool:
        """Retient qui a invité ce visiteur. Refusé pour soi-même ou pour un client déjà inscrit."""
        if user_id == referrer_id or self.by_owner(user_id):
            return False
        with self._db() as c:
            c.execute("INSERT OR IGNORE INTO pending_referrals(user_id, referrer_id, created_at) VALUES(?,?,?)",
                      (user_id, referrer_id, _iso(now or utc_now())))
        return True

    def pending_referrer(self, user_id: int) -> int | None:
        with self._db() as c:
            r = c.execute("SELECT referrer_id FROM pending_referrals WHERE user_id=?", (user_id,)).fetchone()
        return r["referrer_id"] if r else None

    def reward_referrer(self, referred_client_id: int, days: int, now: datetime | None = None):
        """À appeler après un paiement du filleul. Crédite UNE fois le premier robot du parrain.
        Retourne (parrain, robot crédité, nouvelle date de fin) ou None."""
        client = self.get(referred_client_id)
        if not client or not client.get("referred_by") or days <= 0:
            return None
        sponsor_bots = self.by_owner(client["referred_by"])
        if not sponsor_bots:
            return None
        target = sponsor_bots[0]
        until = self.extend(target["id"], days, "PARRAINAGE", charge_id=f"ref:{referred_client_id}", now=now)
        return (client["referred_by"], target, until) if until else None

    def referral_stats(self, owner_id: int) -> dict:
        with self._db() as c:
            invited = c.execute("SELECT id FROM clients WHERE referred_by=?", (owner_id,)).fetchall()
            ids = [r["id"] for r in invited]
            paid = earned = 0
            if ids:
                marks = ",".join("?" * len(ids))
                paid = c.execute(f"SELECT COUNT(DISTINCT client_id) FROM payments WHERE client_id IN ({marks}) "
                                 "AND method IN ('STARS','MANUEL')", ids).fetchone()[0]
                earned = c.execute(f"SELECT COALESCE(SUM(days),0) FROM payments WHERE method='PARRAINAGE' AND "
                                   f"charge_id IN ({','.join('?' * len(ids))})", [f"ref:{i}" for i in ids]).fetchone()[0]
        return {"invited": len(ids), "paid": paid, "days_earned": earned}


# ---------------------------------------------------------------- règles d'abonnement
def is_running_allowed(client: dict, now: datetime | None = None) -> bool:
    """Le robot du client doit tourner seulement s'il n'est pas suspendu et pas expiré."""
    now = now or utc_now()
    return client["status"] == STATUS_ACTIVE and (_parse(client["paid_until"]) or now) > now


def days_left(client: dict, now: datetime | None = None) -> float:
    now = now or utc_now()
    return ((_parse(client["paid_until"]) or now) - now).total_seconds() / 86400


def needs_reminder(client: dict, before_days: int, now: datetime | None = None) -> bool:
    """Un seul rappel par période d'abonnement, `before_days` jours avant la fin."""
    if client["status"] != STATUS_ACTIVE or client.get("reminded_at"):
        return False
    return 0 < days_left(client, now) <= before_days
