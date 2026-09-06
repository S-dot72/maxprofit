#!/usr/bin/env python
r"""
Contrôle avant déploiement — à lancer EN LOCAL, avec la configuration de prod.

    .venv\Scripts\python.exe outils\verifier_deploiement.py

Déployer d'abord et déboguer ensuite dans le visualiseur de journaux d'un
hébergeur coûte plusieurs minutes par aller-retour, et l'erreur arrive souvent
sous une forme tronquée. Tout ce qui peut échouer se vérifie ici en quinze
secondes.

Ce script contrôle en particulier les deux choses qu'aucun test ne peut couvrir,
parce qu'elles dépendent de comptes réels :

- que le jeton Telegram est valide et que le bot existe (`getMe`) ;
- que `TELEGRAM_CHAT_ID` désigne bien VOTRE conversation, en vous envoyant un
  message. C'est l'erreur la plus fréquente : on y met l'identifiant du bot au
  lieu du sien, et le bot ne peut alors alerter personne. Un bot ne s'envoie pas
  de message à lui-même, et l'API refuse silencieusement dans certains cas —
  d'où l'envoi réel plutôt qu'un simple contrôle de format.

Aucun secret n'est affiché : les jetons sont masqués.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

from maxprofit.core.config import charger_env_local  # noqa: E402

VERT, ROUGE, JAUNE, FIN = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


class Rapport:
    def __init__(self):
        self.bloquants: list[str] = []
        self.avertissements: list[str] = []

    def ok(self, quoi: str, detail: str = "") -> None:
        print(f"  {VERT}OK{FIN}    {quoi}" + (f"  ({detail})" if detail else ""))

    def echec(self, quoi: str, pourquoi: str) -> None:
        print(f"  {ROUGE}ÉCHEC{FIN} {quoi}")
        print(f"        {pourquoi}")
        self.bloquants.append(quoi)

    def alerte(self, quoi: str, pourquoi: str) -> None:
        print(f"  {JAUNE}NOTE{FIN}  {quoi}")
        print(f"        {pourquoi}")
        self.avertissements.append(quoi)


def masquer(valeur: str, garde: int = 6) -> str:
    if len(valeur) <= garde * 2:
        return "*" * len(valeur)
    return f"{valeur[:garde]}…{valeur[-garde:]} ({len(valeur)} car.)"


def _telegram(jeton: str, methode: str, **params):
    url = f"https://api.telegram.org/bot{jeton}/{methode}"
    requete = urllib.request.Request(
        url, method="POST", data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(requete, timeout=20) as reponse:
        return json.loads(reponse.read().decode("utf-8"))


# --------------------------------------------------------------------------- #

def verifier_donnees(r: Rapport) -> None:
    print("\n— Données —")
    from maxprofit.store import turso
    try:
        replique = turso.configure()
    except Exception:                                    # noqa: BLE001
        replique = False

    chemin = os.environ.get("TRADING_DB_PATH", "").strip()
    if not chemin:
        if replique:
            r.ok("TRADING_DB_PATH", f"non définie — cache dans "
                                    f"{turso.chemin_cache()}")
        else:
            r.echec("TRADING_DB_PATH", "absente. Aucune valeur par défaut (§5).")
            return
    if replique:
        # Le fichier n'est qu'un cache : ni chemin absolu ni répertoire
        # existant ne sont exigés, contrairement au stockage durable.
        if chemin:
            r.ok("TRADING_DB_PATH", f"{chemin} (réplique locale)")
        payout = os.environ.get("MIN_PAYOUT_PCT", "").strip()
        if not payout.isdigit():
            r.echec("MIN_PAYOUT_PCT", f"« {payout} » n'est pas un entier.")
        else:
            r.ok("MIN_PAYOUT_PCT", f"{payout} %")
        return
    p = Path(chemin)
    if not p.is_absolute():
        r.echec("TRADING_DB_PATH", f"{p} est relatif : donnez un chemin absolu.")
    elif not p.parent.is_dir():
        r.echec("TRADING_DB_PATH",
                f"le répertoire {p.parent} n'existe pas. Créez-le : le code "
                f"refuse de le faire, pour qu'une faute de frappe produise une "
                f"erreur et non une base vide.")
    else:
        r.ok("TRADING_DB_PATH", str(p))
        try:
            p.resolve().relative_to(RACINE)
            r.alerte("Emplacement de la base",
                     "elle est DANS le répertoire de code : un déploiement "
                     "l'écrasera (§1.1). En conteneur, visez le volume monté.")
        except ValueError:
            pass

    payout = os.environ.get("MIN_PAYOUT_PCT", "").strip()
    if not payout.isdigit():
        r.echec("MIN_PAYOUT_PCT",
                f"« {payout} » n'est pas un entier. Obligatoire : aucune valeur "
                f"par défaut sur ce qui touche à l'argent (§5).")
    else:
        r.ok("MIN_PAYOUT_PCT", f"{payout} %")


def verifier_stockage(r: Rapport) -> None:
    """Sur un hébergement sans disque, c'est le point qui décide de tout.

    Une collecte posée sur un disque éphémère tourne, a l'air saine, et repart
    de zéro à chaque redémarrage. Rien ne le signale — c'est le désastre
    silencieux de la §1.1, et le seul moyen de le voir est de vérifier ici.
    """
    print("\n— Stockage durable —")
    from maxprofit.store import turso

    try:
        actif = turso.configure()
    except Exception as erreur:                          # noqa: BLE001
        r.echec("Turso", str(erreur))
        return

    if not actif:
        r.alerte("Turso",
                 "non configuré : la base vit sur le disque local. Correct sur "
                 "un poste ou avec un volume persistant ; sur un hébergement "
                 "gratuit, la collecte disparaîtra au premier redémarrage.")
        return

    try:
        import libsql  # noqa: F401
    except ImportError as erreur:
        r.alerte("Pilote libsql",
                 f"absent de CE poste ({erreur}). Sans wheel pour Windows/"
                 f"Python 3.14 ; l'image Docker, elle, l'installe et le "
                 f"vérifie à la construction.")
    else:
        r.ok("Pilote libsql", "présent")

    r.ok("Turso", f"{os.environ[turso.ENV_URL]} — cache local : "
                  f"{turso.chemin_cache()}")


def verifier_broker(r: Rapport) -> None:
    print("\n— Broker —")
    from maxprofit.collect.pocketoption import chemin_session, resoudre_ssid

    jeton = resoudre_ssid(demo=True)
    if not jeton:
        r.echec("Jeton de session",
                f"ni POCKET_OPTION_SSID ni {chemin_session()}. Lancez "
                f"outils/capturer_ssid.py.")
        return
    if not jeton.startswith('42["auth"'):
        r.echec("Jeton de session", "format inattendu : il est tronqué ou décoré.")
        return
    if '"isDemo":1' not in jeton:
        r.alerte("Jeton de session",
                 "ce jeton est pour un compte RÉEL. Cette bibliothèque n'est "
                 "pas officielle : préférez un compte démo dédié.")
    r.ok("Jeton de session", masquer(jeton))


def verifier_telegram(r: Rapport) -> None:
    print("\n— Telegram —")
    jeton = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not jeton and not chat:
        r.alerte("Telegram", "non configuré : ni alertes ni renouvellement à "
                             "distance. La collecte fonctionnera quand même.")
        return
    if not (jeton and chat):
        r.echec("Telegram",
                "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID vont ensemble. Un jeton "
                "sans identifiant de conversation donnerait un bot sans liste "
                "blanche, pilotable par quiconque le découvre.")
        return

    try:
        reponse = _telegram(jeton, "getMe")
    except urllib.error.HTTPError as erreur:
        r.echec("TELEGRAM_BOT_TOKEN",
                f"Telegram refuse le jeton ({erreur.code}). Révoqué ou mal "
                f"copié ? Regénérez-le auprès de @BotFather.")
        return
    except urllib.error.URLError as erreur:
        r.echec("Telegram", f"API injoignable : {erreur.reason}")
        return

    if not reponse.get("ok"):
        r.echec("TELEGRAM_BOT_TOKEN", str(reponse.get("description")))
        return

    bot = reponse["result"]
    r.ok("TELEGRAM_BOT_TOKEN", f"@{bot.get('username')} (id {bot.get('id')})")

    if chat == str(bot.get("id")):
        r.echec("TELEGRAM_CHAT_ID",
                "c'est l'identifiant du BOT, pas celui de votre conversation. "
                "Un bot ne s'envoie pas de message à lui-même : aucune alerte "
                "ne vous parviendrait. Écrivez à @userinfobot pour obtenir le "
                "vôtre.")
        return

    try:
        envoi = _telegram(
            jeton, "sendMessage", chat_id=chat,
            text="✅ Contrôle avant déploiement : ce canal fonctionne.\n"
                 "C'est ici que vous serez alerté si la collecte s'arrête.",
        )
    except urllib.error.HTTPError as erreur:
        detail = erreur.read().decode("utf-8", "replace")[:200]
        r.echec("TELEGRAM_CHAT_ID",
                f"envoi refusé ({erreur.code}) : {detail}\n"
                f"        Avez-vous envoyé /start à votre bot ? Telegram "
                f"interdit à un bot d'écrire le premier.")
        return
    if envoi.get("ok"):
        r.ok("TELEGRAM_CHAT_ID", f"{chat} — message de test envoyé")
    else:
        r.echec("TELEGRAM_CHAT_ID", str(envoi.get("description")))


def verifier_admin(r: Rapport) -> None:
    print("\n— Renouvellement à distance —")
    secret = os.environ.get("ADMIN_SECRET", "").strip()
    if not secret:
        r.alerte("ADMIN_SECRET",
                 "absent : POST /session répondra 404 et le renouvellement "
                 "sans copier-coller sera indisponible. La commande Telegram "
                 "/ssid restera utilisable.")
        return
    if len(secret) < 16:
        r.alerte("ADMIN_SECRET",
                 f"{len(secret)} caractères, c'est court pour un secret qui "
                 f"autorise à changer le compte collecté.")
    if secret.lower() in {"changez-moi", "secret", "admin",
                          "changez-moi-par-une-valeur-aleatoire-longue"}:
        r.echec("ADMIN_SECRET", "c'est la valeur d'exemple. Changez-la.")
        return
    r.ok("ADMIN_SECRET", masquer(secret))


def verifier_image(r: Rapport) -> None:
    print("\n— Image —")
    dockerfile = (RACINE / "Dockerfile").read_text(encoding="utf-8")
    # Seules les lignes ACTIVES comptent : le fichier explique en commentaire
    # pourquoi `git+` est écarté, et chercher la chaîne partout accuserait sa
    # propre documentation.
    broker = "\n".join(
        ligne for ligne in
        (RACINE / "requirements-broker.txt").read_text(encoding="utf-8").splitlines()
        if ligne.strip() and not ligne.lstrip().startswith("#")
    )
    if "git+" in broker:
        r.echec("requirements-broker.txt",
                "dépendance déclarée par `git+https://`, ce qui exige git — "
                "absent de python:3.12-slim. La construction échouera. Utilisez "
                "une URL d'archive avec un commit épinglé.")
    elif "/archive/" not in broker:
        r.alerte("requirements-broker.txt",
                 "la bibliothèque du broker n'est pas épinglée à un commit : "
                 "un redéploiement peut installer autre chose que ce qui a été "
                 "testé.")
    else:
        r.ok("requirements-broker.txt", "commit épinglé, sans git")

    if "requirements-broker.txt" not in dockerfile:
        r.echec("Dockerfile",
                "n'installe pas requirements-broker.txt : l'adaptateur Pocket "
                "Option serait absent de l'image et --source po échouerait au "
                "démarrage.")
    else:
        r.ok("Dockerfile", "dépendances du broker incluses")

    import subprocess
    try:
        sale = subprocess.run(["git", "status", "--porcelain"], cwd=RACINE,
                              capture_output=True, text=True, timeout=10).stdout
        distant = subprocess.run(["git", "remote"], cwd=RACINE,
                                 capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return

    if not distant.strip():
        r.echec("Dépôt distant",
                "aucun. Render déploie depuis un dépôt Git : créez-en un sur "
                "GitHub et poussez-y le code.")
    else:
        r.ok("Dépôt distant", distant.split()[0])
    if sale.strip():
        r.alerte("Arbre de travail",
                 f"{len(sale.strip().splitlines())} fichier(s) non commité(s) : "
                 f"ils ne partiront pas au déploiement.")


def main() -> int:
    charges = charger_env_local()
    print("=" * 72)
    print("CONTRÔLE AVANT DÉPLOIEMENT")
    print("=" * 72)
    if charges:
        print(f"Chargé depuis .env : {', '.join(charges)}")

    r = Rapport()
    verifier_donnees(r)
    verifier_stockage(r)
    verifier_broker(r)
    verifier_telegram(r)
    verifier_admin(r)
    verifier_image(r)

    print("\n" + "=" * 72)
    if r.bloquants:
        print(f"{ROUGE}{len(r.bloquants)} point(s) bloquant(s){FIN} : "
              f"{', '.join(r.bloquants)}")
        print("Corrigez-les avant de déployer.")
        return 1
    if r.avertissements:
        print(f"{JAUNE}Prêt, avec {len(r.avertissements)} remarque(s){FIN} : "
              f"{', '.join(r.avertissements)}")
    else:
        print(f"{VERT}Tout est prêt.{FIN}")
    print()
    print("Vous devriez avoir reçu un message de test sur Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
