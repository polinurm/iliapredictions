#!/usr/bin/env bash
# Сохраняет зашифрованный снимок базы в ветку `data` (одним коммитом, старый затирается).
# Вызывается из workflow каждые 10 минут и в конце работы.
set -euo pipefail

[ -f game.db ] || exit 0

# Безопасный снимок, даже если бот прямо сейчас пишет в базу
rm -f /tmp/snap.db
python3 - <<'PY'
import sqlite3
src = sqlite3.connect("game.db", timeout=20)
dst = sqlite3.connect("/tmp/snap.db")
src.backup(dst)
dst.close()
src.close()
PY

new=$(sha256sum /tmp/snap.db | cut -d' ' -f1)
if [ -f /tmp/dbhash ] && [ "$new" = "$(cat /tmp/dbhash)" ]; then
  echo "База не менялась, сохранение пропущено"
  exit 0
fi

openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:DB_KEY -in /tmp/snap.db -out /tmp/new.enc

REMOTE="${PUSH_URL:-https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git}"
rm -rf /tmp/pushrepo
mkdir /tmp/pushrepo
cd /tmp/pushrepo
git init -q -b data
git config user.name "predict-bot"
git config user.email "predict-bot@users.noreply.github.com"
cp /tmp/new.enc game.db.enc
git add game.db.enc
git commit -q -m "db"
git push -q --force "$REMOTE" data:data

echo "$new" > /tmp/dbhash
echo "База сохранена в ветку data"
