#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.sakurahirahira.kikiwatch.plist"
PLIST_SRC="${SCRIPT_DIR}/${PLIST_NAME}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_DST="${LAUNCH_AGENTS_DIR}/${PLIST_NAME}"
LOG_DIR="${HOME}/.kikiwatch"

echo "=== kikiwatch インストール ==="

# .env が存在するか確認
if [ ! -f "${SCRIPT_DIR}/.env" ]; then
    echo "エラー: .env ファイルが見つかりません。"
    echo ".env.example をコピーして設定してください:"
    echo "  cp ${SCRIPT_DIR}/.env.example ${SCRIPT_DIR}/.env"
    exit 1
fi

# ~/.kikiwatch/ ディレクトリ作成
echo "ログディレクトリを作成します: ${LOG_DIR}"
mkdir -p "${LOG_DIR}"

# plist 内の PLACEHOLDER_USER を現在のユーザー名に置換してコピー
echo "plist を ${PLIST_DST} にインストールします"
mkdir -p "${LAUNCH_AGENTS_DIR}"
sed "s/PLACEHOLDER_USER/$(whoami)/g" "${PLIST_SRC}" > "${PLIST_DST}"

# 既に登録済みの場合はアンロード
if launchctl list | grep -q "com.sakurahirahira.kikiwatch" 2>/dev/null; then
    echo "既存の登録を解除します..."
    launchctl unload "${PLIST_DST}" 2>/dev/null || true
fi

# launchctl load で登録
echo "launchd に登録します..."
launchctl load "${PLIST_DST}"

echo ""
echo "=== インストール完了 ==="
echo "サービス名 : com.sakurahirahira.kikiwatch"
echo "plist      : ${PLIST_DST}"
echo "ログ       : ${LOG_DIR}/kikiwatch.log"
echo ""
echo "状態確認:"
echo "  launchctl list | grep kikiwatch"
echo "停止:"
echo "  launchctl unload ${PLIST_DST}"
