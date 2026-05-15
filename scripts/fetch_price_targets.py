"""
fetch_price_targets.py
FMP (Financial Modeling Prep) の upgrades-downgrades-rss-feed および
price-target-rss-feed から最新のアナリスト格付け変更・目標株価変更を取得し、
ウォッチリスト銘柄でフィルタリングして JSON に保存する。

必要な環境変数:
    FMP_API_KEY: Financial Modeling Prep の API キー
                 取得: https://financialmodelingprep.com/
                 無料枠: 250 リクエスト/日
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
OUTPUT_JSON = ROOT / "docs" / "data.json"

FMP_BASE = "https://financialmodelingprep.com/api/v4"
FMP_RATINGS_ENDPOINT = f"{FMP_BASE}/upgrades-downgrades-rss-feed"
FMP_PT_ENDPOINT = f"{FMP_BASE}/price-target-rss-feed"

MAX_PAGES = 5     # 格付けフィード: 1ページ = 100件 → 最大500件取得
PT_MAX_PAGES = 5  # PTフィード: 1ページ = 100件 → 最大500件取得
SLEEP_SEC = 1.0   # FMP レート制限対策（無料枠: 250 req/日）


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


def fetch_page(api_key: str, page: int, endpoint: str) -> list[dict]:
    """FMP から 1 ページ分のデータを取得（page=0 始まり）"""
    params = {"page": page, "apikey": api_key}
    try:
        resp = requests.get(endpoint, params=params, timeout=20)
        logger.info(f"[{endpoint.split('/')[-1]}] Page {page}: HTTP {resp.status_code}, {len(resp.content)} bytes")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            logger.warning(f"Page {page}: unexpected response format: {str(data)[:200]}")
            return []
        logger.info(f"[{endpoint.split('/')[-1]}] Page {page}: {len(data)} records fetched")
        return data
    except requests.RequestException as e:
        logger.warning(f"Page {page} fetch failed: {e}")
        return []


def normalize_action(action: str) -> str:
    """FMP の action 文字列（小文字）を表示用に正規化"""
    a = (action or "").lower()
    if "upgrade" in a:
        return "Upgrade"
    if "downgrade" in a:
        return "Downgrade"
    if "initiat" in a:
        return "Initiated"
    if "reiterat" in a or "maintain" in a or "reaffirm" in a:
        return "Reiterated"
    return action.capitalize() if action else "Reiterated"


def normalize_date(date_str: str) -> str:
    """FMP の日付文字列 'YYYY-MM-DD HH:MM:SS' を 'YYYY-MM-DD' に正規化"""
    if not date_str:
        return ""
    return date_str[:10]


def build_pt_index(pt_rows: list[dict]) -> dict[str, dict]:
    """
    PTフィードから {date|ticker|analyst} → {pt_prev, pt_new} のインデックスを生成。
    同日・同銘柄・同アナリストの格付けレコードに目標株価をマージするために使用。
    """
    index = {}
    for row in pt_rows:
        date = normalize_date(row.get("publishedDate", ""))
        sym = (row.get("symbol") or "").strip().upper()
        analyst = row.get("analystCompany", "")
        key = f"{date}|{sym}|{analyst}"
        index[key] = {
            "pt_prev": str(row.get("previousPriceTarget", "") or ""),
            "pt_new":  str(row.get("priceTarget", "") or ""),
        }
    logger.info(f"PT index built: {len(index)} entries")
    return index


def build_records(
    raw_rows: list[dict],
    watchlist: dict[str, dict],
    pt_index: dict[str, dict],
) -> list[dict]:
    """
    格付けフィードからウォッチリスト銘柄のみ抽出してメタデータをマージ。
    同日・同銘柄・同アナリストのPTデータがあれば pt_prev/pt_new を付与。
    """
    records = []
    for row in raw_rows:
        sym = (row.get("symbol") or "").strip().upper()
        if sym not in watchlist:
            continue
        meta = watchlist[sym]
        date = normalize_date(row.get("publishedDate", ""))
        analyst = row.get("gradingCompany", "")
        pt_key = f"{date}|{sym}|{analyst}"
        pt_data = pt_index.get(pt_key, {"pt_prev": "", "pt_new": ""})
        records.append({
            "date":        date,
            "ticker":      sym,
            "company":     meta["company"],
            "sector":      meta["sector"],
            "industry":    meta["industry"],
            "action":      normalize_action(row.get("action", "")),
            "analyst":     analyst,
            "rating_prev": row.get("previousGrade", ""),
            "rating_new":  row.get("newGrade", ""),
            "pt_prev":     pt_data["pt_prev"],
            "pt_new":      pt_data["pt_new"],
        })
    return records


def build_pt_only_records(
    pt_rows: list[dict],
    watchlist: dict[str, dict],
    ratings_keys: set[str],
) -> list[dict]:
    """
    格付け変更なし・PT変更のみのレコードを生成（action = "PT Change"）。
    すでに格付けレコードとマージ済みのエントリは除外する。
    """
    records = []
    for row in pt_rows:
        sym = (row.get("symbol") or "").strip().upper()
        if sym not in watchlist:
            continue
        date = normalize_date(row.get("publishedDate", ""))
        analyst = row.get("analystCompany", "")
        pt_key = f"{date}|{sym}|{analyst}"
        if pt_key in ratings_keys:
            continue  # 格付けレコードにマージ済み
        meta = watchlist[sym]
        records.append({
            "date":        date,
            "ticker":      sym,
            "company":     meta["company"],
            "sector":      meta["sector"],
            "industry":    meta["industry"],
            "action":      "PT Change",
            "analyst":     analyst,
            "rating_prev": "",
            "rating_new":  "",
            "pt_prev":     str(row.get("previousPriceTarget", "") or ""),
            "pt_new":      str(row.get("priceTarget", "") or ""),
        })
    return records


def main():
    api_key = os.environ.get("FMP_API_KEY", "")
    if not api_key:
        logger.error("FMP_API_KEY environment variable is not set. Exiting.")
        raise SystemExit(1)

    watchlist = load_watchlist(WATCHLIST_CSV)

    # ── 格付けフィード取得 ──────────────────────────────────────────────
    ratings_rows: list[dict] = []
    for page in range(0, MAX_PAGES):
        rows = fetch_page(api_key, page, FMP_RATINGS_ENDPOINT)
        if not rows:
            logger.info(f"[ratings] No more data at page {page}, stopping.")
            break
        ratings_rows.extend(rows)
        time.sleep(SLEEP_SEC)

    # ── PTフィード取得 ─────────────────────────────────────────────────
    pt_rows: list[dict] = []
    for page in range(0, PT_MAX_PAGES):
        rows = fetch_page(api_key, page, FMP_PT_ENDPOINT)
        if not rows:
            logger.info(f"[pt] No more data at page {page}, stopping.")
            break
        pt_rows.extend(rows)
        time.sleep(SLEEP_SEC)

    # ── データ統合 ────────────────────────────────────────────────────
    pt_index = build_pt_index(pt_rows)

    # 格付けレコード（PT情報マージ済み）
    ratings_records = build_records(ratings_rows, watchlist, pt_index)
    logger.info(f"Ratings matched: {len(ratings_records)} / {len(ratings_rows)} total")

    # 格付けレコードで使用済みの PT インデックスキー
    ratings_keys = set()
    for row in ratings_rows:
        sym = (row.get("symbol") or "").strip().upper()
        if sym not in watchlist:
            continue
        date = normalize_date(row.get("publishedDate", ""))
        analyst = row.get("gradingCompany", "")
        ratings_keys.add(f"{date}|{sym}|{analyst}")

    # PT Changeのみのレコード
    pt_only_records = build_pt_only_records(pt_rows, watchlist, ratings_keys)
    logger.info(f"PT-only records: {len(pt_only_records)}")

    records = ratings_records + pt_only_records

    # ── 既存データとマージ（重複排除） ───────────────────────────────────
    existing: list[dict] = []
    if OUTPUT_JSON.exists():
        with open(OUTPUT_JSON, encoding="utf-8") as f:
            payload = json.load(f)
            existing = payload.get("records", [])

    merged_map: dict[str, dict] = {}
    for r in existing + records:
        key = f"{r['date']}|{r['ticker']}|{r['analyst']}|{r.get('action', '')}|{r['rating_new']}"
        merged_map[key] = r

    merged = sorted(merged_map.values(), key=lambda x: x["date"], reverse=True)

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(merged),
        "records": merged,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved {len(merged)} records → {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
