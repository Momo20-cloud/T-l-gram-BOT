# Robot ANONYMETRADER VIP — Guide d'installation

## Comment ça marche

1. En privé avec le robot, tu tapes **/signal**.
2. Il te demande : actif → BUY/SELL → **type d'ordre** (au marché, LIMIT, STOP) → entrée → SL → TP → (validité si ordre en attente) → photo → commentaire.
3. Il te montre un **aperçu** exact du post. Tu appuies sur **✅ Publier**.
4. Le signal part dans le canal VIP avec ta photo (bandeau doré « ANONYMETRADER VIP » ajouté en bas) et le modèle.
5. Tu reçois des **boutons de suivi** : `🎯 TP1` `🎯 TP2` `🎯 TP3` `🔒 SL → BE` `❌ SL / BE touché` `✋ Clôture manuelle`.
   Chaque bouton publie une mise à jour **en réponse au signal d'origine** dans le canal.
6. Le soir, le robot publie tout seul le **bilan du jour** (lun→ven), le vendredi le **bilan de la semaine**, et le dernier jour du mois le **bilan du mois**.

### ⚡ Mode rapide : tout en un message

Plus besoin de répondre étape par étape : envoie directement au robot (même sans taper /signal) :

```
XAUUSD BUY 2650 SL 2645 TP 2655 2660 2670
Cassure résistance H1, RSI > 50          ← analyse (optionnelle) sur la 2e ligne
```

Le robot répond « ✅ Compris », te demande la **photo** (ou /passer), puis montre l'**aperçu** avec ✅ Publier / ❌ Annuler.
**Encore plus rapide :** envoie la photo du graphique avec ce texte **en légende** → aperçu direct.

| Ce que tu veux | Exemple |
|---|---|
| Ordre au marché | `XAUUSD BUY 2650 SL 2645 TP 2655 2660` |
| Zone d'entrée | `XAUUSD BUY 2650-2652 SL 2645 TP 2660` |
| Ordre en attente + validité | `XAUUSD SELL LIMIT 2670 SL 2676 TP 2660 2650 EXP 2h` |
| Validité à une heure | `BTCUSD SELL STOP 63500 SL 64000 TP 63000 EXP 18:00` |
| Analyse sur la même ligne | `... TP 2660 NOTE: cassure du support` |

Les virgules décimales (`2650,5`, `1,0850`) sont acceptées. Le robot vérifie que le SL et les TP sont du bon côté, et t'explique l'erreur sinon. Le mode pas à pas (`/signal` puis les boutons) existe toujours.

### 📷 Capture TradingView : le robot lit les niveaux

1. Sur TradingView, place ton trade avec l'outil **Position longue** ou **Position courte**.
2. Fais une capture où l'on voit **l'outil en entier** et **l'échelle de prix à droite**.
3. Envoie-la au robot, sans rien taper. Le robot lit :
   - la direction (zone verte au-dessus = BUY, en dessous = SELL) ;
   - l'entrée, le SL et le TP ;
   - l'actif, dans l'en-tête du graphique.
4. Il te montre l'aperçu, puis tu publies d'un clic.

Astuces :
- **Pour plus de précision**, envoie la capture **en fichier** (📎 → Fichier) plutôt qu'en photo : Telegram compresse les photos.
- **Si l'actif n'apparaît pas** sur la capture, écris-le en légende (`XAUUSD`). Le reste de la légende devient ton analyse.
- **Pour corriger une valeur ou ajouter des TP**, envoie simplement le signal corrigé en texte au moment de l'aperçu (`XAUUSD BUY 2650 SL 2644 TP 2660 2670`). Le robot garde la photo.
- **Les valeurs marquées « estimées »** sont déduites de la position des zones sur le graphique, à environ un pixel près : vérifie-les avant de publier.

### Commandes

| Commande | Rôle |
|---|---|
| `/signal` | Nouveau signal |
| `/ouverts` | Signaux en cours + leurs boutons |
| `/cloture 12 2655.3` | Clôturer le signal #12 à un prix précis |
| `/bilan` · `/bilan hier` | Bilan du jour / d'hier (aperçu + bouton « Publier ») |
| `/bilan semaine` · `/bilan semaine derniere` | Semaine en cours / précédente |
| `/bilan mois` · `/bilan mois dernier` · `/bilan septembre` · `/bilan 09/2026` | Bilan d'un mois |
| `/bilan 01/09 15/09` · `/bilan 01/09/2026 30/09/2026` | Période au choix |
| `/bilan annee` · `/bilan 2026` | Bilan de l'année |
| `/news` | Annonce économique (NFP, CPI, FOMC…) avec rappel et chiffre réel |
| `/reglages` | Actifs favoris, fuseau horaire, heure du bilan, jour du bilan hebdo, bandeau photo, BE auto, % aux TP, rappel news |
| `/accueil` | Publie et épingle le message d'accueil (règles + avertissement) |
| `/verifier` | Vérifie que le robot peut publier dans le canal |
| `/id` | Ton identifiant Telegram |
| `/annuler` | Annule la saisie en cours |

### Prises de profits partielles

Quand tu appuies sur **🎯 TP1** (ou TP2…), le robot te demande **quel % de la position clôturer** : `25 %` `33 %` `50 %` `75 %` `Tout le reste` ou `Autre %` (tu tapes le chiffre).

Exemple sur l'or, BUY 2650, SL 2645, TP1 2655 / TP2 2660 / TP3 2670 :

| Étape | Message publié | Total |
|---|---|---|
| TP1, 50 % | TP1 +50 pips · 50 % fermés → +25 pips sécurisés · SL au BE · on continue vers TP2 | +25 pips |
| TP2, 30 % | TP2 +100 pips · 30 % fermés → +30 pips · on continue vers TP3 avec 20 % | +55 pips |
| BE touché | Les 20 % restants sortent au BE (0 pip) | **+55 pips** |

- Les résultats sont **pondérés** : chaque morceau compte pour sa part de la position.
- Au **dernier TP**, le reste est fermé automatiquement.
- Après une clôture partielle au TP1, le SL passe **automatiquement au BE** (désactivable : `AUTO_BE_AFTER_TP1=false`). Sans BE, le reste sorti au SL compte en perte.
- `/cloture 12 2685` ferme **ce qu'il reste** au prix donné et calcule le total.

Les bilans sont exprimés en **R** (1R = le risque pris jusqu'au SL) **et en pips par actif**. Ils indiquent aussi le profit factor, le meilleur et le pire trade, et le nombre d'ordres annulés.

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

Le R permet d'additionner l'or et le BTC sans mélanger des pips qui n'ont pas la même valeur.

### Annonces économiques (`/news`)

1. `/news` → choisis l'événement (NFP, CPI, FOMC, Powell, PPI, chômage, PIB, PMI, BCE) ou tape son nom.
2. Heure (`14:30` ou `10/10 14:30`), impact (🔴 fort / 🟠 moyen / 🟡 faible), devises concernées, prévision / précédent (`180K / 175K`), ton conseil (ou le conseil standard).
3. Aperçu → **Publier**. Le robot publie un **rappel 15 min avant** (réglable : `NEWS_REMINDER_MIN`).
4. Après la sortie du chiffre, bouton **📊 Publier le chiffre réel** → tape `210K Emploi plus fort que prévu`. Le robot compare à la prévision (« au-dessus des attentes »).

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
`event` : `activate` (ordre en attente déclenché), `cancel` (ordre en attente annulé), `tp1` … `tp5` (+ `"close_pct": 50` = % à fermer ; sinon `TP_DEFAULT_PCT`, et tout le reste au dernier TP), `be` (SL au point d'entrée), `sl` (SL/BE touché), `close` (+ `"price": 2663.2`).
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
