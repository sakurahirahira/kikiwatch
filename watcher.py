#!/usr/bin/env python3
"""
kikiwatch - iCloud Voice Memos 自動監視・kikitoru連携デーモン
"""

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

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

# KIKIWATCH_ENV=dev → 開発DB、KIKIWATCH_ENV=prod → 本番DB
_env = os.environ.get("KIKIWATCH_ENV", "prod").strip().lower()
if _env == "dev":
    DATABASE_URL = os.environ.get("DATABASE_URL_DEV", "")
    MEDIA_DIR = os.environ.get("MEDIA_DIR_DEV", "")
    REMOTE_HOST = ""
    REMOTE_MEDIA_DIR = ""
else:
    DATABASE_URL = os.environ.get("DATABASE_URL_PROD", "")
    MEDIA_DIR = os.environ.get("MEDIA_DIR_DEV", "")   # ローカルで処理してN100へSCP
    REMOTE_HOST = os.environ.get("REMOTE_HOST", "")
    REMOTE_MEDIA_DIR = os.environ.get("REMOTE_MEDIA_DIR", "")

KIKITORU_CMD = os.environ.get("KIKITORU_CMD", "kikitoru")

SUPPORTED_EXTENSIONS = {".m4a", ".qta"}

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


def is_already_processed(file_hash: str, check_path: str = "") -> bool:
    """DB で二重処理チェックを行う。
    file_hashで照合し、見つからない場合はcheck_pathでも照合する
    （KIKITORU_FILE_PATH_OVERRIDEでfile_hashがNULL登録される場合の対策）。
    """
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
                if cur.fetchone() is not None:
                    return True
                if check_path:
                    cur.execute(
                        "SELECT id FROM recordings WHERE file_path = %s AND user_id = 1",
                        (check_path,),
                    )
                    return cur.fetchone() is not None
                return False
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


def get_custom_vocabulary() -> Optional[str]:
    """settings テーブルから custom_vocabulary を取得して initial_prompt 文字列を返す。"""
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM settings WHERE key = 'custom_vocabulary' AND user_id = 1"
                )
                row = cur.fetchone()
            if row and row[0].strip():
                words = [w.strip() for w in row[0].replace('、', ',').split(',') if w.strip()]
                return '、'.join(words) if words else None
            return None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("カスタム辞書の取得に失敗しました: %s", e)
        return None


def run_kikitoru(file_path: Path) -> bool:
    """kikitoru transcribe --use-db <ファイルパス> を実行する。成功時True。"""
    cmd = ["caffeinate", "-i", KIKITORU_CMD, "transcribe", "--use-db", str(file_path)]
    env = os.environ.copy()
    # launchd は PATH が狭いので Homebrew パスを明示的に追加
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + env.get("PATH", "")
    if DATABASE_URL:
        env["KIKITORU_DB_URL"] = DATABASE_URL
    hf_token = os.environ.get("HF_TOKEN", "")
    if hf_token:
        env["HF_TOKEN"] = hf_token
    if MEDIA_DIR:
        env["KIKITORU_MEDIA_DIR"] = MEDIA_DIR
    # prodモード: DBには最初からN100の最終パスを保存する
    if REMOTE_MEDIA_DIR:
        env["KIKITORU_FILE_PATH_OVERRIDE"] = f"{REMOTE_MEDIA_DIR}/{file_path.name}"
    # カスタム辞書があれば --initial-prompt として渡す
    vocab = get_custom_vocabulary()
    if vocab:
        cmd += ["--initial-prompt", vocab]
        logger.info("カスタム辞書を使用: %s", vocab)
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
            return True
        else:
            logger.error(
                "kikitoru 異常終了 (returncode=%d): %s", result.returncode, file_path.name
            )
            if result.stderr:
                logger.error("stderr:\n%s", result.stderr.strip())
            return False
    except FileNotFoundError:
        logger.error("kikitoru コマンドが見つかりません: %s", KIKITORU_CMD)
        return False
    except Exception as e:
        logger.error("kikitoru 実行中にエラーが発生しました: %s", e)
        return False


def convert_qta_to_m4a(qta_path: Path) -> Optional[Path]:
    """.qta ファイルを .m4a に変換する（AAC トラックを抽出）。"""
    out_dir = Path(MEDIA_DIR) if MEDIA_DIR else qta_path.parent
    out_path = out_dir / (qta_path.stem + ".m4a")
    logger.info("qta→m4a 変換開始: %s → %s", qta_path.name, out_path.name)
    try:
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        result = subprocess.run(
            [ffmpeg, "-i", str(qta_path), "-map", "0:0", "-c", "copy", str(out_path), "-y"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            logger.error("ffmpeg 変換失敗: %s", result.stderr.strip())
            return None
        logger.info("qta→m4a 変換完了: %s", out_path.name)
        return out_path
    except Exception as e:
        logger.error("qta 変換中にエラー: %s", e)
        return None


def transfer_to_remote(local_path: Path) -> bool:
    """音声ファイルをリモートサーバーへSCPで転送する。
    DBのfile_pathはkikitoruがKIKITORU_FILE_PATH_OVERRIDEで最初から正しいパスを保存するため更新不要。
    """
    if not REMOTE_HOST or not REMOTE_MEDIA_DIR:
        return False

    remote_path = f"{REMOTE_MEDIA_DIR}/{local_path.name}"
    logger.info("N100へ転送中: %s → %s:%s", local_path.name, REMOTE_HOST, remote_path)

    try:
        result = subprocess.run(
            ["scp", str(local_path), f"{REMOTE_HOST}:{remote_path}"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.error("SCP失敗: %s", result.stderr.strip())
            return False
        logger.info("SCP完了: %s", local_path.name)
        return True
    except Exception as e:
        logger.error("SCP中にエラー: %s", e)
        return False


def _enqueue_scp_pending(local_path: Path) -> None:
    """SCP失敗ファイルを保留キューに追加する。"""
    import json
    entry = {"local_path": str(local_path), "failed_at": time.time()}
    try:
        with open(SCP_PENDING_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("SCP保留キューに追加: %s", local_path.name)
    except Exception as e:
        logger.error("SCP保留キュー書き込みエラー: %s", e)


def poll_scp_pending() -> None:
    """保留中のSCPファイルをリトライする（バックグラウンドスレッドから呼ばれる）。"""
    import json
    if not SCP_PENDING_FILE.exists():
        return

    try:
        with open(SCP_PENDING_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        logger.error("SCP保留キュー読み込みエラー: %s", e)
        return

    if not lines:
        return

    remaining = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            local_path = Path(entry["local_path"])
        except Exception:
            continue  # 壊れた行はスキップして削除

        if not local_path.exists():
            logger.warning("SCP保留ファイルが見つかりません（スキップ）: %s", local_path.name)
            continue  # ファイルなければキューから削除

        logger.info("SCPリトライ: %s", local_path.name)
        if transfer_to_remote(local_path):
            logger.info("SCPリトライ成功: %s", local_path.name)
        else:
            remaining.append(line)  # 失敗したままキューに残す

    # キューを更新（成功分を除いて書き直し）
    if remaining:
        with open(SCP_PENDING_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(remaining) + "\n")
    else:
        try:
            SCP_PENDING_FILE.unlink()
        except FileNotFoundError:
            pass


def process_file(file_path: Path) -> None:
    """新規音声ファイルを処理する（.m4a / .qta 対応）。"""
    ext = file_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return

    logger.info("新規ファイル検知: %s", file_path.name)

    # iCloud 同期完了まで待機
    if not wait_for_stable_size(file_path):
        return

    # .qta → .m4a 変換（MD5チェック前に変換してm4aのハッシュで照合）
    if ext == ".qta":
        target_path = convert_qta_to_m4a(file_path)
        if target_path is None:
            return
    else:
        target_path = file_path

    # MD5 計算（変換後ファイルで重複チェック）
    try:
        file_hash = compute_md5(target_path)
    except Exception as e:
        logger.error("MD5 計算中にエラーが発生しました: %s", e)
        return

    # N100最終パスでの照合パス（KIKITORU_FILE_PATH_OVERRIDE使用時の重複チェック用）
    check_path = f"{REMOTE_MEDIA_DIR}/{target_path.name}" if REMOTE_MEDIA_DIR else ""

    # 二重処理チェック
    if is_already_processed(file_hash, check_path):
        logger.info("既に処理済みのためスキップ: %s", file_path.name)
        return

    # kikitoru 実行（成功時のみ転送）
    success = run_kikitoru(target_path)

    # N100へ音声ファイルを転送（DBパスはkikitoruが最初から正しいパスを保存済み）
    if success and MEDIA_DIR:
        local_media_path = Path(MEDIA_DIR) / target_path.name
        if local_media_path.exists():
            if not transfer_to_remote(local_media_path):
                _enqueue_scp_pending(local_media_path)


class VoiceMemoHandler(FileSystemEventHandler):
    """Voice Memos ディレクトリのイベントハンドラ。"""

    def on_created(self, event):
        if not event.is_directory:
            process_file(Path(event.src_path))

    def on_moved(self, event):
        # iCloud が一時ファイルから移動してくるケースを捕捉
        if not event.is_directory:
            process_file(Path(event.dest_path))


RECENT_FILE_HOURS = 24        # 起動時スキャンの遡り時間（時間）
MANUAL_SCAN_FILE_HOURS = 168  # 手動スキャンボタン時の遡り時間（7日間）


def scan_existing_files(max_age_hours: float = RECENT_FILE_HOURS) -> None:
    """既存ファイルを DB ハッシュと照合し、未処理ファイルを自動処理する。

    max_age_hours: この時間以内に更新されたファイルを対象にする（デフォルト24h、手動スキャン時は7日）
    """
    logger.info("既存ファイルのスキャン開始 (遡り: %.0fh): %s", max_age_hours, WATCH_DIR)
    audio_files = []
    for ext in SUPPORTED_EXTENSIONS:
        audio_files.extend(WATCH_DIR.rglob(f"*{ext}"))
    logger.info("既存音声ファイル数: %d", len(audio_files))

    now = time.time()
    recent_unprocessed = []
    for file_path in audio_files:
        try:
            file_hash = compute_md5(file_path)
            # .qtaは変換後の.m4aファイル名でN100パスを照合
            target_name = (file_path.stem + ".m4a"
                           if file_path.suffix.lower() == ".qta"
                           else file_path.name)
            check_path = f"{REMOTE_MEDIA_DIR}/{target_name}" if REMOTE_MEDIA_DIR else ""
            if is_already_processed(file_hash, check_path):
                logger.debug("既処理済み（スキップ）: %s", file_path.name)
            else:
                age_hours = (now - file_path.stat().st_mtime) / 3600
                if age_hours <= max_age_hours:
                    logger.info(
                        "未処理ファイル検出（自動処理します）: %s (%.1fh前)",
                        file_path.name, age_hours,
                    )
                    recent_unprocessed.append(file_path)
                else:
                    logger.debug(
                        "未処理ファイル（古いためスキップ）: %s (%.0fh前)",
                        file_path.name, age_hours,
                    )
        except Exception as e:
            logger.warning("既存ファイルスキャン中にエラー: %s - %s", file_path.name, e)

    logger.info("既存ファイルのスキャン完了（自動処理対象: %d件）", len(recent_unprocessed))

    for file_path in recent_unprocessed:
        logger.info("スキャン処理: %s", file_path.name)
        process_file(file_path)


RETRANSCRIBE_POLL_INTERVAL = 30   # 秒
SCP_RETRY_INTERVAL        = 300  # 秒（5分おき）
SCAN_TRIGGER_POLL_INTERVAL = 15  # 秒（手動スキャントリガーの確認間隔）
SCP_PENDING_FILE = LOG_DIR / "scp_pending.jsonl"


def process_retranscribe_job(conn, job: dict) -> None:
    """DB キューから取り出した再文字起こしジョブを処理する。"""
    import tempfile
    job_id = job["id"]
    recording_id = job["recording_id"]
    model = job["model"]
    clustering_threshold = job["clustering_threshold"]
    normalize = job["normalize"]

    def update_job(status=None, phase=None, progress=None, error=None):
        fields, values = [], []
        if status is not None:
            fields.append("status = %s"); values.append(status)
            if status == "processing":
                fields.append("started_at = NOW()")
            elif status in ("completed", "failed"):
                fields.append("completed_at = NOW()")
        if phase is not None:
            fields.append("phase = %s"); values.append(phase)
        if progress is not None:
            fields.append("progress = %s"); values.append(progress)
        if error is not None:
            fields.append("error = %s"); values.append(error)
        values.append(job_id)
        with conn.cursor() as cur:
            cur.execute(f"UPDATE retranscribe_jobs SET {', '.join(fields)} WHERE id = %s", values)
        conn.commit()

    try:
        # ファイルパスを取得
        with conn.cursor() as cur:
            cur.execute("SELECT file_path FROM recordings WHERE id = %s", (recording_id,))
            row = cur.fetchone()
        if not row:
            update_job(status="failed", error="Recording not found")
            return
        remote_file_path = row[0]

        update_job(status="processing", phase="転送中", progress=3)

        # N100から音声ファイルをMacへSCPでダウンロード
        file_name = Path(remote_file_path).name
        with tempfile.TemporaryDirectory() as tmpdir:
            local_tmp = Path(tmpdir) / file_name
            scp_result = subprocess.run(
                ["scp", f"{REMOTE_HOST}:{remote_file_path}", str(local_tmp)],
                capture_output=True, text=True,
            )
            if scp_result.returncode != 0:
                update_job(status="failed", error=f"SCP failed: {scp_result.stderr[:200]}")
                return

            update_job(phase="文字起こし中", progress=8)

            # kikitoru コマンド構築
            cmd = ["caffeinate", "-i", KIKITORU_CMD, "transcribe", "--use-db", str(local_tmp), "--enhance"]
            if normalize:
                cmd.append("--normalize")
            if model:
                cmd += ["--model", model]
            if clustering_threshold is not None:
                cmd += ["--clustering-threshold", str(clustering_threshold)]
            vocab = get_custom_vocabulary()
            if vocab:
                cmd += ["--initial-prompt", vocab]

            env = os.environ.copy()
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + env.get("PATH", "")
            env["KIKITORU_DB_URL"] = DATABASE_URL
            if os.environ.get("HF_TOKEN"):
                env["HF_TOKEN"] = os.environ["HF_TOKEN"]
            # DBには元のN100パスを保存する
            env["KIKITORU_FILE_PATH_OVERRIDE"] = remote_file_path

            phase_map = [
                ("[0/4]",        "音量均一化中",           5),
                ("[0.5/4]",      "音声強調中",             10),
                ("[1/4]",        "前処理中",               15),
                ("[2/4]",        "話者分離中",             25),
                ("[3/4]",        "文字起こし中",           40),
                ("[4/4]",        "話者ラベル割り当て中",   75),
                ("[DB] 声紋",    "声紋抽出中",             85),
                ("[DB] データ",  "DB保存中",               92),
                ("[DB] 保存完了","完了",                   100),
            ]

            logger.info("再文字起こし開始: recording_id=%d job_id=%s", recording_id, job_id)
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.rstrip()
                for key, phase, progress in phase_map:
                    if key in line:
                        update_job(phase=phase, progress=progress)
                        break
            proc.wait()

            if proc.returncode == 0:
                update_job(status="completed", phase="完了", progress=100)
                logger.info("再文字起こし完了: recording_id=%d", recording_id)
            else:
                update_job(status="failed", error=f"kikitoru returncode={proc.returncode}")
                logger.error("再文字起こし失敗: recording_id=%d returncode=%d", recording_id, proc.returncode)

    except Exception as e:
        logger.error("再文字起こしジョブエラー: %s", e)
        try:
            update_job(status="failed", error=str(e)[:200])
        except Exception:
            pass


def poll_retranscribe_jobs() -> None:
    """pending ジョブを DB から取り出して処理する（バックグラウンドスレッドから呼ばれる）。"""
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, recording_id, model, clustering_threshold, normalize
                    FROM retranscribe_jobs
                    WHERE status = 'pending'
                    ORDER BY created_at
                    LIMIT 1
                """)
                row = cur.fetchone()
            if row is None:
                return
            job = {
                "id": row[0], "recording_id": row[1],
                "model": row[2], "clustering_threshold": row[3], "normalize": row[4],
            }
            process_retranscribe_job(conn, job)
        finally:
            conn.close()
    except Exception as e:
        logger.error("ジョブポーリングエラー: %s", e)


def main() -> None:
    import threading

    if not WATCH_DIR.exists():
        logger.error("監視ディレクトリが存在しません: %s", WATCH_DIR)
        sys.exit(1)

    logger.info("kikiwatch 起動 [環境: %s]", _env.upper())
    logger.info("DB接続先: %s", DATABASE_URL.split("@")[-1] if DATABASE_URL else "(未設定)")
    logger.info("監視ディレクトリ: %s", WATCH_DIR)
    logger.info("kikitoru コマンド: %s", KIKITORU_CMD)
    logger.info("ログファイル: %s", LOG_FILE)

    # 既存ファイルスキャン
    scan_existing_files()

    # 再文字起こしジョブポーリングスレッド
    def _retranscribe_loop():
        while True:
            time.sleep(RETRANSCRIBE_POLL_INTERVAL)
            poll_retranscribe_jobs()

    retranscribe_thread = threading.Thread(target=_retranscribe_loop, daemon=True)
    retranscribe_thread.start()
    logger.info("再文字起こしジョブポーリング開始 (interval=%ds)", RETRANSCRIBE_POLL_INTERVAL)

    # SCP保留リトライスレッド
    def _scp_retry_loop():
        while True:
            time.sleep(SCP_RETRY_INTERVAL)
            poll_scp_pending()

    scp_retry_thread = threading.Thread(target=_scp_retry_loop, daemon=True)
    scp_retry_thread.start()
    logger.info("SCP保留リトライ開始 (interval=%ds)", SCP_RETRY_INTERVAL)

    # 手動スキャントリガー監視スレッド
    # meelog UI からのスキャン要求を DB 経由で受け取り、scan_existing_files() を実行する
    def _scan_trigger_loop():
        while True:
            time.sleep(SCAN_TRIGGER_POLL_INTERVAL)
            if not DATABASE_URL:
                continue
            try:
                conn = psycopg2.connect(DATABASE_URL)
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM settings WHERE key = 'scan_requested' AND user_id = 1"
                        )
                        row = cur.fetchone()
                    if row is None:
                        continue
                    # トリガーを消費してからスキャン実行
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM settings WHERE key = 'scan_requested' AND user_id = 1"
                        )
                    conn.commit()
                finally:
                    conn.close()
                logger.info("手動スキャントリガーを検知。過去 %.0fh 分のファイルをスキャンします", MANUAL_SCAN_FILE_HOURS)
                scan_existing_files(max_age_hours=MANUAL_SCAN_FILE_HOURS)
            except Exception as e:
                logger.error("スキャントリガー確認中にエラー: %s", e)

    scan_trigger_thread = threading.Thread(target=_scan_trigger_loop, daemon=True)
    scan_trigger_thread.start()
    logger.info("手動スキャントリガー監視開始 (interval=%ds)", SCAN_TRIGGER_POLL_INTERVAL)

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
