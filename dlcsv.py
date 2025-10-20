# -*- coding: utf-8 -*-
# 指定URLからCSVを取得 → 元ファイル名で一旦保存 → 指定の新ファイル名へリネーム
# 5本そろったら ~/Downloads に funds-YYYYMMDD.zip を作成（YYYYMMDDはJSTの実行日）
# - 失敗時はリトライ（指数バックオフ）
# - Content-Disposition/URLから元ファイル名を推定
# - ZIP作成後は5つのCSVを削除
# - ファイルのエンコーディング変換は行わない
from __future__ import annotations

import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

JST = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True)
class DownloadItem:
    url: str
    expected_original_pattern: str | None  # 例: r"^253266\.csv$", Noneならチェックしない
    rename_to: str                          # 例: "eMAXIS-SP500.csv"


# ---- 設定 ----
OUTPUT_DIR = os.path.abspath("./")                         # 一時保存先（CSV格納）
ZIP_BASENAME_PREFIX = "funds"                              # "funds-YYYYMMDD.zip"
DOWNLOADS_DIR = os.path.expanduser("~/Downloads")          # ZIPの出力先
TIMEOUT = (10, 30)                                         # (connect, read) seconds

# リトライ設定（429/5xxや一時的なネットワークエラーを吸収）
RETRY = Retry(
    total=5,
    connect=5,
    read=5,
    backoff_factor=0.8,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
    raise_on_status=False,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FundsDownloader/1.0)",
    "Accept": "text/csv,application/octet-stream,application/json,text/plain,*/*",
}

# 取得対象
ITEMS: list[DownloadItem] = [
    DownloadItem(
        url="https://www.am.mufg.jp/fund_file/setteirai/253266.csv",
        expected_original_pattern=r"^253266\.csv$",
        rename_to="eMAXIS-SP500.csv",
    ),
    DownloadItem(
        url="https://www.kddi-am.com/wp-content/themes/aufunds/csv/fund_nav_4001.csv",
        expected_original_pattern=r"^fund_nav_4001\.csv$",
        rename_to="auレバナス.csv",
    ),
    DownloadItem(
        url="https://www.amova-am.com/api/fund-export?funds[]=645133",
        # サーバ側の生成名: fund_info_645133_YYYYMMDDHHMM.csv
        # 厳密チェックは不要との要望 → None（取得後は常に rename_to へ）
        expected_original_pattern=None,
        rename_to="ゴルナス.csv",
    ),
    DownloadItem(
        url="https://www.nam.co.jp/fundinfo/data/csv.php?fund_code=122308",
        expected_original_pattern=r"^122308\.csv$",
        rename_to="ニッセイNASDAQ100.csv",
    ),
    DownloadItem(
        url="https://apl.wealthadvisor.jp/webasp/sbi_am/pc/download.aspx?type=1&fnc=202306080A",
        expected_original_pattern=r"^(基準価額データ\.csv|kijyun.*\.csv)$",
        rename_to="サクッと純金.csv",
    ),
]


# ---- ユーティリティ ----
def build_session() -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=RETRY, pool_maxsize=10)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(HEADERS)
    return s


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def guess_filename_from_cd(cd: str | None) -> str | None:
    """Content-Dispositionヘッダからファイル名を推定"""
    if not cd:
        return None
    # RFC 5987/6266 (filename*), もしくは filename=
    m = re.search(r"filename\*\s*=\s*([^'\"]+)''([^;]+)", cd, flags=re.IGNORECASE)
    if m:
        from urllib.parse import unquote
        try:
            return unquote(m.group(2).strip())
        except Exception:
            pass

    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()

    m = re.search(r'filename\s*=\s*([^;]+)', cd, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip().strip('"')
    return None


def guess_filename_from_url(url: str) -> str | None:
    """URLパスの末尾から推定（クエリは無視）"""
    from urllib.parse import urlparse
    p = urlparse(url)
    base = os.path.basename(p.path)
    return base or None


def sanitize_filename(name: str) -> str:
    """OS非対応の文字を最小限クリーニング"""
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip()


def save_response_to_file(resp: requests.Response, filepath: str) -> None:
    CHUNK = 1024 * 256
    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(chunk_size=CHUNK):
            if chunk:
                f.write(chunk)


def fetch_one(session: requests.Session, item: DownloadItem, workdir: str) -> tuple[str, str]:
    """1ファイルをダウンロードして一時保存 → 軽い整合チェック → リネーム"""
    # 1) リクエスト
    resp = session.get(item.url, timeout=TIMEOUT, allow_redirects=True)
    resp.raise_for_status()

    # 2) 元ファイル名の推定
    cd = resp.headers.get("Content-Disposition")
    filename = guess_filename_from_cd(cd) or guess_filename_from_url(item.url) or "downloaded.csv"

    # 3) 拡張子が無ければCSVを付与（通常は不要だが保険）
    if not os.path.splitext(filename)[1]:
        filename = filename + ".csv"

    filename = sanitize_filename(filename)
    tmp_orig_path = os.path.join(workdir, filename)

    # 4) 保存
    save_response_to_file(resp, tmp_orig_path)

    # 5) 元名パターンの軽い整合性チェック（指定がある場合のみ、警告ログ）
    if item.expected_original_pattern:
        base = os.path.basename(tmp_orig_path)
        if not re.match(item.expected_original_pattern, base, flags=re.IGNORECASE):
            print(f"[WARN] '{item.url}' の元名推定 '{base}' が想定パターン '{item.expected_original_pattern}' に合致しませんでした。", file=sys.stderr)

    # 6) リネーム（最終ファイル）
    final_name = sanitize_filename(item.rename_to)
    final_path = os.path.join(workdir, final_name)

    # 既存があれば上書き
    if os.path.exists(final_path):
        os.remove(final_path)
    os.replace(tmp_orig_path, final_path)

    return (os.path.join(workdir, filename), final_path)


def make_zip(file_paths: list[str], zip_path: str) -> None:
    """指定のファイルだけをZIPへ格納（UTF-8名対応）"""
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for full in sorted(file_paths):
            entry = os.path.basename(full)
            zf.write(full, arcname=entry)


def main() -> int:
    ensure_dir(OUTPUT_DIR)
    ensure_dir(DOWNLOADS_DIR)

    session = build_session()

    print(f"[INFO] ダウンロード開始: {len(ITEMS)} 件（保存先: {OUTPUT_DIR}）")
    renamed_paths: list[str] = []

    for i, item in enumerate(ITEMS, 1):
        try:
            _, final_path = fetch_one(session, item, OUTPUT_DIR)
            renamed_paths.append(final_path)
            print(f"[OK] ({i}/{len(ITEMS)}) {item.url}")
            time.sleep(0.5)  # 過剰アクセス回避
        except Exception as e:
            print(f"[ERROR] ({i}/{len(ITEMS)}) {item.url}: {e}", file=sys.stderr)
            return 2

    # 5つ揃っているか確認
    if len(renamed_paths) != 5:
        print(f"[ERROR] 5件のCSVが揃っていません（{len(renamed_paths)} 件）。", file=sys.stderr)
        return 3

    # ZIP名（JSTの実行日）
    today_jst = datetime.now(JST).strftime("%Y%m%d")
    zip_name = f"{ZIP_BASENAME_PREFIX}-{today_jst}.zip"
    zip_path = os.path.join(DOWNLOADS_DIR, zip_name)

    # 既存があれば上書き
    if os.path.exists(zip_path):
        os.remove(zip_path)

    # 指定の5ファイルのみZIPに含める
    make_zip(renamed_paths, zip_path)

    print("[INFO] ダウンロード＆リネーム完了（ZIPへ格納済）:")
    for p in renamed_paths:
        print("  -", p)
    print(f"[OK] ZIP作成: {zip_path}")

    # ZIP作成後に5つのCSVを削除
    delete_errors = []
    for p in renamed_paths:
        try:
            os.remove(p)
        except Exception as e:
            delete_errors.append((p, str(e)))

    if delete_errors:
        print("[WARN] 一部CSVの削除に失敗しました:", file=sys.stderr)
        for p, msg in delete_errors:
            print(f"  - {p}: {msg}", file=sys.stderr)
    else:
        print("[INFO] 5つのCSVを削除しました。")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[WARN] 中断されました。", file=sys.stderr)
        sys.exit(130)
