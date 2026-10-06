"""
Schéma de base de données pour la multi-tenancy (bot factory)
============================================================

Structure :
- Une table 'clients' pour gérer chaque instance client
- Toutes les autres tables avoir un client_id pour l'isolation
- Gestion des abonnements et paiements
"""
import sqlite3
from datetime import datetime, timedelta


def migrate_to_multitenant(db_path: str):
    """Migrer une base de données existante vers multi-tenancy."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Créer la table signals si elle n'existe pas (base vierge)
    c.execute("""CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT, pair TEXT, direction TEXT,
        entry REAL, entry_text TEXT, sl REAL, sl_text TEXT,
        tps TEXT, tps_text TEXT, note TEXT, photo TEXT,
        msg_id INTEGER, status TEXT DEFAULT 'OPEN',
        tp_hit INTEGER DEFAULT 0, be INTEGER DEFAULT 0,
        outcome TEXT, result_pips REAL, closed_at TEXT,
        closed_pct REAL, realized_pips REAL, order_type TEXT DEFAULT 'MARKET',
        ref TEXT, client_id INTEGER DEFAULT NULL
    )""")

    # Créer la table clients
    c.execute("""CREATE TABLE IF NOT EXISTS clients(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_token TEXT UNIQUE NOT NULL,
        owner_id INTEGER NOT NULL,           -- ID Telegram du propriétaire
        bot_username TEXT UNIQUE,             -- @nom_du_robot
        status TEXT DEFAULT 'TRIAL',          -- TRIAL, ACTIVE, SUSPENDED, EXPIRED
        trial_ends_at TEXT,                   -- ISO 8601 (None = essai infini)
        subscription_ends_at TEXT,            -- ISO 8601 (None = pas d'abo)
        payment_method TEXT,                  -- STARS, CRYPTO, MANUAL, None
        payment_status TEXT,                  -- PENDING, CONFIRMED, FAILED
        last_payment_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # Créer une table pour les "paiements attendus"
    c.execute("""CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL UNIQUE,
        amount REAL,                          -- En EUR ou équivalent
        currency TEXT DEFAULT 'EUR',          -- EUR, USDT, etc.
        payment_link TEXT,                    -- Lien Telegram Stars / Crypto / Stripe
        status TEXT DEFAULT 'PENDING',        -- PENDING, CONFIRMED, EXPIRED
        due_date TEXT,                        -- Quand doit-on relancer ?
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(client_id) REFERENCES clients(id)
    )""")

    # client_id est déjà dans signals si créée ci-dessus, sinon l'ajouter
    try:
        c.execute("ALTER TABLE signals ADD COLUMN client_id INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # colonne existe déjà ou table a été créée avec

    # Créer les autres tables avec client_id
    c.execute("""CREATE TABLE IF NOT EXISTS settings_v2(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(client_id, key),
        FOREIGN KEY(client_id) REFERENCES clients(id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS news_v2(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        created_at TEXT,
        title TEXT,
        event_at TEXT,
        impact TEXT,
        assets TEXT,
        forecast TEXT,
        previous TEXT,
        note TEXT,
        msg_id INTEGER,
        reminded INTEGER DEFAULT 0,
        actual TEXT,
        FOREIGN KEY(client_id) REFERENCES clients(id)
    )""")

    # Index pour les requêtes fréquentes
    c.execute("CREATE INDEX IF NOT EXISTS idx_signals_client ON signals(client_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_settings_client ON settings_v2(client_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_news_client ON news_v2(client_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_clients_status ON clients(status)")

    conn.commit()
    conn.close()


def get_client_db(client_id: int, base_db_path: str = "signals.db"):
    """
    Retourne une connexion DB pour un client.
    Dans cette implémentation, une seule DB est utilisée avec client_id comme clé.
    """
    conn = sqlite3.connect(base_db_path)
    conn.row_factory = sqlite3.Row
    return conn


class ClientManager:
    """Gère les clients et leurs abonnements."""

    def __init__(self, db_path: str = "signals.db"):
        self.db_path = db_path
        migrate_to_multitenant(db_path)

    def add_client(self, bot_token: str, owner_id: int, bot_username: str = None,
                   trial_days: int = 14) -> int:
        """Ajouter un nouveau client avec essai gratuit."""
        with get_client_db(None, self.db_path) as conn:
            c = conn.cursor()
            trial_ends = (datetime.utcnow() + timedelta(days=trial_days)).isoformat()
            c.execute(
                """INSERT INTO clients(bot_token, owner_id, bot_username, status, trial_ends_at)
                   VALUES(?, ?, ?, 'TRIAL', ?)""",
                (bot_token, owner_id, bot_username, trial_ends)
            )
            conn.commit()
            return c.lastrowid

    def get_client(self, client_id: int) -> dict:
        """Récupérer les infos d'un client."""
        with get_client_db(client_id, self.db_path) as conn:
            c = conn.cursor()
            r = c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
            return dict(r) if r else None

    def check_subscription(self, client_id: int) -> dict:
        """Vérifier le statut de l'abonnement d'un client."""
        client = self.get_client(client_id)
        if not client:
            return {"status": "NOT_FOUND", "active": False}

        now = datetime.utcnow().isoformat()

        # Vérifier si l'essai est toujours valide
        if client["status"] == "TRIAL" and client["trial_ends_at"] and client["trial_ends_at"] > now:
            return {"status": "TRIAL", "active": True, "expires_at": client["trial_ends_at"]}

        # Vérifier si l'abo est valide
        if client["status"] == "ACTIVE" and client["subscription_ends_at"] and client["subscription_ends_at"] > now:
            return {"status": "ACTIVE", "active": True, "expires_at": client["subscription_ends_at"]}

        # Expiré ou suspendu
        return {"status": client["status"], "active": False, "expires_at": client["subscription_ends_at"]}

    def renew_subscription(self, client_id: int, months: int = 1, payment_method: str = "MANUAL"):
        """Renouveler un abonnement."""
        ends_at = (datetime.utcnow() + timedelta(days=30 * months)).isoformat()
        with get_client_db(client_id, self.db_path) as conn:
            c = conn.cursor()
            c.execute(
                """UPDATE clients SET status='ACTIVE', subscription_ends_at=?,
                   payment_method=?, last_payment_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (ends_at, payment_method, client_id)
            )
            conn.commit()

    def suspend_client(self, client_id: int, reason: str = None):
        """Suspendre un client."""
        with get_client_db(client_id, self.db_path) as conn:
            c = conn.cursor()
            c.execute("UPDATE clients SET status='SUSPENDED' WHERE id=?", (client_id,))
            conn.commit()

    def list_expiring_clients(self, days_before: int = 3):
        """Lister les clients dont l'abo expire bientôt."""
        soon = (datetime.utcnow() + timedelta(days=days_before)).isoformat()
        with get_client_db(None, self.db_path) as conn:
            c = conn.cursor()
            rows = c.execute(
                """SELECT * FROM clients
                   WHERE status='ACTIVE' AND subscription_ends_at < ? AND subscription_ends_at > ?
                   ORDER BY subscription_ends_at""",
                (soon, datetime.utcnow().isoformat())
            ).fetchall()
            return [dict(r) for r in rows]


if __name__ == "__main__":
    mgr = ClientManager()

    # Exemple : ajouter un client
    client_id = mgr.add_client(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        owner_id=987654321,
        bot_username="@MonRobotVIP"
    )
    print(f"✅ Client créé : ID={client_id}")

    # Vérifier l'abonnement
    status = mgr.check_subscription(client_id)
    print(f"📊 Statut : {status}")

    # Renouveler
    mgr.renew_subscription(client_id, months=1, payment_method="STARS")
    print(f"✅ Abonnement renouvelé")
