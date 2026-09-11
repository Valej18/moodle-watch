#!/bin/sh
# Prevenir quand un run echoue, et surtout dire QUOI FAIRE.
#
# Un serveur qui meurt en silence sur un jeton expire est un piege : le jeton
# Moodle delivre par SSO se renouvelle a la main, et rien d'autre ne le signale.
# Appele par ExecStopPost, qui recoit $EXIT_STATUS de systemd.
#
# Ecrit en `if` explicites et non en listes `A || B && exit` : le traitement de
# set -e sur une liste AND-OR differe entre bash en mode sh et dash, donc un tel
# script peut marcher sur un Mac et sortir trop tot sur un Ubuntu.
#
# Aucun secret ici : le jeton du bot vient de l'EnvironmentFile.

set -eu

statut="${EXIT_STATUS:-0}"
if [ "$statut" = "0" ]; then
    exit 0
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "moodle-watch: run en echec (code $statut), aucune notification configuree" >&2
    exit 0
fi

if [ "$statut" = "2" ]; then
    message="moodle-watch : le jeton Moodle a expire.
Le renouveler par :
  moodle-dl -nt -sso --path '${MOODLE_WATCH_MIRROR:-?}'"
else
    message="moodle-watch : run en echec (code $statut).
  journalctl -u moodle-watch-sync -n 50"
fi

curl -sS -m 15 -o /dev/null \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${message}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" || true
