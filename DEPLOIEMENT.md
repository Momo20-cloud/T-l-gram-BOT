# 🚀 Mise en ligne — vendre ton robot en abonnement

Compte environ **15 minutes**. Ton canal VIP actuel n'est **pas touché** : il garde son service Railway.

```
Railway
├─ Service 1 (existant, inchangé)  python bot.py   → ton canal VIP
└─ Service 2 (nouveau)             python usine.py → robot de vente + page de vente
                                     ├─ robot du client #1 → /data/clients/1/signals.db
                                     ├─ robot du client #2 → /data/clients/2/signals.db
                                     └─ …
```

---

## 1. Crée ton robot de vente (2 min)

Dans Telegram, ouvre **@BotFather** :

1. `/newbot` → nom : `Anonymetrader Signals` → identifiant : par ex. `AnonymeSignalsBot`.
   Garde le **jeton** qu'il te donne.
2. `/setdescription` → choisis ce robot → colle :
   > Crée ton propre robot de signaux Telegram en 3 minutes : signaux brandés, suivi TP/SL en un clic, bilans automatiques. 7 jours d'essai gratuit.
3. `/setuserpic` → envoie ton logo.

Ton identifiant Telegram (pour `OWNER_IDS`) : envoie `/id` à ton robot VIP actuel.

## 2. Ajoute le service sur Railway (5 min)

1. Ouvre ton projet Railway (celui du canal VIP) → **+ New** → **GitHub Repo** → choisis ce dépôt.
2. Dans le nouveau service → **Settings** :
   - **Config-as-code → Railway Config File** : `railway.usine.json`
     (ça règle la commande `python usine.py`, le contrôle de santé et les redémarrages)
   - **Source → Branch** : la branche où se trouve ce code (`main` une fois fusionné).
3. **Volume** : clic droit sur le service → **Attach Volume** → chemin de montage **`/data`**.
   ⚠️ Indispensable : sans volume, la liste des clients disparaît à chaque redéploiement.
4. **Variables** → **Raw Editor** → colle le contenu de `.env.usine.example` et remplis :
   `USINE_BOT_TOKEN`, `OWNER_IDS`, `SUPPORT_CONTACT` (et `SITE_NAME` si tu veux un autre nom).
5. **Settings → Networking → Generate Domain**. Railway te donne une adresse du type
   `https://xxx.up.railway.app` : c'est ta **page de vente**. (Tu peux y brancher ton propre nom de domaine plus tard : *Custom Domain*.)
6. **Deploy**. Dans les journaux, tu dois voir :
   `🌐 Page de vente en ligne sur le port …` puis `🏭 Usine démarrée`.

## 3. Teste comme un client (5 min)

1. Ouvre ta page de vente → **Créer mon robot** → ça ouvre ton robot de vente dans Telegram.
2. `/creer` avec un **robot de test** (crée-en un autre sur @BotFather) et un **canal de test**.
3. Envoie un signal au robot de test : `XAUUSD BUY 2650 SL 2645 TP 2655 2660` → vérifie qu'il arrive dans le canal de test.
4. Dans ton robot de vente : `/clients` → tu dois voir ton client de test.
5. Retire le test : `/supprimer 1`.

## 4. Les paiements (Telegram Stars)

- Rien à configurer : pas de Stripe, pas de compte marchand. Le client paie dans Telegram.
- Les Stars reçues s'accumulent sur ton robot de vente. Pour les retirer : @BotFather → ton robot → **Balance** (retrait via Fragment, délai imposé par Telegram).
- Pour un client qui paie autrement (virement, crypto) : `/prolonger ID 30`.
- Le prix se change avec la variable `PRICE_STARS` (puis redéploiement).

## 5. Au quotidien — commandes propriétaire

| Commande | Effet |
|---|---|
| `/clients` | liste, statut, date de fin de chaque client |
| `/prolonger 3 30` | +30 jours au client #3 |
| `/suspendre 3` · `/reactiver 3` | couper / rallumer un robot |
| `/supprimer 3` | arrêter et retirer (ses données restent sur le disque) |
| `/journal 3` | dernières lignes du journal de son robot |

Tu reçois automatiquement : chaque nouveau client, chaque paiement, chaque expiration, et une alerte si le robot d'un client plante en boucle.

## Ce que voit le client

- `/creer` : jeton de son robot (message effacé aussitôt) → canal vérifié → nom de marque → robot en ligne.
- Dans **son** robot : `/signal`, mode rapide en un message, `/ouverts`, `/bilan`, `/news`, et **`/reglages`** (favoris, fuseau horaire, heure des bilans, bandeau photo, BE auto, % aux TP, rappel news).
- `/monrobot` et `/abonner` dans ton robot de vente pour voir son abonnement et payer.

## Dépannage

| Symptôme | Cause probable |
|---|---|
| Le déploiement échoue au contrôle de santé | le domaine n'est pas généré ou `PORT` est écrasé dans les variables : supprime `PORT` |
| `USINE_BOT_TOKEN manquant ou mal formé` | jeton mal recopié (sans guillemets ni espaces) |
| Clients perdus après un redéploiement | pas de volume monté sur `/data` |
| Un client dit que son robot ne répond pas | `/journal ID` : jeton révoqué ou robot retiré du canal |
| `Conflict: terminated by other getUpdates` dans un journal | le même jeton tourne ailleurs (ancien hébergement du client) |

## Capacité

Chaque robot client utilise environ 50 à 70 Mo de mémoire. Le plan Hobby de Railway convient pour une vingtaine de clients ;
au-delà, augmente la mémoire du service. Tests automatiques : `python -m unittest test_usine test_reglages test_instruments`.
