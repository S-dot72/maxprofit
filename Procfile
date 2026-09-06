# Un seul processus. Le Procfile du dépôt modèle déclare deux lignes `worker:`,
# dont la seconde écrase silencieusement la première : le format n'autorise
# qu'une entrée par nom de process, et c'est la dernière qui gagne. Son
# `signal_bot.py` ne tournait donc pas.
#
# `web:` et non `worker:` : c'est le type de process auquel l'hébergeur
# attribue $PORT et sur lequel il branche son health check.
web: python -m maxprofit.hosting.service --source po --min-payout ${MIN_PAYOUT_PCT}
