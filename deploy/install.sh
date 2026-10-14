#!/bin/sh
# One-command setup on a fresh Ubuntu/Debian server:
#   curl -fsSL https://raw.githubusercontent.com/rachimanna/secret-ai/main/deploy/install.sh | sh
# Installs Docker, downloads the bot, asks for the settings, gets HTTPS and keeps the bot updated from GitHub.
set -e
[ "$(id -u)" = 0 ] || { echo "Запусти от root (sudo -i)"; exit 1; }
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
command -v git >/dev/null || (apt-get update -qq && apt-get install -y -qq git)
[ -d /opt/secret-ai/.git ] || git clone -q https://github.com/rachimanna/secret-ai.git /opt/secret-ai
cd /opt/secret-ai/deploy
if [ ! -f .env ]; then
  cp env.example .env
  echo ""
  echo ">>> Сейчас откроется файл настроек. Вставь значения из Render → Environment,"
  echo ">>> потом Ctrl+O, Enter (сохранить) и Ctrl+X (выйти)."
  sleep 3
  command -v nano >/dev/null || apt-get install -y -qq nano
  nano .env < /dev/tty
fi
if ! grep -q '^DOMAIN=' .env; then
  ip=$(curl -fsS https://api.ipify.org)
  echo "DOMAIN=$(echo "$ip" | tr . -).sslip.io" >> .env
fi
domain=$(grep '^DOMAIN=' .env | cut -d= -f2)
grep -q '^PUBLIC_URL=' .env || echo "PUBLIC_URL=https://$domain" >> .env
export DOMAIN="$domain"
docker compose --env-file .env up -d --build
chmod +x update.sh
( crontab -l 2>/dev/null | grep -v secret-ai; echo "*/5 * * * * DOMAIN=$domain /opt/secret-ai/deploy/update.sh" ) | crontab -
echo ""
echo "✅ Готово. Бот запущен, Mini App: https://$domain"
echo "   Логи: cd /opt/secret-ai/deploy && docker compose logs -f bot"
