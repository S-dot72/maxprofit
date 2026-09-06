FROM python:3.12-slim

# Aucun paquet système ajouté — pas même git.
#
# La première construction a échoué ici : `requirements-broker.txt` demandait la
# bibliothèque du broker par `git+https://…`, ce qui exige un clone, donc git,
# absent de python:3.12-slim. Plutôt que d'installer git puis de le retirer, la
# dépendance est déclarée par ARCHIVE et le commit est ÉPINGLÉ — ce qui règle du
# même coup un problème plus sérieux que le build : sans référence, un
# redéploiement installait l'état du moment de `main`, pour une bibliothèque non
# officielle qui peut changer sans préavis.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Les dépendances d'abord : cette couche est mise en cache tant que
# requirements.txt ne change pas, ce qui rend les redéploiements de code rapides.
COPY requirements.txt requirements-broker.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -r requirements-broker.txt

# `pocketoptionapi.stable_api` fait `import webview` au niveau module, même
# quand le SSID vient de l'environnement et qu'aucune fenêtre ne sera ouverte.
# Cet import n'initialise aucun backend graphique — il ne charge que du Python
# pur — mais si un jour il échoue sur cette image, c'est ici qu'il faut agir :
# soit ajouter les paquets système correspondants, soit installer la
# bibliothèque avec --no-deps et lister ses dépendances à la main. On vérifie
# donc à la CONSTRUCTION plutôt qu'au premier démarrage en production.
RUN python -c "import webview, pocketoptionapi.stable_api; print('adaptateur broker importable')"

COPY maxprofit/ ./maxprofit/
COPY reset_db.py pyproject.toml ./

# Processus non privilégié : si le conteneur est compromis, il n'est pas root.
RUN useradd --create-home --uid 10001 bot && chown -R bot:bot /app
USER bot

# ATTENTION — la base ne doit PAS vivre dans le conteneur. Le système de
# fichiers d'un conteneur est effacé à chaque déploiement : y écrire market.db
# revient à perdre la collecte à chaque mise à jour, ce qui est exactement le
# problème que la spec §1.1 cherche à éviter. TRADING_DB_PATH doit pointer vers
# un VOLUME PERSISTANT monté par l'hébergeur (Render : Disk ; Railway : Volume ;
# Fly : Volume). Le répertoire doit exister : le code refuse de le créer, pour
# qu'une faute de frappe donne une erreur et non une base vide.
ENV TRADING_DB_PATH=/data/market.db

# Le SSID vient de l'environnement, JAMAIS de l'image : c'est un jeton de
# session complet. La bibliothèque sait aussi l'obtenir en ouvrant une fenêtre
# de connexion, ce qui n'a aucun sens dans un conteneur — récupérez-le une fois
# en local avec outils/diagnostic_pocketoption.py, puis injectez-le ici.
#   POCKET_OPTION_SSID  (secret, à définir dans le tableau de bord)

EXPOSE 10000

# --min-payout n'a pas de valeur par défaut : il vient de $MIN_PAYOUT_PCT.
CMD ["python", "-m", "maxprofit.hosting.service", "--source", "po"]
