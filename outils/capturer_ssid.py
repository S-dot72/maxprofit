#!/usr/bin/env python
r"""
Capture du SSID Pocket Option — à faire UNE fois.

    .venv\Scripts\python.exe outils\capturer_ssid.py
    .venv/bin/python outils/capturer_ssid.py          # Linux/mac

Une fenêtre s'ouvre. Connectez-vous sur votre compte DÉMO. Dès que la session
est détectée, la fenêtre se ferme et le SSID s'affiche : copiez-le dans `.env`
sous `POCKET_OPTION_SSID`. Tout le reste du projet le lira de là, et plus
aucune fenêtre ne s'ouvrira jamais.

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

C'est un jeton de session complet : quiconque l'a peut agir sur le compte. Ne
le commitez pas (`.env` est dans `.gitignore`), ne le collez pas dans une
conversation. Il expire — quand la collecte s'arrêtera sur une erreur
d'authentification, relancez cet outil.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

#: Le seul cookie qui compte : c'est lui qui authentifie la requête vers le
#: cabinet, d'où l'on extrait l'identifiant de session et l'uid.
COOKIE_REQUIS = "ci_session"

URL_CABINET = "https://pocketoption.com/en/cabinet/demo-quick-high-low"


def _verifier_interpreteur() -> None:
    attendu = (RACINE / ".venv" / "Scripts" / "python.exe" if os.name == "nt"
               else RACINE / ".venv" / "bin" / "python")
    if not attendu.exists():
        return
    try:
        if Path(sys.executable).resolve() == attendu.resolve():
            return
    except OSError:
        return
    print(f"Mauvais interpréteur Python.\n"
          f"  utilisé : {sys.executable}\n"
          f"  attendu : {attendu}\n", file=sys.stderr)
    raise SystemExit(2)


_verifier_interpreteur()


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
    fenetre = webview.create_window("Connexion Pocket Option", URL_CABINET)

    # `webview.start` est BLOQUANT : il tient la boucle graphique jusqu'à la
    # fermeture de la fenêtre. Toute la surveillance se fait donc dans le
    # callback, que pywebview exécute dans un thread séparé.
    webview.start(
        lambda w: _surveiller(w, resultat, args.delai, not args.reel),
        fenetre, private_mode=False,
    )

    print()
    if "ssid" in resultat:
        print("=" * 72)
        print("SSID CAPTURÉ")
        print("=" * 72)
        print()
        print(resultat["ssid"])
        print()
        print(f"uid : {resultat.get('uid', '?')}")
        print()
        print("Ajoutez cette ligne à votre fichier .env (jamais commité) :")
        print()
        print(f"POCKET_OPTION_SSID={resultat['ssid']}")
        print()
        print("Puis lancez le diagnostic :")
        commande = (".\\.venv\\Scripts\\python.exe" if os.name == "nt"
                    else ".venv/bin/python")
        print(f"    {commande} outils/diagnostic_pocketoption.py --duree 90")
        print()
        print("C'est un jeton de session complet : ne le partagez pas, ne le "
              "commitez pas. Il expire — relancez cet outil le jour où la "
              "collecte s'arrête sur une erreur d'authentification.")
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
    print("  - en dernier recours, récupérez le SSID à la main : connectez-vous")
    print("    sur pocketoption.com dans votre navigateur habituel, ouvrez les")
    print("    outils de développement, onglet Réseau, filtre WS, et cherchez")
    print("    la trame émise qui commence par 42[\"auth\",{ — c'est le SSID,")
    print("    à copier tel quel.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
