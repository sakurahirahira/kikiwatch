#!/usr/bin/env python3
"""
kikiwatch - iCloud Voice Memos 自動監視・kikitoru連携デーモン
"""

import hashlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# .env 読み込み
load_dotenv()

# 環境変数
WATCH_DIR = Path(
    os.environ.get(
        "WATCH_DIR",
        "~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings",
    )
).expanduser()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
KIKITORU_CMD = os.environ.get("KIKITORU_CMD", "kikitoru")
MEDIA_DIR = os.environ.get("MEDIA_DIR", "")

# ログ設定
LOG_DIR = Path("~/.kikiwatch").expanduser()
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "kikiwatch.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# サイズ安定確認パラメータ
STABILITY_INTERVAL = 3      # 秒
STABILITY_COUNT = 2         # 連続同一サイズ回数
STABILITY_TIMEOUT = 60      # 最大待機秒数


def compute_md5(file_path: Path) -> str:
    """ファイルの MD5 ハッシュを計算する。"""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_already_processed(file_hash: str) -> bool:
    """DB で二重処理チェックを行う。"""
    if not DATABASE_URL:
        logger.warning("DATABASE_URL が未設定のため二重処理チェックをスキップします")
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM recordings WHERE file_hash = %s AND user_id = 1",
                    (file_hash,),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        logger.error("DB チェック中にエラーが発生しました: %s", e)
        return False


def wait_for_stable_size(file_path: Path) -> bool:
    """ファイルサイズが安定するまで待つ。

    Returns:
        True: 安定した（処理可能）
        False: タイムアウト
    """
    stable_count = 0
    prev_size = -1
    elapsed = 0

    while elapsed < STABILITY_TIMEOUT:
        try:
            current_size = file_path.stat().st_size
        except FileNotFoundError:
            logger.warning("ファイルが見つかりません（削除された可能性）: %s", file_path)
            return False

        if current_size == prev_size and current_size > 0:
            stable_count += 1
            if stable_count >= STABILITY_COUNT:
                logger.info(
                    "ファイルサイズ安定確認 (%d bytes): %s", current_size, file_path.name
                )
                return True
        else:
            stable_count = 0

        prev_size = current_size
        time.sleep(STABILITY_INTERVAL)
        elapsed += STABILITY_INTERVAL

    logger.warning("ファイルサイズ安定待ちがタイムアウトしました: %s", file_path.name)
    return False


def run_kikitoru(file_path: Path) -> None:
    """kikitoru transcribe --use-db <ファイルパス> を実行する。"""
    cmd = [KIKITORU_CMD, "transcribe", "--use-db", str(file_path)]
    env = os.environ.copy()
    if DATABASE_URL:
        env["KIKITORU_DB_URL"] = DATABASE_URL
    hf_token = os.environ.get("HF_TOKEN", "")
    if hf_token:
        env["HF_TOKEN"] = hf_token
    if MEDIA_DIR:
        env["KIKITORU_MEDIA_DIR"] = MEDIA_DIR
    logger.info("kikitoru 実行開始: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        if result.returncode == 0:
            logger.info("kikitoru 正常終了: %s", file_path.name)
            if result.stdout:
                logger.debug("stdout:\n%s", result.stdout.strip())
        else:
            logger.error(
                "kikitoru 異常終了 (returncode=%d): %s", result.returncode, file_path.name
            )
            if result.stderr:
                logger.error("stderr:\n%s", result.stderr.strip())
    except FileNotFoundError:
        logger.error("kikitoru コマンドが見つかりません: %s", KIKITORU_CMD)
    except Exception as e:
        logger.error("kikitoru 実行中にエラーが発生しました: %s", e)


def process_file(file_path: Path) -> None:
    """新規 .m4a ファイルを処理する。"""
    if file_path.suffix.lower() != ".m4a":
        return

    logger.info("新規ファイル検知: %s", file_path.name)

    # iCloud 同期完了まで待機
    if not wait_for_stable_size(file_path):
        return

    # MD5 計算
    try:
        file_hash = compute_md5(file_path)
    except Exception as e:
        logger.error("MD5 計算中にエラーが発生しました: %s", e)
        return

    # 二重処理チェック
    if is_already_processed(file_hash):
        logger.info("既に処理済みのためスキップ: %s (hash=%s)", file_path.name, file_hash)
        return

    # kikitoru 実行
    run_kikitoru(file_path)


class VoiceMemoHandler(FileSystemEventHandler):
    """Voice Memos ディレクトリのイベントハンドラ。"""

    def on_created(self, event):
        if not event.is_directory:
            process_file(Path(event.src_path))

    def on_moved(self, event):
        # iCloud が一時ファイルから移動してくるケースを捕捉
        if not event.is_directory:
            process_file(Path(event.dest_path))


def scan_existing_files() -> None:
    """起動時に既存ファイルを DB ハッシュと照合してスキップ確認（ログ出力のみ）。"""
    logger.info("既存ファイルのスキャン開始: %s", WATCH_DIR)
    m4a_files = list(WATCH_DIR.rglob("*.m4a"))
    logger.info("既存 .m4a ファイル数: %d", len(m4a_files))

    for file_path in m4a_files:
        try:
            file_hash = compute_md5(file_path)
            if is_already_processed(file_hash):
                logger.debug("既処理済み（スキップ）: %s", file_path.name)
            else:
                logger.info("未処理ファイル検出（監視中に変化があれば処理）: %s", file_path.name)
        except Exception as e:
            logger.warning("既存ファイルスキャン中にエラー: %s - %s", file_path.name, e)

    logger.info("既存ファイルのスキャン完了")


def main() -> None:
    if not WATCH_DIR.exists():
        logger.error("監視ディレクトリが存在しません: %s", WATCH_DIR)
        sys.exit(1)

    logger.info("kikiwatch 起動")
    logger.info("監視ディレクトリ: %s", WATCH_DIR)
    logger.info("kikitoru コマンド: %s", KIKITORU_CMD)
    logger.info("ログファイル: %s", LOG_FILE)

    # 既存ファイルスキャン
    scan_existing_files()

    # FSEvents 監視開始
    event_handler = VoiceMemoHandler()
    observer = Observer()
    observer.schedule(event_handler, str(WATCH_DIR), recursive=True)
    observer.start()
    logger.info("監視開始 (FSEvents)")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("kikiwatch 停止中...")
        observer.stop()

    observer.join()
    logger.info("kikiwatch 終了")


if __name__ == "__main__":
    main()
