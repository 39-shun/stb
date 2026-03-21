#!/bin/bash
# ============================================================
# deploy.sh - スタバ用 データ自動デプロイスクリプト
# cron例: 30 14 * * * bash /home/pi/stb/deploy.sh >> /home/pi/stb/logs/deploy.log 2>&1
# ============================================================

set -euo pipefail

REPO_DIR="/home/pi/stb"
LOG_PREFIX="[stb deploy.sh]"

echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') 開始"

cd "${REPO_DIR}"

# 未追跡ファイルチェック
if git diff --quiet HEAD -- data/ && ! git ls-files --others --exclude-standard data/ | grep -q .; then
  echo "${LOG_PREFIX} data/ に変更なし。スキップします。"
  exit 0
fi

git add data/

if git diff --cached --quiet; then
  echo "${LOG_PREFIX} ステージングに変更なし。スキップします。"
  exit 0
fi

CHANGED_FILES=$(git diff --cached --name-only | wc -l)
COMMIT_MSG="data: update ${CHANGED_FILES} file(s) at $(date '+%Y-%m-%d %H:%M')"
git commit -m "${COMMIT_MSG}"

for i in 1 2 3; do
  if git push origin main; then
    echo "${LOG_PREFIX} プッシュ成功: ${COMMIT_MSG}"
    exit 0
  fi
  echo "${LOG_PREFIX} プッシュ失敗 (試行 ${i}/3)。30秒後リトライ..."
  sleep 30
done

echo "${LOG_PREFIX} ERROR: プッシュが3回失敗しました。"
exit 1
