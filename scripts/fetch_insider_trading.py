"""
fetch_insider_trading.py
FMP (Financial Modeling Prep) の insider-trading-rss-feed エンドポイントから
インサイダー取引データを取得し、ウォッチリスト銘柄でフィルタリングして JSON に保存する。

対象取引種別:
    - P-Purchase  : 市場買い付け（純粋な購入）
    - S-Sale      : 市場売却（純粋な売り）
    - S-Sale+OE   : 市場売却（オプション行使に伴う売り）
    - M-Exempt    : ストックオプション行使

必要な環境変数:
    FMP_API_KEY: Financial Modeling Prep の API キー
                 取得: https://financialmodelingprep.com/
                 スタンダードプラン: 5,000,000 リクエスト/月
"""

import csv
import json
import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
WATCHLIST_CSV = Path(__file__).parent / "metadata_target_stocks_latest.csv"
OUTPUT_JSON = ROOT / "docs" / "insider.json"

FMP_BASE = "https://financialmodelingprep.com/api/v4"
FMP_INSIDER_ENDPOINT = f"{FMP_BASE}/insider-trading-rss-feed"

# 取得対象の取引種別（純粋売買 + オプション行使のみ）
TARGET_TYPES = {"P-Purchase", "S-Sale", "S-Sale+OE", "M-Exempt"}

MAX_PAGES = 10   # 1ページ = 100件 → 最大1000件取得
SLEEP_SEC = 0.5  # Standard プランはレート制限が緩いが念のため
MAX_RETRIES = 3  # 一時的なエラーに対するリトライ回数


def load_watchlist(csv_path: Path) -> dict[str, dict]:
    """ウォッチリスト CSV を {Symbol: {company, sector, industry}} に変換"""
    watchlist = {}
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sym = row["Symbol"].strip().upper()
            watchlist[sym] = {
                "company": row["Company Name"].strip(),
                "sector": row["Sector"].strip(),
                "industry": row["Industry"].strip(),
            }
    logger.info(f"Watchlist loaded: {len(watchlist)} symbols")
    return watchlist


def fetch_page(api_key: str, page: int) -> list[dict] | None:
    """
    FMP insider-trading-rss-feed から 1 ページ分のデータを取得（page=0 始まり）

    Returns:
        list[dict]: レコードのリスト（空リスト = このページにデータなし）
        None: API エラー発生時（ネットワークエラー・プラン制限・不正レスポンスなど）
    """
    params = {"page": page, "limit": 100, "apikey": api_key}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(FMP_INSIDER_ENDPOINT, params=params, timeout=30)
            logger.info(
                f"[insider] Page {page} (attempt {attempt}): "
                f"HTTP {resp.status_code}, {len(resp.content)} bytes"
            )
            if resp.status_code != 200:
                logger.error(
                    f"Page {page}: HTTP {resp.status_code} エラー。"
                    f"レスポンスボディ: {resp.text[:500]}"
                )
                if attempt < MAX_RETRIES:
                    wait = 2 ** attempt
                    logger.info(f"Page {page}: {wait}秒後にリトライ...")
                    time.sleep(wait)
                    continue
                return None
            data = resp.json()
            if not isinstance(data, list):
                logger.error(
                    f"Page {page}: 予期しないレスポンス形式（リストではありません）。\n"
                    f"レスポンス全文: {json.dumps(data, ensure_ascii=False)}\n"
                    "→ FMP_API_KEY の有効期限・プランの制限をご確認ください。"
                )
                return None  # API エラー（プラン制限等）
            logger.info(f"[insider] Page {page}: {len(data)} 件取得")
            return data
        except requests.RequestException as e:
            logger.warning(f"Page {page} (attempt {attempt}) ネットワークエラー: {e}")
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                logger.info(f"Page {page}: {wait}秒後にリトライ...")
                time.sleep(wait)
    return None


def normalize_date(date_str) -> str:
    """FMP の日付文字列 'YYYY-MM-DD HH:MM:SS' を 'YYYY-MM-DD' に正規化"""
    if not date_str:
        return ""
    return str(date_str)[:10]


def parse_role(type_of_owner: str) -> str:
    """
    FMP の typeOfOwner フィールドから役職ラベルを抽出する。
    例: "officer: Chief Executive Officer" → "Chief Executive Officer"
        "director"                          → "Director"
        "ten percent owner"                 → "Ten Percent Owner"
    """
    s = (type_of_owner or "").strip()
    if not s:
        return ""
    if ":" in s:
        return s.split(":", 1)[1].strip().title()
    return s.title()


def transaction_label(tx_type: str) -> str:
    """取引種別コードを表示ラベルに変換"""
    mapping = {
        "P-Purchase":  "Buy",
        "S-Sale":      "Sell",
        "S-Sale+OE":   "Sell",
        "M-Exempt":    "Option",
    }
    return mapping.get(tx_type, tx_type)


def safe_float(v) -> float | None:
    """安全に float 変換。None または変換不可なら None を返す"""
    try:
        f = float(v)
        return f if f == f else None  # NaN チェック
    except (TypeError, ValueError):
        return None


def build_records(raw_rows: list[dict], watchlist: dict[str, dict]) -> list[dict]:
    """
    インサイダー取引フィードから:
      - ウォッチリスト銘柄のみ
      - 対象取引種別（P-Purchase / S-Sale / S-Sale+OE / M-Exempt）のみ
    を抽出し、整形したレコードリストを返す。
    """
    records = []
    for row in raw_rows:
        sym = (row.get("symbol") or "").strip().upper()
        if sym not in watchlist:
            continue
        tx_type = (row.get("transactionType") or "").strip()
        if tx_type not in TARGET_TYPES:
            continue

        meta = watchlist[sym]
        shares = safe_float(row.get("securitiesTransacted"))
        price  = safe_float(row.get("price"))
        total_value = (
            round(shares * price, 2)
            if shares is not None and price is not None
            else None
        )

        records.append({
            "transactionDate":  normalize_date(row.get("transactionDate", "")),
            "filingDate":       normalize_date(row.get("filingDate", "")),
            "ticker":           sym,
            "company":          meta["company"],
            "sector":           meta["sector"],
            "industry":         meta["industry"],
            "insiderName":      (row.get("reportingName") or "").strip(),
            "role":             parse_role(row.get("typeOfOwner", "")),
            "transactionType":  tx_type,
            "transactionLabel": transaction_label(tx_type),
            "shares":           shares,
            "price":            price,
            "totalValue":       total_value,
            "sharesOwned":      safe_float(row.get("securitiesOwned")),
            "formType":         (row.get("formType") or "").strip(),
            "link":             (row.get("link") or "").strip(),
        })
    return records


def main():
    api_key = os.environ.get("FMP_API_KEY", "")
    if not api_key:
        logger.error("FMP_API_KEY 環境変数が設定されていません。終了します。")
        raise SystemExit(1)

    watchlist = load_watchlist(WATCHLIST_CSV)

    # ── RSS フィード取得 ────────────────────────────────────────────────
    raw_rows: list[dict] = []
    api_error_occurred = False
    for page in range(0, MAX_PAGES):
        rows = fetch_page(api_key, page)
        if rows is None:
            # API エラー（プラン制限・認証失敗・ネットワーク障害など）
            logger.error(
                f"[insider] page={page} で API エラーが発生しました。取得を中止します。\n"
                "FMP_API_KEY が有効か、プランが /api/v4/insider-trading-rss-feed を"
                "サポートしているかをご確認ください。"
            )
            api_error_occurred = True
            break
        if len(rows) == 0:
            logger.info(f"[insider] page={page} でデータなし。取得完了。")
            break
        raw_rows.extend(rows)
        time.sleep(SLEEP_SEC)

    logger.info(f"[insider] 合計取得: {len(raw_rows)} 件 (raw)")

    # ── フィルタリング & 整形 ──────────────────────────────────────────
    records = build_records(raw_rows, watchlist)
    logger.info(f"[insider] ウォッチリスト&種別マッチ: {len(records)} 件")

    # API エラーかつ取得0件の場合は既存データを保持したまま終了
    if api_error_occurred and len(raw_rows) == 0:
        logger.warning("[insider] API エラーのため既存データを変更せずに終了します。")
        return

    # ── 既存データとマージ（重複排除）──────────────────────────────────
    existing: list[dict] = []
    if OUTPUT_JSON.exists():
        with open(OUTPUT_JSON, encoding="utf-8") as f:
            payload = json.load(f)
            existing = payload.get("records", [])

    # 重複キー: 取引日 + ティッカー + インサイダー名 + 取引種別 + 株数
    merged_map: dict[str, dict] = {}
    for r in existing:
        key = (
            f"{r.get('transactionDate','')}|{r.get('ticker','')}|"
            f"{r.get('insiderName','')}|{r.get('transactionType','')}|"
            f"{r.get('shares','')}"
        )
        merged_map[key] = r
    for r in records:
        key = (
            f"{r.get('transactionDate','')}|{r.get('ticker','')}|"
            f"{r.get('insiderName','')}|{r.get('transactionType','')}|"
            f"{r.get('shares','')}"
        )
        merged_map[key] = r  # 新データで上書き

    merged = sorted(
        merged_map.values(),
        key=lambda x: (x.get("transactionDate", ""), x.get("filingDate", "")),
        reverse=True,
    )

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(merged),
        "records": merged,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(f"[insider] {len(merged)} 件を保存 → {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
