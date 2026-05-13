"""
fetch_price_targets.py
Finviz の analyst ratings ページから目標株価変更を取得し、
ウォッチリスト銘柄でフィルタリングして JSON に保存する。
"""

import csv
import json
import time
import random
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
WATCHLIST_CSV = Path(__file__).parent / "metadata_target_stocks_latest.csv"
OUTPUT_JSON = ROOT / "docs" / "data.json"

FINVIZ_URL = "https://finviz.com/analyst_ratings_all.ashx"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finviz.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

MAX_PAGES = 10   # 1ページ = 100件 → 最大1000件取得
SLEEP_MIN = 2.0
SLEEP_MAX = 4.0


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


def _find_ratings_table(soup: BeautifulSoup) -> object:
    """アナリスト評価テーブルを複数のセレクタで検索する"""
    candidates = [
        ("id", "analyst-ratings-full-table"),
        ("id", "analyst_ratings_table"),
        ("id", "analyst-ratings"),
        ("id", "ratings-table"),
    ]
    for attr, val in candidates:
        table = soup.find("table", {attr: val})
        if table:
            logger.info(f"Table found by {attr}={val}")
            return table

    # クラスベース検索
    for keyword in ("analyst", "rating"):
        table = soup.find("table", {"class": lambda c: c and keyword in c.lower()})
        if table:
            logger.info(f"Table found by class keyword '{keyword}'")
            return table

    # 列数 ≥ 7 の最初の大きなテーブルにフォールバック
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) > 5:
            first_data = rows[1].find_all("td") if len(rows) > 1 else []
            if len(first_data) >= 7:
                logger.info("Table found by column count heuristic")
                return t

    return None


def fetch_page(page: int, session: requests.Session) -> list[dict]:
    """Finviz analyst ratings から 1 ページ分を取得"""
    # r パラメータ（行オフセット）でページネーション: page 1→r=1, page 2→r=101, ...
    offset = (page - 1) * 100 + 1
    params = {"v": "2", "r": offset}
    try:
        resp = session.get(FINVIZ_URL, headers=HEADERS, params=params, timeout=30)
        logger.info(f"Page {page} (r={offset}): HTTP {resp.status_code}, {len(resp.text)} bytes")
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Page {page} fetch failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    table = _find_ratings_table(soup)

    if not table:
        logger.warning(
            f"Page {page}: ratings table not found. "
            f"Response snippet: {resp.text[:500]!r}"
        )
        return []

    rows = []
    for tr in table.find_all("tr")[1:]:   # ヘッダー行をスキップ
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        rows.append({
            "date":        tds[0].get_text(strip=True),
            "ticker":      tds[1].get_text(strip=True).upper(),
            "action":      tds[2].get_text(strip=True),   # Upgrade / Downgrade / Reiterated
            "analyst":     tds[3].get_text(strip=True),
            "rating_prev": tds[4].get_text(strip=True),
            "rating_new":  tds[5].get_text(strip=True),
            "pt_prev":     tds[6].get_text(strip=True),
            "pt_new":      tds[7].get_text(strip=True) if len(tds) > 7 else "",
        })

    logger.info(f"Page {page}: {len(rows)} rows parsed")
    return rows


def build_records(raw_rows: list[dict], watchlist: dict[str, dict]) -> list[dict]:
    """ウォッチリスト銘柄のみ抽出してメタデータをマージ"""
    records = []
    for row in raw_rows:
        sym = row["ticker"]
        if sym not in watchlist:
            continue
        meta = watchlist[sym]
        records.append({
            "date":        row["date"],
            "ticker":      sym,
            "company":     meta["company"],
            "sector":      meta["sector"],
            "industry":    meta["industry"],
            "action":      row["action"],
            "analyst":     row["analyst"],
            "rating_prev": row["rating_prev"],
            "rating_new":  row["rating_new"],
            "pt_prev":     row["pt_prev"],
            "pt_new":      row["pt_new"],
        })
    return records


def main():
    watchlist = load_watchlist(WATCHLIST_CSV)
    all_rows: list[dict] = []

    session = requests.Session()
    # Finviz のトップページを先に訪問してクッキーを取得
    try:
        session.get("https://finviz.com/", headers=HEADERS, timeout=20)
        logger.info("Visited Finviz homepage to obtain session cookie")
        time.sleep(random.uniform(1.0, 2.0))
    except requests.RequestException as e:
        logger.warning(f"Failed to visit homepage: {e}")

    for page in range(1, MAX_PAGES + 1):
        rows = fetch_page(page, session)
        if not rows:
            logger.info(f"No more data at page {page}, stopping.")
            break
        all_rows.extend(rows)
        time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

    records = build_records(all_rows, watchlist)
    logger.info(f"Matched records: {len(records)} / {len(all_rows)} total")

    # 既存データとマージ（重複排除）
    existing: list[dict] = []
    if OUTPUT_JSON.exists():
        with open(OUTPUT_JSON, encoding="utf-8") as f:
            payload = json.load(f)
            existing = payload.get("records", [])

    merged_map: dict[str, dict] = {}
    for r in existing + records:
        key = f"{r['date']}|{r['ticker']}|{r['analyst']}|{r['pt_new']}"
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
