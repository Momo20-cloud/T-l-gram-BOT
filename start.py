"""
Lanceur unique pour Railway (Procfile : python start.py)
- variable USINE_BOT_TOKEN présente → usine à robots + page de vente (usine.py)
- sinon                             → ton robot VIP (bot.py), comme avant
Ainsi le même dépôt sert aux deux services, sans réglage de commande sur Railway.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
target = "usine.py" if os.getenv("USINE_BOT_TOKEN", "").strip() else "bot.py"
print(f"▶️ Démarrage de {target}", flush=True)
os.execv(sys.executable, [sys.executable, os.path.join(HERE, target)])
