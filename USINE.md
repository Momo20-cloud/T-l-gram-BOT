# 🏭 Usine à robots — vendre ton robot en abonnement

Ton `bot.py` ne change pas : ton canal VIP reste sur son service Railway actuel.
L'usine est un **deuxième service** qui lance une copie de `bot.py` pour chaque client.

## Comment ça marche

```
Service 1 (inchangé) : python bot.py   → ton canal VIP
Service 2 (nouveau)  : python usine.py → ton robot de vente
                         ├─ bot.py du client #1 → /data/clients/1/signals.db
                         ├─ bot.py du client #2 → /data/clients/2/signals.db
                         └─ ...
```

- Chaque client a **son propre robot, son propre processus et sa propre base**. Aucun client ne peut voir les données d'un autre.
- Essai gratuit → rappel avant la fin → pause automatique à l'expiration → redémarrage automatique au paiement.
- Paiement en **Telegram Stars** (aucun compte Stripe nécessaire). Tu peux aussi prolonger à la main (`/prolonger ID JOURS`) pour un virement ou de la crypto.
- Si le robot d'un client plante, il est relancé automatiquement. Tu es prévenu s'il plante en boucle.

## Mise en ligne sur Railway (≈ 10 min)

1. Sur @BotFather : `/newbot` → crée ton **robot de vente** (ex : `@AnonymeFactoryBot`).
2. Railway → ton projet → **New → GitHub Repo** → ce même dépôt (un 2ᵉ service).
3. Settings → **Custom Start Command** : `python usine.py`
4. Ajoute un **Volume** monté sur `/data` (indispensable, sinon les clients sont perdus au redéploiement).
5. Variables : copie `.env.usine.example` et remplis `USINE_BOT_TOKEN`, `OWNER_IDS`, `SUPPORT_CONTACT`.
6. Deploy. Envoie `/start` à ton robot de vente, puis `/creer` pour tester avec un robot de test.

## Parcours du client

`/creer` → colle le jeton de son robot (le message est effacé) → ajoute son robot admin de son canal et transfère un message du canal (droits vérifiés) → choisit sa marque → son robot tourne. Il ouvre son robot et utilise `/signal`, `/bilan`, `/news` comme toi.

## Commandes propriétaire (dans ton robot de vente)

| Commande | Effet |
|---|---|
| `/clients` | liste, statut, date de fin |
| `/prolonger 3 30` | +30 jours au client #3 (paiement manuel) |
| `/suspendre 3` · `/reactiver 3` | couper / rallumer |
| `/supprimer 3` | arrêter et retirer (ses données restent sur le disque) |
| `/journal 3` | dernières lignes du journal de son robot |

## Limites actuelles (à faire ensemble ensuite)

- Une copie de bot.py ≈ 50–70 Mo de mémoire : prévoir le plan Railway en conséquence au-delà de ~20 clients.
- L'API pour IA de trading (`API_KEY`) est désactivée pour les clients (un seul port par service).
- Les jetons des clients sont stockés en clair dans `/data/usine.db` : le volume ne doit être accessible qu'à toi.
- Les Stars se retirent via Fragment (délai de Telegram de 21 jours).

Tests : `python -m unittest test_usine -v`
