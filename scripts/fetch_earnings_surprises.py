"""
fetch_earnings_surprises.py
FMP (Financial Modeling Prep) の earning_calendar エンドポイントから
決算サプライズデータを取得し、ウォッチリスト銘柄でフィルタリングして JSON に保存する。

対象期間：過去 90 日 〜 翌 7 日

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
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
WATCHLIST_CSV = Path(__file__).parent / "metadata_target_stocks_latest.csv"
OUTPUT_JSON = ROOT / "docs" / "earnings.json"

FMP_BASE = "https://financialmodelingprep.com/api/v3"
FMP_CALENDAR_ENDPOINT = f"{FMP_BASE}/earning_calendar"

LOOKBACK_DAYS = 90   # 過去 90 日分を取得（直近の決算サプライズ）
LOOKAHEAD_DAYS = 7   # 翌 7 日分（予定）を取得
SLEEP_SEC = 0.5      # Standard プランはレート制限が緩いが念のため


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


def calc_surprise_pct(actual, estimated) -> str:
    """
    サプライズ率（%）を計算。
    - estimated が 0 の場合は計算不可なので空文字を返す
    - actual が None の場合（未発表）も空文字を返す
    """
    try:
        a = float(actual)
        e = float(estimated)
        if e == 0:
            return ""
        pct = (a - e) / abs(e) * 100
        return f"{pct:.2f}"
    except (TypeError, ValueError):
        return ""


def fmt_number(v) -> str:
    """None や空文字の場合は空文字、それ以外は文字列化"""
    if v is None:
        return ""
    return str(v)


def fetch_calendar(api_key: str, from_date: str, to_date: str) -> list[dict]:
    """FMP earning_calendar から指定期間の決算データを取得"""
    params = {
        "from": from_date,
        "to": to_date,
        "apikey": api_key,
    }
    try:
        resp = requests.get(FMP_CALENDAR_ENDPOINT, params=params, timeout=30)
        logger.info(f"earning_calendar: HTTP {resp.status_code}, {len(resp.content)} bytes")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            logger.warning(f"Unexpected response format: {str(data)[:300]}")
            return []
        logger.info(f"Fetched {len(data)} total records from earning_calendar")
        return data
    except requests.RequestException as e:
        logger.warning(f"earning_calendar fetch failed: {e}")
        return []


def build_records(rows: list[dict], watchlist: dict[str, dict]) -> list[dict]:
    """ウォッチリスト銘柄のみ抽出してサプライズ率を計算"""
    records = []
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        if sym not in watchlist:
            continue
        meta = watchlist[sym]

        eps_actual    = row.get("eps")           # None if not yet reported
        eps_estimated = row.get("epsEstimated")
        rev_actual    = row.get("revenue")
        rev_estimated = row.get("revenueEstimated")

        # eps が None → 未発表、0 以外の数値 or 0 → 発表済み
        is_reported = eps_actual is not None

        eps_surp_pct = calc_surprise_pct(eps_actual, eps_estimated) if is_reported else ""
        rev_surp_pct = calc_surprise_pct(rev_actual, rev_estimated) if is_reported else ""

        records.append({
            "date":              row.get("date", ""),
            "symbol":            sym,
            "company":           meta["company"],
            "sector":            meta["sector"],
            "industry":          meta["industry"],
            "fiscalDateEnding":  row.get("fiscalDateEnding", "") or "",
            "time":              row.get("time", "") or "",
            "isReported":        is_reported,
            "epsActual":         fmt_number(eps_actual),
            "epsEstimated":      fmt_number(eps_estimated),
            "epsSurprisePct":    eps_surp_pct,
            "revenueActual":     fmt_number(rev_actual),
            "revenueEstimated":  fmt_number(rev_estimated),
            "revenueSurprisePct": rev_surp_pct,
        })
    return records


def main():
    api_key = os.environ.get("FMP_API_KEY", "")
    if not api_key:
        logger.error("FMP_API_KEY environment variable is not set. Exiting.")
        raise SystemExit(1)

    watchlist = load_watchlist(WATCHLIST_CSV)

    today = datetime.now(timezone.utc).date()
    from_date = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    to_date   = (today + timedelta(days=LOOKAHEAD_DAYS)).isoformat()

    logger.info(f"Fetching earnings calendar: {from_date} → {to_date}")
    rows = fetch_calendar(api_key, from_date, to_date)
    time.sleep(SLEEP_SEC)

    if not rows:
        logger.warning("No data fetched. Keeping existing data if any.")
        existing_payload: dict = {}
        if OUTPUT_JSON.exists():
            with open(OUTPUT_JSON, encoding="utf-8") as f:
                existing_payload = json.load(f)
        existing_payload["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(existing_payload, f, ensure_ascii=False, indent=2)
        return

    records = build_records(rows, watchlist)
    logger.info(f"Watchlist matches: {len(records)} / {len(rows)} total calendar entries")

    # 発表済みは日付降順、未発表は日付昇順でソート
    reported = sorted(
        [r for r in records if r["isReported"]],
        key=lambda x: (x["date"], x["symbol"]),
        reverse=True,
    )
    upcoming = sorted(
        [r for r in records if not r["isReported"]],
        key=lambda x: (x["date"], x["symbol"]),
    )
    records = reported + upcoming

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(records),
        "from_date": from_date,
        "to_date": to_date,
        "records": records,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved {len(records)} records → {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
