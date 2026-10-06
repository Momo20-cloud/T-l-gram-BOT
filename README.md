# Anonymetrader — robot de signaux Telegram

| Fichier | Rôle |
|---|---|
| `bot.py` | le robot de signaux (ton canal VIP, et la copie lancée pour chaque client) |
| `templates.py` | le style des messages publiés |
| `instruments.py` | le catalogue d'actifs (≈ 120) et la taille de leurs pips |
| `usine.py` · `usine_db.py` | l'usine à robots : vente par abonnement, un robot par client |
| `site/index.html` | la page de vente, servie par l'usine |
| `GUIDE.md` | utiliser le robot au quotidien |
| `DEPLOIEMENT.md` | **mettre l'usine en ligne sur Railway** |

Lancer les tests : `python -m unittest test_usine test_reglages test_instruments`
