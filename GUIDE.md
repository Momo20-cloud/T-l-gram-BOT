# Robot ANONYMETRADER VIP — Guide d'installation

## Comment ça marche

1. En privé avec le robot, tu tapes **/signal**.
2. Il te demande : actif → BUY/SELL → **type d'ordre** (au marché, LIMIT, STOP) → entrée → SL → TP → (validité si ordre en attente) → photo → commentaire.
3. Il te montre un **aperçu** exact du post. Tu appuies sur **✅ Publier**.
4. Le signal part dans le canal VIP avec ta photo (bandeau doré « ANONYMETRADER VIP » ajouté en bas) et le modèle.
5. Tu reçois des **boutons de suivi** : `🎯 TP1` `🎯 TP2` `🎯 TP3` `🔒 SL → BE` `❌ SL / BE touché` `✋ Clôture manuelle`.
   Chaque bouton publie une mise à jour **en réponse au signal d'origine** dans le canal.
6. Le soir, le robot publie tout seul le **bilan du jour** (lun→ven), et le vendredi le **bilan de la semaine**.

### Commandes

| Commande | Rôle |
|---|---|
| `/signal` | Nouveau signal |
| `/ouverts` | Signaux en cours + leurs boutons |
| `/cloture 12 2655.3` | Clôturer le signal #12 à un prix précis |
| `/bilan` · `/bilan semaine` · `/bilan mois` | Aperçu du bilan + bouton « Publier » |
| `/accueil` | Publie et épingle le message d'accueil (règles + avertissement) |
| `/verifier` | Vérifie que le robot peut publier dans le canal |
| `/id` | Ton identifiant Telegram |
| `/annuler` | Annule la saisie en cours |

**Règle des résultats :** si un TP a été touché puis le prix revient, le bouton `❌ SL / BE touché` compte le trade comme gagné au dernier TP (le reste de la position est considéré fermé au BE, comme le dit le modèle). Sans TP touché : perte si le SL n'était pas au BE, 0 sinon.
### Ordres en attente (LIMIT / STOP)

| Type | BUY | SELL |
|---|---|---|
| **Au marché** | achat immédiat | vente immédiate |
| **LIMIT** | achat **plus bas** que le prix actuel (repli) | vente **plus haut** que le prix actuel (rebond) |
| **STOP** | achat **plus haut** que le prix actuel (cassure) | vente **plus bas** que le prix actuel (cassure) |

- Un ordre LIMIT/STOP est publié « en attente » avec sa **validité** (`2h`, `90min`, `18:00`, `26/09 12:00`, ou jusqu'à annulation).
- Boutons tant qu'il n'est pas déclenché : `⚡ Ordre déclenché` · `🚫 Annuler l'ordre`.
- Après « déclenché », tu retrouves les boutons habituels (TP, BE, SL, clôture).
- À l'heure limite, le robot **annule tout seul** l'ordre et prévient le canal.
- Un ordre annulé ou expiré **ne compte pas** dans les bilans.

Les bilans sont exprimés en **R** (1R = le risque pris jusqu'au SL). Comme ça on peut additionner l'or et le BTC sans mélanger des pips qui n'ont pas la même valeur.

---

## Étape 1 — Créer le robot (2 min)

1. Sur Telegram, ouvre **@BotFather** → `/newbot`.
2. Nom : `Anonymetrader VIP Bot` · identifiant : par ex. `anonymetrader_vip_bot`.
3. BotFather te donne un **jeton** (`123456:AA...`). Garde-le secret : c'est `BOT_TOKEN`.
4. Tu peux aussi faire `/setuserpic` (logo) et `/setdescription` chez BotFather.

## Étape 2 — Ajouter le robot au canal

Canal VIP → Administrateurs → Ajouter un admin → cherche ton robot.
Coche : **Publier des messages**, **Modifier les messages**, **Épingler les messages**.

## Étape 3 — Récupérer les deux identifiants

Démarre le robot une première fois (étape 4), puis :
- envoie **/id** au robot → c'est `ADMIN_IDS` ;
- **transfère au robot un message du canal** → il répond avec l'ID du canal (`-100…`) → c'est `CHANNEL_ID`.

Mets ces valeurs dans les variables, puis redémarre. Tape `/verifier` : tout doit être ✅.

## Étape 4 — L'hébergement : faire tourner le robot 24h/24

Telegram ne fait que **transporter les messages**. Le programme du robot doit tourner sur un ordinateur allumé en permanence, un serveur. Deux options :

### Option A — Railway (la plus simple, quelques $/mois)
1. Crée un compte sur **github.com** et envoie ce dossier dans un dépôt **privé** (bouton « Add file → Upload files »). N'envoie **pas** de fichier `.env`.
2. Sur **railway.app** : New Project → Deploy from GitHub repo → choisis le dépôt.
3. Onglet **Variables** : ajoute `BOT_TOKEN`, `CHANNEL_ID`, `ADMIN_IDS`, `TIMEZONE`, `DB_PATH=/data/signals.db` (voir `.env.example`).
4. Clic droit sur le service → **Attach Volume** → chemin de montage `/data`. C'est là que l'historique des signaux est conservé : sans volume, il s'efface à chaque redéploiement.
5. Railway lit le `Procfile` et lance `python bot.py`. Dans les logs tu dois voir `🤖 Robot démarré`.

### Option B — Un petit VPS (Hetzner, OVH, Contabo…)
```bash
sudo apt update && sudo apt install -y python3-venv
cd anonymetrader-bot && python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env      # remplis tes valeurs
python bot.py                           # test ; Ctrl+C pour arrêter
```
Pour qu'il tourne en permanence : `sudo nano /etc/systemd/system/anonybot.service`
```ini
[Unit]
Description=Anonymetrader bot
After=network.target
[Service]
WorkingDirectory=/root/anonymetrader-bot
ExecStart=/root/anonymetrader-bot/venv/bin/python bot.py
Restart=always
[Install]
WantedBy=multi-user.target
```
Puis : `sudo systemctl enable --now anonybot`

### Pour tester sur ton PC d'abord
Installe Python 3.11+, puis les mêmes commandes que l'option B (`pip install -r requirements.txt`, remplir `.env`, `python bot.py`). Le robot marche tant que la fenêtre est ouverte.

---

## Personnaliser le modèle

Tout le texte est dans **`templates.py`** : nom de marque, emojis, pied de page de gestion du risque, messages TP/SL, bilans et message d'accueil. Tu modifies, tu redémarres, c'est appliqué.
- Désactiver le bandeau sur les photos : `WATERMARK=false`
- Changer les boutons d'actifs rapides : `QUICK_PAIRS=XAUUSD,BTCUSD`
- Heure du bilan : `DAILY_REPORT_TIME=22:00` (vide = pas de bilan auto)

---

# 🤖 Brancher ton IA de trading

Sur Telegram, un robot **ne peut pas lire les messages d'un autre robot**. Ton IA envoie donc ses signaux au robot **par internet (API web)**, et le robot les publie avec le même modèle.

```
IA de trading ──(HTTP + clé secrète)──▶ Robot ANONYMETRADER ──▶ Canal VIP
                                          │
                                          └─▶ toi (aperçu à valider, ou simple notification)
```

## Mise en place sur Railway (5 min)

1. Onglet **Variables** du robot, ajoute :
   - `API_KEY` = une longue clé secrète (ex. générée au hasard, 30+ caractères)
   - `API_MODE` = `validation` (conseillé au début) ou `auto`
   - `PORT` = `8080`
2. Onglet **Settings → Networking → Generate Domain**, port **8080**. Railway te donne une adresse du type `https://anonymetrader-bot-production.up.railway.app`.
3. **Deploy**. Dans les logs : `🌐 API active sur le port 8080 — mode validation`.
4. Test : ouvre l'adresse dans ton navigateur → tu dois voir `{"ok": true, "service": "anonymetrader-bot", ...}`.

## Les deux modes

| Mode | Ce qui se passe |
|---|---|
| `validation` | Tu reçois l'aperçu du signal en privé avec **✅ Publier / ❌ Refuser**. Au-delà de 5 min, il expire (le prix a bougé). |
| `auto` | Publié immédiatement dans le canal. Tu reçois une notification avec les boutons de suivi. |

Commence en `validation` tant que l'IA n'a pas fait ses preuves.

## Envoyer un signal : `POST /api/signal`

En-tête `X-API-Key: TA_CLE` (ou `?key=TA_CLE` dans l'adresse, ou `"key"` dans le JSON).

```json
{
  "pair": "XAUUSD",
  "direction": "BUY",
  "entry": 2650.5,
  "sl": 2645,
  "tp": [2655, 2660, 2670],
  "note": "Cassure résistance H1, RSI > 50",
  "ref": "ia-2026-09-23-001",
  "photo_url": "https://... (optionnel)",
  "photo_base64": "... (optionnel, à la place de photo_url)"
}
```
- `direction` accepte aussi `LONG/SHORT`, `ACHAT/VENTE`, ou directement `BUY_LIMIT`, `SELL STOP`…
- `order_type` (optionnel) : `market` (par défaut), `limit` ou `stop`.
- Validité d'un ordre en attente (optionnel) : `"expiry_minutes": 120` ou `"valid_until": "18:00"`.
- `entry` peut être une zone : `[2650, 2652]`.
- `ref` = identifiant unique du trade côté IA : le même `ref` envoyé 2 fois n'est publié qu'une fois, et sert pour les mises à jour.
- Le robot fait les **mêmes contrôles** qu'en manuel (SL et TP du bon côté) et répond une erreur claire sinon.

**Format texte accepté aussi** (pratique pour TradingView) :
```
XAUUSD BUY 2650.5 SL 2645 TP 2655 2660 2670
XAUUSD SELL LIMIT 2670 SL 2676 TP 2660 2650
```

## Mettre à jour un trade : `POST /api/update`

```json
{ "ref": "ia-2026-09-23-001", "event": "tp1" }
```
`event` : `activate` (ordre en attente déclenché), `cancel` (ordre en attente annulé), `tp1` … `tp5`, `be` (SL au point d'entrée), `sl` (SL/BE touché), `close` (+ `"price": 2663.2`).
On peut utiliser `"id": 12` (numéro du signal) au lieu de `ref`.

## Réponses

| Code | Sens |
|---|---|
| 200 `published` | publié (mode auto) — contient `id` |
| 200 `pending_validation` | en attente de ta validation |
| 200 `duplicate` | déjà reçu avec ce `ref` |
| 400 | données invalides (message en français dans `error`) |
| 401 | mauvaise clé |

## Exemples

**Python** : voir `exemple_ia.py` (fonctions `envoyer_signal()` et `mettre_a_jour()` prêtes à copier).

**TradingView** (alerte → Notifications → Webhook URL) :
- URL : `https://TON-DOMAINE.up.railway.app/api/signal?key=TA_CLE`
- Message : `{{ticker}} BUY {{close}} SL 2645 TP 2660 2670` (ou le JSON ci-dessus)

**Test rapide en ligne de commande :**
```bash
curl -X POST https://TON-DOMAINE.up.railway.app/api/signal \
  -H "X-API-Key: TA_CLE" -H "Content-Type: application/json" \
  -d '{"pair":"XAUUSD","direction":"BUY","entry":2650.5,"sl":2645,"tp":[2655,2660]}'
```

⚠️ Ne partage jamais ta `API_KEY` : quiconque l'a peut publier dans ton canal. En cas de fuite, change-la dans Railway.
