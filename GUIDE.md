# Robot ANONYMETRADER VIP — Guide d'installation

## Comment ça marche

1. En privé avec le robot, tu tapes **/signal**.
2. Il te demande : actif → BUY/SELL → entrée → SL → TP → photo → commentaire.
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
