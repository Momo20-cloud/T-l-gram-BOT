"""
USINE À ROBOTS — registre des clients
-------------------------------------
Une ligne par client : son jeton, son canal, sa marque et son abonnement.
Les signaux de chaque client ne sont PAS ici : chaque robot client a sa
propre base (DATA_DIR/clients/<id>/signals.db). L'isolation est physique,
aucune requête ne peut lire les données d'un autre client.
"""
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
            trial_days: int, tz: str = "UTC", now: datetime | None = None) -> int:
        now = now or utc_now()
        with self._db() as c:
            cur = c.execute(
                """INSERT INTO clients(owner_id, bot_token, bot_username, channel_id, brand, timezone,
                                       paid_until, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (owner_id, bot_token, bot_username, str(channel_id), brand, tz,
                 _iso(now + timedelta(days=trial_days)), _iso(now)))
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

    def mark_reminded(self, client_id: int, now: datetime | None = None):
        with self._db() as c:
            c.execute("UPDATE clients SET reminded_at=? WHERE id=?", (_iso(now or utc_now()), client_id))

    def delete(self, client_id: int):
        with self._db() as c:
            c.execute("DELETE FROM clients WHERE id=?", (client_id,))


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
