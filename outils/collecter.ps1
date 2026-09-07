<#
.SYNOPSIS
    Lance la collecte en continu sur ce poste, et l'y maintient.

.DESCRIPTION
    Il n'existe pas, en 2026, d'hébergement gratuit offrant un disque
    persistant : Render réserve les disques aux offres payantes et endort les
    instances gratuites, Fly.io n'a plus de tier gratuit, Railway suspend ses
    services quand le crédit d'essai est épuisé. Sur un disque éphémère, la base
    repart vide à chaque redémarrage — on croirait collecter, et l'on
    n'accumulerait rien.

    Collecter depuis ce poste ne coûte rien et fait presque aussi bien. Les
    alertes Telegram partent quand même : elles n'ont besoin que d'une sortie
    HTTPS. Le disque est plus fiable que celui d'un conteneur. Et le
    renouvellement du jeton devient immédiat, la capture et la collecte étant
    sur la même machine.

    Le seul inconvénient est l'uptime, et c'est exactement ce que la table
    `uptime` mesure : le backtest refuse de générer un signal sur une fenêtre
    qui chevauche un trou de connexion (§2.4). Une interruption est une donnée
    manquante déclarée, pas une donnée fausse.

    Ce script fait trois choses que lancer la commande à la main ne fait pas :

      - il EMPÊCHE LA MISE EN VEILLE. Sans cela, la collecte s'arrête au premier
        écran noir et vous récoltez trois heures au lieu de quatorze jours ;
      - il RELANCE le service s'il s'arrête, avec un délai croissant ;
      - il JOURNALISE dans un fichier, à côté de la base — donc hors du
        répertoire de code (§1.1).

.EXAMPLE
    .\outils\collecter.ps1

.EXAMPLE
    .\outils\collecter.ps1 -Source sim
    Répète avec la source simulée, pour vérifier la chaîne sans le broker.
#>

[CmdletBinding()]
param(
    [ValidateSet('po', 'sim')]
    [string]$Source = 'po',

    # Faire tourner AUSSI le bot Telegram sur ce poste.
    #
    # Par defaut NON, et c'est important : Telegram n'autorise qu'un seul
    # consommateur de `getUpdates` a la fois. Si le bot tourne deja sur
    # l'hebergeur, en lancer un second ici les fait se voler les messages a tour
    # de role -- une commande sur deux disparait, sans erreur nulle part.
    #
    # Le partage naturel est donc : le collecteur ici, ou le broker accepte de
    # diffuser ; le bot et la sonde sur l'hebergeur, joignables en permanence.
    # Les deux ecrivent et lisent la meme base Turso.
    [switch]$AvecBot,

    # Délai maximal entre deux tentatives, en secondes. Le délai double après
    # chaque échec sans jamais dépasser cette valeur : un broker en maintenance
    # ne doit pas être martelé, mais la collecte doit reprendre vite quand il
    # revient.
    [int]$AttenteMaxSec = 300
)

$ErrorActionPreference = 'Stop'
$racine = Split-Path -Parent $PSScriptRoot
$python = Join-Path $racine '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Error "Environnement virtuel introuvable : $python`nCréez-le : python -m venv .venv"
    exit 2
}

# --- Empêcher la mise en veille -------------------------------------------
# SetThreadExecutionState signale au système que ce processus doit rester
# actif. ES_CONTINUOUS rend l'état persistant jusqu'à la fin du script ;
# ES_SYSTEM_REQUIRED empêche la veille. L'écran, lui, peut s'éteindre : seul le
# système doit rester debout.
# Les indicateurs sont écrits en décimal : `0x80000000 -bor 0x00000001` produit
# un entier SIGNÉ que PowerShell refuse de convertir en UInt32, et l'appel
# échoue silencieusement — la veille resterait active et la collecte s'arrêterait
# au premier écran noir.
$ES_CONTINUOUS = [uint32]2147483648        # 0x80000000
$ES_SYSTEM_REQUIRED = [uint32]2147483649   # 0x80000000 | 0x00000001

$signature = @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
try {
    $veille = Add-Type -MemberDefinition $signature -Name 'Veille' `
        -Namespace 'MaxProfit' -PassThru
    $veilleBloquee = $veille::SetThreadExecutionState($ES_SYSTEM_REQUIRED) -ne 0
} catch {
    $veilleBloquee = $false
}

# --- Où écrire le journal --------------------------------------------------
# À côté de la base, jamais dans le répertoire de code : un journal de quatorze
# jours n'a rien à faire dans un dépôt git, et le §1.1 vaut aussi pour lui.
# Python écrit en UTF-8 ; sans cette ligne, PowerShell décode sa sortie avec la
# page de codes de la console (CP850 ici) puis la réencode en UTF-8 — chaque
# accent finit en « ├⌐ » dans un journal de quatorze jours.
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$cheminBase = $env:TRADING_DB_PATH
if (-not $cheminBase) {
    $dotenv = Join-Path $racine '.env'
    if (Test-Path $dotenv) {
        $ligne = Select-String -Path $dotenv -Pattern '^\s*TRADING_DB_PATH\s*=' |
                 Select-Object -First 1
        if ($ligne) {
            $cheminBase = ($ligne.Line -split '=', 2)[1].Trim().Trim('"').Trim("'")
        }
    }
}
if (-not $cheminBase) {
    Write-Error @"
TRADING_DB_PATH n'est défini ni dans l'environnement ni dans .env.

Aucune valeur par défaut n'est appliquée : une base créée à la volée dans un
chemin inattendu part vide et se perd (spec 1.1). Définissez-la, par exemple :

    `$env:TRADING_DB_PATH = "`$HOME\trading_data\market.db"
    mkdir `$HOME\trading_data -Force
"@
    exit 2
}
$dossier = Split-Path -Parent $cheminBase
$journal = Join-Path $dossier 'collecte.log'

# --- Turso : la question qui decide de tout --------------------------------
# Sans ces variables, la collecte ecrit dans un fichier local que personne
# d'autre ne voit. On collecterait quatorze jours pour decouvrir que le bot
# heberge lit une base vide. Mieux vaut le dire avant de commencer qu'apres.
$tursoUrl = $env:TURSO_DATABASE_URL
if (-not $tursoUrl) {
    $dotenv = Join-Path $racine '.env'
    if (Test-Path $dotenv) {
        $ligne = Select-String -Path $dotenv -Pattern '^\s*TURSO_DATABASE_URL\s*=' |
                 Select-Object -First 1
        if ($ligne) {
            $tursoUrl = ($ligne.Line -split '=', 2)[1].Trim().Trim('"').Trim("'")
        }
    }
}
$destination = if ($tursoUrl) {
    "Turso ($tursoUrl), base partagee avec l'hebergeur"
} else {
    'FICHIER LOCAL SEUL — le bot heberge ne verra rien'
}

Write-Host ('=' * 72)
Write-Host 'COLLECTE CONTINUE'
Write-Host ('=' * 72)
Write-Host "Source        : $Source"
Write-Host "Base          : $cheminBase"
Write-Host "Destination   : $destination"
Write-Host "Journal       : $journal"
Write-Host ("Mise en veille: " + $(if ($veilleBloquee) { 'bloquée' } else { 'NON bloquée — la collecte s''arrêtera à la veille' }))
Write-Host ''
Write-Host 'Ctrl+C pour arrêter. Les tampons sont vidés avant la sortie.'
Write-Host ''

$tentative = 0
$attente = 5

try {
    while ($true) {
        $tentative++
        $debut = Get-Date
        Write-Host "[$($debut.ToString('HH:mm:ss'))] Démarrage n°$tentative"

        # Pas de `2>&1` : le service journalise sur stdout, et rediriger la
        # sortie d'erreur d'un exécutable natif ferait envelopper chaque ligne
        # dans un objet d'erreur PowerShell.
        #
        # Pas de Tee-Object non plus : sous PowerShell 5.1 il écrit en UTF-16,
        # et un journal de quatorze jours illisible par les outils habituels ne
        # sert à rien. Add-Content -Encoding UTF8 fait le même travail
        # correctement.
        # Le collecteur seul par defaut : voir -AvecBot ci-dessus.
        $module = if ($AvecBot) { 'maxprofit.hosting.service' }
                  else { 'maxprofit.collect.collector' }
        & $python -m $module --source $Source |
            ForEach-Object {
                Write-Host $_
                Add-Content -Path $journal -Value $_ -Encoding UTF8
            }
        $code = $LASTEXITCODE

        $duree = (Get-Date) - $debut
        Write-Host ("[$((Get-Date).ToString('HH:mm:ss'))] Arrêt (code $code) " +
                    "après $([int]$duree.TotalMinutes) min")

        # Le service sort en 2 sur une erreur de configuration : la relancer
        # n'y changerait rien, et boucler masquerait le message.
        if ($code -eq 2) {
            Write-Host ''
            Write-Host 'Erreur de configuration : voir le message ci-dessus.'
            Write-Host 'Rien à relancer tant qu''elle n''est pas corrigée.'
            break
        }

        # Un service qui a tenu longtemps puis s'arrête n'est pas dans le même
        # cas qu'un service qui échoue au démarrage : on remet le délai à zéro
        # pour reprendre vite après une coupure passagère.
        if ($duree.TotalMinutes -ge 5) { $attente = 5 }

        Write-Host "Relance dans $attente s..."
        Start-Sleep -Seconds $attente
        $attente = [Math]::Min($attente * 2, $AttenteMaxSec)
    }
}
finally {
    if ($veilleBloquee) {
        # ES_CONTINUOUS seul : rend au système le droit de se mettre en veille.
        $null = $veille::SetThreadExecutionState($ES_CONTINUOUS)
        Write-Host 'Mise en veille rétablie.'
    }
}
