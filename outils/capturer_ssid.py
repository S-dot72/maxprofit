#!/usr/bin/env python
r"""
Capture du SSID Pocket Option — à faire UNE fois.

    .venv\Scripts\python.exe outils\capturer_ssid.py
    .venv/bin/python outils/capturer_ssid.py          # Linux/mac

Une fenêtre s'ouvre. Connectez-vous sur votre compte DÉMO. Dès que la session
est lisible, la fenêtre se ferme et le SSID est ENREGISTRÉ dans `session.json`.
Il n'y a rien à recopier : le collecteur et le diagnostic le reliront de là.

Si la fenêtre se referme aussitôt, c'est qu'une session valide existait déjà
dans les cookies enregistrés — c'est le cas normal après une première capture.
Pour vous connecter sur un autre compte, utilisez `--nouvelle-session`, qui
ouvre une fenêtre sans cookies.

En hébergement, relancez avec `--afficher` : le disque d'un conteneur est
éphémère, `session.json` n'y survivrait pas à un déploiement, et c'est
`POCKET_OPTION_SSID` qu'il faut renseigner dans les variables de la plateforme.

--- Pourquoi cet outil existe ---------------------------------------------

La bibliothèque sait le faire elle-même, mais sa méthode se bloque en silence.
Son `read_cookies` attend que SEPT cookies soient présents en même temps :

    ci_session, afUserId, ttcsid, _scid, _scid_r, _twpid, lo_uid

Seul le premier est le cookie d'authentification. Les six autres sont des
traceurs tiers — Snapchat (`_scid`), TikTok (`ttcsid`), Twitter (`_twpid`),
AppsFlyer (`afUserId`). S'il en manque un, parce qu'un bloqueur l'empêche,
parce que le consentement n'a pas été donné, ou simplement parce que le traceur
n'a pas encore tiré, la condition n'est jamais vraie. La boucle tourne alors 250
secondes, renonce, et NE FERME PAS la fenêtre : `webview.start()` ne rend jamais
la main et le processus reste figé, sans un message.

C'est exactement le mode de défaillance que ce projet passe son temps à
éliminer. Cet outil n'exige donc que `ci_session`, affiche ce qu'il attend
pendant qu'il attend, et s'arrête franchement au bout du délai.

--- Ce que contient un SSID ------------------------------------------------

    42["auth",{"session":"...","isDemo":1,"uid":123456,"platform":2,...}]

C'est un jeton de session complet : quiconque l'a peut agir sur le compte.
`session.json` et `.env` sont tous deux dans `.gitignore`. Ne le collez pas dans
une conversation — l'outil ne l'affiche d'ailleurs que masqué, sauf demande
explicite. Il expire : quand la collecte s'arrêtera sur une erreur
d'authentification, relancez cet outil.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

#: Le seul cookie qui compte : c'est lui qui authentifie la requête vers le
#: cabinet, d'où l'on extrait l'identifiant de session et l'uid.
COOKIE_REQUIS = "ci_session"

URL_CABINET = "https://pocketoption.com/en/cabinet/demo-quick-high-low"


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _interpreteur import exiger  # noqa: E402

# On verifie ce que l'interpreteur SAIT FAIRE, pas son chemin : depuis
# qu'il existe un second venv en 3.13 pour libsql, exiger `.venv` en dur
# revenait a refuser le seul environnement capable de faire tourner ceci.
exiger('webview', 'outils\\capturer_ssid.py')


def _cookies_de(window) -> dict[str, str]:
    """Extrait les cookies de la fenêtre, sans supposer du format exact.

    `window.get_cookies()` renvoie des objets dont `output()` produit une ligne
    d'en-tête `Set-Cookie:`. Le format varie selon le moteur de rendu, d'où un
    découpage défensif plutôt que le `split("-Cookie: ")[1]` de la bibliothèque,
    qui lève sur toute variation.
    """
    trouves: dict[str, str] = {}
    try:
        bruts = window.get_cookies() or []
    except Exception:                                    # noqa: BLE001
        return trouves
    for c in bruts:
        try:
            ligne = c.output()
        except Exception:                                # noqa: BLE001
            continue
        _, _, apres = ligne.partition("Set-Cookie:")
        paire = (apres or ligne).split(";")[0].strip()
        nom, _, valeur = paire.partition("=")
        if nom:
            trouves[nom.strip()] = valeur.strip()
    return trouves


def _construire_ssid(cookies: dict[str, str], demo: bool) -> str | None:
    """Reproduit la construction du SSID de la bibliothèque, en signalant
    précisément ce qui manque au lieu de renvoyer None."""
    import requests

    reponse = requests.get(URL_CABINET, cookies=cookies, timeout=20)
    if reponse.status_code != 200:
        print(f"  le cabinet répond {reponse.status_code} : session pas encore "
              f"authentifiée")
        return None

    texte = reponse.text
    try:
        session = texte.split('demoSessionId":"')[1].split('"')[0]
        uid = texte.split('uid":')[1].split(",")[0]
    except IndexError:
        print("  page reçue mais sans demoSessionId/uid : connexion incomplète, "
              "ou la page a changé de format")
        return None

    if not session or not uid:
        return None

    if demo:
        return ('42["auth",{"session":"%s","isDemo":1,"uid":%s,"platform":2,'
                '"isFastHistory":true,"isOptimized":true}]' % (session, uid))

    import urllib.parse
    reel = urllib.parse.unquote(cookies[COOKIE_REQUIS]).replace('"', '\\"')
    return ('42["auth",{"session":"%s","isDemo":0,"uid":%s,"platform":2,'
            '"isFastHistory":true,"isOptimized":true}]' % (reel, uid))


def _surveiller(window, resultat: dict, delai_sec: int, demo: bool) -> None:
    """Tourne dans un thread de pywebview pendant que la fenêtre est affichée."""
    limite = time.monotonic() + delai_sec
    dernier_etat = None

    while time.monotonic() < limite:
        cookies = _cookies_de(window)
        restant = int(limite - time.monotonic())

        if COOKIE_REQUIS in cookies:
            if dernier_etat != "authentifie":
                print(f"\n{COOKIE_REQUIS} détecté. Lecture de la session...")
                dernier_etat = "authentifie"
            ssid = _construire_ssid(cookies, demo)
            if ssid:
                resultat["ssid"] = ssid
                resultat["uid"] = ssid.split('"uid":')[1].split(",")[0]
                window.destroy()
                return
        elif dernier_etat != "attente":
            print(f"\nEn attente de connexion ({restant} s restantes).")
            print(f"Cookies vus : {', '.join(sorted(cookies)) or 'aucun'}")
            print(f"Attendu : {COOKIE_REQUIS}")
            dernier_etat = "attente"

        print(f"  ... {restant} s   ", end="\r", flush=True)
        time.sleep(2)

    resultat["erreur"] = (
        f"Délai de {delai_sec} s dépassé sans voir le cookie {COOKIE_REQUIS}."
    )
    window.destroy()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Capture le SSID Pocket Option.")
    ap.add_argument("--delai", type=int, default=300,
                    help="Délai maximal d'attente, en secondes (défaut : 300)")
    ap.add_argument("--reel", action="store_true",
                    help="Compte RÉEL au lieu du compte démo. Déconseillé.")
    ap.add_argument("--nouvelle-session", action="store_true",
                    help="Ouvre une fenêtre SANS cookies enregistrés. À "
                         "utiliser si la fenêtre se referme aussitôt : cela "
                         "signifie qu'une session valide existait déjà.")
    ap.add_argument("--envoyer", metavar="URL", default=None,
                    help="Envoie le jeton au service hébergé, qui reprend la "
                         "collecte seul. Ex. : https://mon-bot.onrender.com "
                         "Le secret est lu dans ADMIN_SECRET.")
    ap.add_argument("--afficher", action="store_true",
                    help="Affiche le SSID en clair. Utile seulement pour le "
                         "copier dans les variables d'un hébergeur.")
    args = ap.parse_args(argv)

    try:
        import webview
    except ImportError as erreur:
        print(f"pywebview manquant ({erreur}).\n"
              f"  .venv\\Scripts\\python -m pip install -e \".[pocketoption]\"",
              file=sys.stderr)
        return 2

    print("=" * 72)
    print("CAPTURE DU SSID POCKET OPTION")
    print("=" * 72)
    print(f"Compte      : {'RÉEL' if args.reel else 'DÉMO'}")
    print(f"Délai       : {args.delai} s")
    print()
    print("Une fenêtre va s'ouvrir. Connectez-vous, puis laissez-la.")
    print("Elle se fermera d'elle-même dès que la session sera lisible.")
    print()

    resultat: dict = {}
    debut = time.monotonic()
    fenetre = webview.create_window("Connexion Pocket Option", URL_CABINET)

    # `webview.start` est BLOQUANT : il tient la boucle graphique jusqu'à la
    # fermeture de la fenêtre. Toute la surveillance se fait donc dans le
    # callback, que pywebview exécute dans un thread séparé.
    # `private_mode` appartient à `start()`, pas à `create_window()`, et son
    # défaut est True — c'est-à-dire AUCUN cookie conservé. Passer False est
    # donc ce qui permet de retrouver une session d'une exécution précédente ;
    # True force une page de connexion vierge.
    webview.start(
        lambda w: _surveiller(w, resultat, args.delai, not args.reel),
        fenetre, private_mode=args.nouvelle_session,
    )

    immediat = (time.monotonic() - debut) < 10

    print()
    if "ssid" in resultat:
        from maxprofit.collect.pocketoption import ecrire_session

        fichier = ecrire_session(resultat["ssid"], demo=not args.reel,
                                 uid=resultat.get("uid"))
        print("=" * 72)
        print("SSID CAPTURÉ ET ENREGISTRÉ")
        print("=" * 72)
        print()
        print(f"Fichier : {fichier}")
        print(f"Compte  : {'RÉEL' if args.reel else 'DÉMO'}   "
              f"uid : {resultat.get('uid', '?')}")
        print(f"SSID    : {_masquer(resultat['ssid'])}")
        print()
        print("Rien à recopier : le collecteur et le diagnostic le reliront de")
        print("là. Ce fichier est dans .gitignore — c'est un jeton de session")
        print("complet, ne le partagez pas.")
        print()
        if immediat:
            print("La fenêtre s'est fermée aussitôt : une session valide")
            print("existait déjà dans les cookies enregistrés. C'est normal.")
            print("Pour vous connecter sur un AUTRE compte, relancez avec")
            print("--nouvelle-session.")
            print()
        # L'interpreteur COURANT, pas un chemin en dur : c'est celui avec
        # lequel l'utilisateur vient de lancer l'outil, donc celui qui marche.
        # Nommer `.venv` alors que la collecte tourne sous `.venv313` envoie
        # droit dans le mur.
        commande = sys.executable
        separateur = "\\" if os.name == "nt" else "/"
        if args.envoyer:
            code = _envoyer_au_serveur(args.envoyer, resultat["ssid"])
            if code != 0:
                return code
            print()

        print("Étape suivante :")
        print(f"    {commande} outils{separateur}diagnostic_pocketoption.py --duree 90")
        print()
        if args.afficher:
            print("SSID en clair (pour la configuration d'un hébergeur) :")
            print()
            print(f"POCKET_OPTION_SSID={resultat['ssid']}")
            print()
        else:
            print("Pour l'hébergement, relancez avec --afficher afin de le")
            print("copier dans les variables d'environnement de la plateforme :")
            print("le disque d'un conteneur est éphémère, le fichier n'y")
            print("survivrait pas au déploiement.")
        return 0

    print("=" * 72)
    print("ÉCHEC")
    print("=" * 72)
    print(resultat.get("erreur", "Fenêtre fermée avant la capture."))
    print()
    print("Pistes, par ordre de fréquence :")
    print("  - la connexion n'a pas abouti dans la fenêtre (vérifiez que vous")
    print("    voyez bien votre solde démo) ;")
    print("  - relancez avec --delai 600 si votre connexion est lente ;")
    print("  - relancez avec --nouvelle-session pour repartir de cookies vierges ;")
    print("  - en dernier recours, récupérez le SSID à la main : connectez-vous")
    print("    sur pocketoption.com dans votre navigateur habituel, ouvrez les")
    print("    outils de développement, onglet Réseau, filtre WS, et cherchez")
    print("    la trame émise qui commence par 42[\"auth\",{ — c'est le SSID.")
    return 1


def _envoyer_au_serveur(base_url: str, ssid: str) -> int:
    """POSTe le jeton au service hébergé.

    C'est ce qui supprime le copier-coller. Le serveur ne peut pas capturer le
    jeton lui-même — cela demande un navigateur, et les cookies naîtraient de
    toute façon sur la machine qui se connecte, pas sur la sienne. Mais rien
    n'oblige un humain à faire le transport.

    Le secret vient de `ADMIN_SECRET`, la même valeur que celle configurée sur
    l'hébergeur. Sans lui, la route n'existe même pas côté serveur : un point
    d'entrée acceptant un jeton de session sans authentification permettrait à
    quiconque connaît l'URL de détourner la collecte vers un autre compte.
    """
    import json
    import urllib.error
    import urllib.request

    secret = os.environ.get("ADMIN_SECRET", "").strip()
    if not secret:
        print("ADMIN_SECRET n'est pas défini : impossible d'authentifier "
              "l'envoi.", file=sys.stderr)
        print("Définissez la même valeur que sur l'hébergeur :", file=sys.stderr)
        print('    $env:ADMIN_SECRET = "..."', file=sys.stderr)
        return 2

    url = base_url.rstrip("/") + "/session"
    requete = urllib.request.Request(
        url, method="POST",
        data=json.dumps({"ssid": ssid}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Admin-Secret": secret},
    )
    print(f"Envoi à {url} ...")
    try:
        with urllib.request.urlopen(requete, timeout=30) as reponse:
            corps = json.loads(reponse.read().decode("utf-8"))
    except urllib.error.HTTPError as erreur:
        detail = erreur.read().decode("utf-8", "replace")[:300]
        print(f"Refus du serveur ({erreur.code}) : {detail}", file=sys.stderr)
        if erreur.code == 401:
            print("ADMIN_SECRET ne correspond pas à celui du serveur.",
                  file=sys.stderr)
        elif erreur.code == 404:
            print("La route est désactivée : ADMIN_SECRET n'est pas défini "
                  "côté serveur.", file=sys.stderr)
        return 1
    except urllib.error.URLError as erreur:
        print(f"Serveur injoignable : {erreur.reason}", file=sys.stderr)
        return 1

    print(f"Serveur : {corps.get('message', corps)}")
    print("La collecte reprend. Rien d'autre à faire.")
    return 0


def _masquer(ssid: str) -> str:
    """Assez pour reconnaître le jeton, pas assez pour s'en servir."""
    if len(ssid) <= 24:
        return "*" * len(ssid)
    return f"{ssid[:18]}...{ssid[-6:]}  ({len(ssid)} caractères)"


if __name__ == "__main__":
    raise SystemExit(main())
