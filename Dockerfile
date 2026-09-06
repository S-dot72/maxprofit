FROM python:3.12-slim

# Pas de wget/unzip/git : rien ici ne télécharge ni ne clone à l'exécution.
# Chaque paquet système en moins est une vulnérabilité de moins à suivre.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Les dépendances d'abord : cette couche est mise en cache tant que
# requirements.txt ne change pas, ce qui rend les redéploiements de code rapides.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

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

EXPOSE 10000

# --min-payout n'a pas de valeur par défaut : il vient de $MIN_PAYOUT_PCT.
CMD ["python", "-m", "maxprofit.hosting.service", "--source", "po"]
