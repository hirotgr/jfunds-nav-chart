#!/usr/bin/env zsh
set -e
set -u
set -o pipefail

# --- 設定 ---
# 一時保存ディレクトリ（CSV格納先）
WORKDIR="./"
# ZIP出力先
ZIPDIR="$HOME/Downloads"
# ZIPファイル名（例: funds-20251020.zip）
DATE_JST=$(TZ=Asia/Tokyo date +%Y%m%d)
ZIPNAME="funds-${DATE_JST}.zip"

# URLと保存名の対応（| 区切り）
TOTAL=5

# ループ末尾のヒアドキュメントへ直接埋め込む

# --- 初期化 ---
mkdir -p "$WORKDIR"
mkdir -p "$ZIPDIR"

# --- ダウンロード処理 ---
echo "[INFO] ダウンロード開始 (${TOTAL} 件)"
i=0

# ヒアドキュメントから1行ずつ取得（サブシェルを避けるためパイプは使わない）
while IFS='|' read -r url target_name; do
  # 空行はスキップ
  [ -z "$url" ] && continue
  i=$((i+1))
  tmpfile=$(mktemp "${WORKDIR}/tmpXXXXXX.csv")

  echo "[INFO] ($i/${TOTAL}) ${url}"

  # curl でダウンロード (最大3回リトライ)
  if ! curl -fsSL --retry 3 --retry-delay 2 -A "Mozilla/5.0 (FundsDownloader/1.0)" -o "$tmpfile" "$url"; then
    echo "[ERROR] ${url} のダウンロードに失敗しました" >&2
    exit 1
  fi

  # 取得後に所定名へ移動
  mv "$tmpfile" "${WORKDIR}/${target_name}"

  sleep 0.5
done <<'URLS'
https://www.am.mufg.jp/fund_file/setteirai/253266.csv|eMAXIS-SP500.csv
https://www.kddi-am.com/wp-content/themes/aufunds/csv/fund_nav_4001.csv|auレバナス.csv
https://www.amova-am.com/api/fund-export?funds[]=645133|ゴルナス.csv
https://www.nam.co.jp/fundinfo/data/csv.php?fund_code=122308|ニッセイNASDAQ100.csv
https://apl.wealthadvisor.jp/webasp/sbi_am/pc/download.aspx?type=1&fnc=202306080A|サクッと純金.csv
URLS

# --- ZIP作成 ---
ZIPPATH="${ZIPDIR}/${ZIPNAME}"
echo "[INFO] ZIP作成中: ${ZIPPATH}"
cd "$WORKDIR"
setopt null_glob
csvs=( *.csv )
if (( ${#csvs[@]} == 0 )); then
  echo "[ERROR] CSV が 0 件です。ダウンロードに失敗している可能性があります。" >&2
  exit 2
fi
zip -9 -q "$ZIPPATH" "${csvs[@]}"

# --- CSV削除 ---
echo "[INFO] ZIP作成後にCSVを削除します"
rm -f -- "${csvs[@]}"

echo "[OK] 完了しました → ${ZIPPATH}"
