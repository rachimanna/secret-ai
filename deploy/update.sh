#!/bin/sh
# Run by cron every few minutes: if GitHub has a new commit on main, rebuild and restart the bot.
cd /opt/secret-ai || exit 1
git fetch -q origin main || exit 0
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && exit 0
git reset -q --hard origin/main
cd deploy && docker compose up -d --build >> /var/log/secret-ai-update.log 2>&1
docker image prune -f > /dev/null 2>&1
