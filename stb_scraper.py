"""
Starbucks Japan Store Price Scraper - HTMLスクレイピング版
===========================================================
使い方:
  python3 stb_scraper.py              # フルモード
  python3 stb_scraper.py --mode=full
  python3 stb_scraper.py --mode=retry # 失敗店舗リトライのみ

cron設定例:
  0 14 * * * cd /home/pi/stb && /usr/bin/python3 stb_scraper.py --mode=full >> logs/scraper.log 2>&1
  0 17 * * * cd /home/pi/stb && /usr/bin/python3 stb_scraper.py --mode=retry >> logs/retry.log 2>&1

データ取得方法:
  Step 1: https://store.starbucks.co.jp/pref/{pref_slug}/ をパースして店舗IDリストを取得
  Step 2: https://store.starbucks.co.jp/detail-{store_id}/ をパースして
          座標・営業時間・価格ランク・ティバーナ・ドライブスルーを取得

価格テーブル（スタバラテ Tall, 2025年調査値）:
  通常: テイクアウト¥491 / 店内¥500
  A:    テイクアウト¥510 / 店内¥520
  B:    テイクアウト¥501 / 店内¥510
"""

import argparse
import json
import logging
import os
import random
import re
import shutil
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ============================================================
# ★ 運用設定
# ============================================================
BATCH_COUNT = 3  # 1日に処理する県数（HTML取得は重いので3が安全）

# ============================================================
# パス設定
# ============================================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR  = BASE_DIR / "logs"
STATE_FILE  = BASE_DIR / "last_run.json"
FAILED_FILE = BASE_DIR / "failed_stores.json"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"scraper_{datetime.now():%Y%m%d}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ============================================================
# Discord Webhook（.envから読み込み）
# ============================================================
def _load_env():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

_load_env()
WEBHOOK_DIFF  = os.environ.get("DISCORD_DIFF", "")
WEBHOOK_ERROR = os.environ.get("DISCORD_ERROR", "")
WEBHOOK_LOG   = os.environ.get("DISCORD_LOG", "")

def _discord_post(url: str, content: str):
    if not url:
        return
    try:
        content = content[:1990] + "…" if len(content) > 1990 else content
        requests.post(url, json={"content": content}, timeout=10)
    except Exception:
        log.warning("Discord通知失敗（処理は継続します）")

def notify_log(msg: str):
    _discord_post(WEBHOOK_LOG, msg)

def notify_error(context: str, exc: Exception = None):
    lines = [f"🚨 **[スタバ] エラー発生** — {context}"]
    if exc:
        lines.append(f"```{traceback.format_exc()[-1200:]}```")
    _discord_post(WEBHOOK_ERROR, "\n".join(lines))

def notify_diff(diffs: list):
    if not diffs or not WEBHOOK_DIFF:
        return
    lines = ["📢 **[スタバ] 差分検知レポート**"]
    for d in diffs[:20]:
        if d["type"] == "new_open":
            lines.append(f"🆕 新店: **{d['name']}** ({d.get('address','')})")
        elif d["type"] == "closed":
            lines.append(f"🚫 閉店: **{d['name']}**")
        elif d["type"] == "price_change":
            lines.append(f"💰 価格変動: **{d['name']}** {d['old_rank']} → {d['new_rank']}")
        elif d["type"] == "facility_change":
            lines.append(f"🏪 設備変更: **{d['name']}** {d['item']} {'追加✅' if d['new'] else '廃止❌'}")
    if len(diffs) > 20:
        lines.append(f"…他 {len(diffs)-20} 件")
    _discord_post(WEBHOOK_DIFF, "\n".join(lines))

# ============================================================
# 価格テーブル
# ============================================================
PRICE_TABLE = {
    "A":      {"takeout": 510, "in_store": 520},
    "B":      {"takeout": 501, "in_store": 510},
    "normal": {"takeout": 491, "in_store": 500},
}

STORE_TYPE_LABEL = {
    "1":  None,
    "2":  "特殊施設内店舗",
    "3":  None,
    "11": "ROASTERY",
}

# ============================================================
# 都道府県設定（JIS X 0401準拠 + スタバURLスラッグ）
# ============================================================
PREFECTURE_CONFIG = {
    "01": {"name": "北海道",   "slug": "hokkaido"},
    "02": {"name": "青森県",   "slug": "aomori"},
    "03": {"name": "岩手県",   "slug": "iwate"},
    "04": {"name": "宮城県",   "slug": "miyagi"},
    "05": {"name": "秋田県",   "slug": "akita"},
    "06": {"name": "山形県",   "slug": "yamagata"},
    "07": {"name": "福島県",   "slug": "fukushima"},
    "08": {"name": "茨城県",   "slug": "ibaraki"},
    "09": {"name": "栃木県",   "slug": "tochigi"},
    "10": {"name": "群馬県",   "slug": "gunma"},
    "11": {"name": "埼玉県",   "slug": "saitama"},
    "12": {"name": "千葉県",   "slug": "chiba"},
    "13": {"name": "東京都",   "slug": "tokyo"},
    "14": {"name": "神奈川県", "slug": "kanagawa"},
    "15": {"name": "新潟県",   "slug": "niigata"},
    "16": {"name": "富山県",   "slug": "toyama"},
    "17": {"name": "石川県",   "slug": "ishikawa"},
    "18": {"name": "福井県",   "slug": "fukui"},
    "19": {"name": "山梨県",   "slug": "yamanashi"},
    "20": {"name": "長野県",   "slug": "nagano"},
    "21": {"name": "岐阜県",   "slug": "gifu"},
    "22": {"name": "静岡県",   "slug": "shizuoka"},
    "23": {"name": "愛知県",   "slug": "aichi"},
    "24": {"name": "三重県",   "slug": "mie"},
    "25": {"name": "滋賀県",   "slug": "shiga"},
    "26": {"name": "京都府",   "slug": "kyoto"},
    "27": {"name": "大阪府",   "slug": "osaka"},
    "28": {"name": "兵庫県",   "slug": "hyogo"},
    "29": {"name": "奈良県",   "slug": "nara"},
    "30": {"name": "和歌山県", "slug": "wakayama"},
    "31": {"name": "鳥取県",   "slug": "tottori"},
    "32": {"name": "島根県",   "slug": "shimane"},
    "33": {"name": "岡山県",   "slug": "okayama"},
    "34": {"name": "広島県",   "slug": "hiroshima"},
    "35": {"name": "山口県",   "slug": "yamaguchi"},
    "36": {"name": "徳島県",   "slug": "tokushima"},
    "37": {"name": "香川県",   "slug": "kagawa"},
    "38": {"name": "愛媛県",   "slug": "ehime"},
    "39": {"name": "高知県",   "slug": "kochi"},
    "40": {"name": "福岡県",   "slug": "fukuoka"},
    "41": {"name": "佐賀県",   "slug": "saga"},
    "42": {"name": "長崎県",   "slug": "nagasaki"},
    "43": {"name": "熊本県",   "slug": "kumamoto"},
    "44": {"name": "大分県",   "slug": "oita"},
    "45": {"name": "宮崎県",   "slug": "miyazaki"},
    "46": {"name": "鹿児島県", "slug": "kagoshima"},
    "47": {"name": "沖縄県",   "slug": "okinawa"},
}

# ============================================================
# HTTPヘッダー
# ============================================================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://store.starbucks.co.jp/",
}

MAX_RETRY = 3
DETAIL_BASE = "https://store.starbucks.co.jp/detail-{store_id}/"
PREF_BASE   = "https://store.starbucks.co.jp/pref/{slug}/"

# ============================================================
# 失敗理由
# ============================================================
class FailureReason:
    NOT_FOUND  = "not_found"
    TEMP_ERROR = "temp_error"
    PARSE_ERR  = "parse_error"

# ============================================================
# 失敗店舗管理
# ============================================================
def load_failed() -> dict:
    if FAILED_FILE.exists():
        return json.loads(FAILED_FILE.read_text())
    return {}

def save_failed(failed: dict):
    FAILED_FILE.write_text(json.dumps(failed, ensure_ascii=False, indent=2))

def record_failure(failed: dict, store_id: str, name: str, reason: str):
    entry = failed.get(store_id, {"reason": reason, "count": 0, "name": name})
    entry["count"] += 1
    entry["reason"] = reason
    entry["last_failed"] = datetime.now().isoformat()
    failed[store_id] = entry
    log.warning(f"    失敗記録: {name} ({store_id}) - {reason} (累計{entry['count']}回)")

def should_skip(failed: dict, store_id: str) -> bool:
    entry = failed.get(store_id)
    if not entry:
        return False
    if entry["reason"] in (FailureReason.NOT_FOUND, FailureReason.PARSE_ERR):
        return True
    if entry["reason"] == FailureReason.TEMP_ERROR and entry["count"] >= MAX_RETRY:
        return True
    return False

# ============================================================
# 状態管理
# ============================================================
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"completed": [], "last_run_date": None}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def get_next_prefectures(state: dict) -> list[str]:
    all_codes = sorted(PREFECTURE_CONFIG.keys())
    completed = set(state.get("completed", []))
    pending = [c for c in all_codes if c not in completed]
    if not pending:
        log.info("全都道府県完了。次のサイクル開始。")
        state["completed"] = []
        save_state(state)
        pending = all_codes
    return pending[:BATCH_COUNT]

# ============================================================
# HTTPリクエスト（指数バックオフ付き）
# ============================================================
def get_html(url: str) -> tuple[str | None, str | None]:
    """
    HTMLを取得。戻り値: (html_text, failure_reason)
    成功: (text, None)
    404:  (None, FailureReason.NOT_FOUND)
    その他失敗: (None, FailureReason.TEMP_ERROR)
    """
    wait = 15
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                return resp.text, None
            if resp.status_code == 404:
                log.warning(f"    404: {url}")
                return None, FailureReason.NOT_FOUND
            if resp.status_code == 429:
                log.warning(f"    429 Too Many Requests (試行{attempt}/{MAX_RETRY}) {wait}秒待機...")
                time.sleep(wait)
                wait = min(wait * 2, 180)
                continue
            log.warning(f"    HTTP {resp.status_code} (試行{attempt}/{MAX_RETRY})")
        except requests.Timeout:
            log.warning(f"    タイムアウト (試行{attempt}/{MAX_RETRY})")
        except requests.RequestException as e:
            log.warning(f"    接続エラー: {e} (試行{attempt}/{MAX_RETRY})")

        if attempt < MAX_RETRY:
            time.sleep(random.uniform(15, 30))

    return None, FailureReason.TEMP_ERROR

# ============================================================
# Step 1: 都道府県ページから店舗IDリストを取得
# ============================================================
def fetch_store_ids_in_pref(pref_code: str) -> list[dict]:
    """
    都道府県一覧ページをパースして店舗IDと店舗名のリストを返す。
    戻り値: [{"store_id": "1234", "name": "青森中央店", "address": "..."}, ...]
    """
    slug = PREFECTURE_CONFIG[pref_code]["slug"]
    url  = PREF_BASE.format(slug=slug)
    html, reason = get_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    stores = []

    # 店舗リンクから store_id を抽出
    # パターン: href="/detail-1234/" または href="https://store.starbucks.co.jp/detail-1234/"
    for a in soup.find_all("a", href=True):
        m = re.search(r'/detail-(\d+)/', a["href"])
        if not m:
            continue
        store_id = m.group(1)
        if any(s["store_id"] == store_id for s in stores):
            continue  # 重複除去

        # 店舗名を取得（aタグ内のテキストまたは親要素から）
        name = a.get_text(strip=True)
        if not name:
            parent = a.find_parent(class_=re.compile(r'store'))
            name = parent.get_text(strip=True)[:30] if parent else f"store_{store_id}"

        stores.append({"store_id": store_id, "name": name})

    log.info(f"  店舗ID取得: {len(stores)}件")
    return stores

# ============================================================
# Step 2: 詳細ページから全情報を取得
# ============================================================
def fetch_store_detail(store_id: str) -> dict:
    """
    詳細ページをパースして店舗情報を返す。
    戻り値: {
        "name": str,
        "address": str,
        "coords": [lat, lng] | None,
        "hours": {...},
        "price_rank": "normal"|"A"|"B",
        "is_teavana": bool,
        "drive_thru": bool,
        "reserve": bool,
        "store_type": str | None,
        "fetch_ok": bool,
        "failure_reason": str | None,
    }
    """
    url = DETAIL_BASE.format(store_id=store_id)
    html, reason = get_html(url)

    if not html:
        return {"fetch_ok": False, "failure_reason": reason}

    try:
        soup = BeautifulSoup(html, "html.parser")

        # 店舗名
        name_el = soup.find("div", class_="store-detail__title-text")
        name = name_el.get_text(strip=True) if name_el else ""

        # 住所
        addr_el = soup.find("div", class_=re.compile(r"text-detail.*line-height"))
        address = addr_el.get_text(strip=True) if addr_el else ""

        # 座標（gmapのiframeのsrcから抽出）
        coords = None
        gmap = soup.find("div", id="gmap")
        if gmap:
            # gmapの近くのscriptタグかdata属性から座標を探す
            # location_jpフィールドを探す（ページのscriptタグ内）
            for script in soup.find_all("script"):
                text = script.string or ""
                m = re.search(r'location_jp["\s:]+([0-9.]+)[,\s]+([0-9.]+)', text)
                if m:
                    try:
                        coords = [float(m.group(1)), float(m.group(2))]
                        break
                    except ValueError:
                        pass

        # 座標が取れなかった場合はJSON-LDから試みる
        if not coords:
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string or "{}")
                    geo = data.get("geo") or {}
                    if geo.get("latitude") and geo.get("longitude"):
                        coords = [float(geo["latitude"]), float(geo["longitude"])]
                        break
                except (json.JSONDecodeError, TypeError):
                    pass

        # 価格ランク
        price_rank = "normal"
        for notice in soup.find_all("div", class_="notice-text"):
            text = notice.get_text()
            m = re.search(r"特定立地価格\s*([A-Z])", text)
            if m:
                price_rank = m.group(1)
                break

        # ティバーナ判定
        is_teavana = bool(soup.find("a", class_="tea-logo"))

        # リザーブ判定（ページタイトルまたはロゴから）
        is_reserve = bool(soup.find("img", alt=re.compile(r"reserve", re.I))) or \
                     "リザーブ" in (name or "")

        # ドライブスルー判定（サービスアイコン一覧から）
        drive_thru = False
        for el in soup.find_all(class_=re.compile(r"service|facility|icon")):
            if "ドライブ" in el.get_text() or "drive" in el.get_text(strip=True).lower():
                drive_thru = True
                break

        # 営業時間（曜日別）
        hours = {}
        days_map = {
            "月": "mon", "火": "tue", "水": "wed", "木": "thu",
            "金": "fri", "土": "sat", "日": "sun", "祝": "hol",
        }
        # 営業時間テキストを探す（"07:00～22:00"形式）
        for row in soup.find_all(class_=re.compile(r"content--row|hours|time")):
            text = row.get_text()
            for ja_day, en_day in days_map.items():
                if ja_day in text:
                    m = re.search(r"(\d{2}:\d{2})[～〜\-](\d{2}:\d{2})", text)
                    if m and en_day not in hours:
                        hours[en_day] = {"open": m.group(1), "close": m.group(2)}

        return {
            "name":           name,
            "address":        address,
            "coords":         coords,
            "hours":          hours,
            "price_rank":     price_rank,
            "is_teavana":     is_teavana,
            "reserve":        is_reserve,
            "drive_thru":     drive_thru,
            "store_type":     None,  # HTMLからは判定不可、将来対応
            "fetch_ok":       True,
            "failure_reason": None,
        }

    except Exception as e:
        log.warning(f"    HTMLパース失敗: {store_id} - {e}")
        return {"fetch_ok": False, "failure_reason": FailureReason.PARSE_ERR}

# ============================================================
# データ変換
# ============================================================
def build_shop_record(store_id: str, pref_code: str, detail: dict) -> dict:
    rank   = detail.get("price_rank", "normal") or "normal"
    prices = PRICE_TABLE.get(rank, PRICE_TABLE["normal"])

    return {
        "id":              store_id,
        "name":            detail.get("name", ""),
        "address":         detail.get("address", ""),
        "coords":          detail.get("coords"),
        "prefecture_code": int(pref_code),
        "price_rank":      rank,
        "price_takeout":   prices["takeout"],
        "price_instore":   prices["in_store"],
        "options": {
            "reserve":    detail.get("reserve", False),
            "teavana":    detail.get("is_teavana", False),
            "drive_thru": detail.get("drive_thru", False),
            "wifi":       False,  # HTMLからは取得困難
        },
        "hours":           detail.get("hours", {}),
        "store_type":      detail.get("store_type"),
        "detail_fetched":  detail.get("fetch_ok", False),
        "scraped_at":      datetime.now().isoformat(),
    }

# ============================================================
# 県スクレイプ処理
# ============================================================
def scrape_prefecture(pref_code: str, failed: dict) -> tuple[bool, int, int]:
    name = PREFECTURE_CONFIG[pref_code]["name"]
    log.info(f"===== {pref_code}: {name} 開始 =====")

    store_list = fetch_store_ids_in_pref(pref_code)
    if not store_list:
        log.error(f"{name}: 店舗リスト取得失敗")
        notify_error(f"{name}（{pref_code}）の店舗リスト取得失敗")
        return False, 0, 0

    log.info(f"{name}: {len(store_list)}店舗")

    # 既存データ読み込み
    out_path = DATA_DIR / f"shops_{pref_code}.json"
    existing = {}
    if out_path.exists():
        for s in json.loads(out_path.read_text()):
            existing[s["id"]] = s

    shops_map = {}
    success_count = skip_count = 0

    for i, store_info in enumerate(store_list, 1):
        store_id   = store_info["store_id"]
        store_name = store_info["name"]
        log.info(f"  [{i}/{len(store_list)}] {store_name} ({store_id})")

        if should_skip(failed, store_id):
            log.info(f"    → スキップ（過去の失敗記録）")
            skip_count += 1
            shops_map[store_id] = existing.get(store_id) or build_shop_record(
                store_id, pref_code,
                {"price_rank": None, "fetch_ok": False, "failure_reason": failed[store_id]["reason"]}
            )
            continue

        detail = fetch_store_detail(store_id)

        if not detail["fetch_ok"]:
            reason = detail.get("failure_reason", FailureReason.TEMP_ERROR)
            record_failure(failed, store_id, store_name, reason)
            save_failed(failed)
        else:
            if store_id in failed:
                del failed[store_id]
                save_failed(failed)
            success_count += 1
            log.info(f"    価格ランク: {detail.get('price_rank')} / ティバーナ: {detail.get('is_teavana')} / 座標: {detail.get('coords') is not None}")

        shops_map[store_id] = build_shop_record(store_id, pref_code, detail)

        # 詳細ページHTML取得後の待機（長めに）
        time.sleep(random.uniform(10, 20))

    shops = list(shops_map.values())
    out_path.write_text(json.dumps(shops, ensure_ascii=False, indent=2))
    log.info(f"{name}: {len(shops)}件保存（成功{success_count}件 スキップ{skip_count}件）")
    return True, success_count, skip_count

# ============================================================
# リトライ処理
# ============================================================
def retry_failed_stores(failed: dict):
    targets = [
        (sid, entry) for sid, entry in list(failed.items())
        if entry["reason"] == FailureReason.TEMP_ERROR and entry["count"] < MAX_RETRY
    ]
    if not targets:
        log.info("リトライ対象なし。")
        return
    log.info(f"===== リトライ: {len(targets)}件 =====")
    for store_id, entry in targets:
        name = entry.get("name", store_id)
        log.info(f"  リトライ: {name} ({store_id})")
        detail = fetch_store_detail(store_id)
        if detail["fetch_ok"]:
            log.info(f"    成功")
            del failed[store_id]
            save_failed(failed)
            # 既存JSONを更新
            pref_code = None
            for code in PREFECTURE_CONFIG:
                path = DATA_DIR / f"shops_{code}.json"
                if not path.exists():
                    continue
                shops = json.loads(path.read_text())
                for s in shops:
                    if s["id"] == store_id:
                        pref_code = str(s.get("prefecture_code", "")).zfill(2)
                        break
                if pref_code:
                    break
            if pref_code:
                path = DATA_DIR / f"shops_{pref_code}.json"
                shops = json.loads(path.read_text())
                for j, s in enumerate(shops):
                    if s["id"] == store_id:
                        shops[j] = build_shop_record(store_id, pref_code, detail)
                        break
                path.write_text(json.dumps(shops, ensure_ascii=False, indent=2))
        else:
            record_failure(failed, store_id, name, FailureReason.TEMP_ERROR)
            save_failed(failed)
        time.sleep(random.uniform(10, 20))

# ============================================================
# 差分検知
# ============================================================
def generate_diff() -> list:
    today_str = datetime.now().strftime("%Y-%m-%d")
    diffs = []

    for code in sorted(PREFECTURE_CONFIG.keys()):
        new_path = DATA_DIR / f"shops_{code}.json"
        old_path = DATA_DIR / f"shops_{code}_prev.json"

        if not new_path.exists():
            continue
        if not old_path.exists():
            shutil.copy(new_path, old_path)
            continue

        new_shops = {s["id"]: s for s in json.loads(new_path.read_text())}
        old_shops = {s["id"]: s for s in json.loads(old_path.read_text())}

        for sid, shop in new_shops.items():
            if sid not in old_shops:
                diffs.append({"type":"new_open","date":today_str,"name":shop["name"],"address":shop.get("address",""),"prefecture_code":shop.get("prefecture_code")})
                log.info(f"[DIFF] 新店: {shop['name']}")
            else:
                old = old_shops[sid]
                if shop.get("price_rank") != old.get("price_rank"):
                    diffs.append({"type":"price_change","date":today_str,"name":shop["name"],"old_rank":old.get("price_rank"),"new_rank":shop.get("price_rank")})
                    log.info(f"[DIFF] 価格変動: {shop['name']} {old.get('price_rank')} → {shop.get('price_rank')}")
                for key in ("reserve","teavana","drive_thru"):
                    ov = old.get("options",{}).get(key)
                    nv = shop.get("options",{}).get(key)
                    if ov != nv and ov is not None:
                        diffs.append({"type":"facility_change","date":today_str,"name":shop["name"],"item":key,"old":ov,"new":nv})

        for sid, shop in old_shops.items():
            if sid not in new_shops:
                diffs.append({"type":"closed","date":today_str,"name":shop["name"],"address":shop.get("address","")})
                log.info(f"[DIFF] 閉店: {shop['name']}")

        shutil.copy(new_path, old_path)

    if diffs:
        out_path = DATA_DIR / f"diff_{today_str}.json"
        existing = json.loads(out_path.read_text()) if out_path.exists() else []
        existing.extend(diffs)
        out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        log.info(f"差分: {len(diffs)}件 → {out_path}")
    else:
        log.info("差分: 変更なし")
    return diffs

# ============================================================
# サマリー生成
# ============================================================
def generate_summary():
    summary = []
    missing = []
    for code in sorted(PREFECTURE_CONFIG.keys()):
        path = DATA_DIR / f"shops_{code}.json"
        if not path.exists():
            missing.append(code)
            continue
        for s in json.loads(path.read_text()):
            if not s.get("coords"):
                continue
            summary.append({
                "id":              s["id"],
                "coords":          s["coords"],
                "prefecture_code": s.get("prefecture_code"),
                "price_rank":      s.get("price_rank"),
                "store_type":      s.get("store_type"),
                "options":         s.get("options", {}),
            })
    out_path = DATA_DIR / "all_summary.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    log.info(f"サマリー: {len(summary)}店舗 / {out_path.stat().st_size/1024:.1f}KB")
    if missing:
        log.info(f"未取得県: {[PREFECTURE_CONFIG[c]['name'] for c in missing]}")

# ============================================================
# エントリーポイント
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["full","retry"], default="full")
    args = parser.parse_args()

    state  = load_state()
    failed = load_failed()
    today  = datetime.now().strftime("%Y-%m-%d")

    if args.mode == "retry":
        log.info("===== モード: リトライのみ =====")
        retry_failed_stores(failed)
        diffs = generate_diff()
        generate_summary()
        if diffs:
            notify_diff(diffs)
        notify_log(f"🔄 **[スタバ] リトライ完了** {today} 差分: {len(diffs)}件")
        return

    log.info("===== モード: フル =====")
    targets = get_next_prefectures(state)
    log.info(f"本日の対象 ({BATCH_COUNT}県): {[PREFECTURE_CONFIG[c]['name'] for c in targets]}")

    total_ok = total_skip = 0
    for pref_code in targets:
        ok, s_ok, s_skip = scrape_prefecture(pref_code, failed)
        if ok:
            state["completed"].append(pref_code)
            state["last_run_date"] = today
            save_state(state)
            total_ok   += s_ok
            total_skip += s_skip
        else:
            notify_error(f"{PREFECTURE_CONFIG[pref_code]['name']}の処理失敗")

        if pref_code != targets[-1]:
            wait = random.uniform(30, 60)
            log.info(f"次の県まで{wait:.0f}秒待機...")
            time.sleep(wait)

    retry_failed_stores(failed)
    diffs = generate_diff()
    generate_summary()

    if diffs:
        notify_diff(diffs)

    completed_names = [PREFECTURE_CONFIG[c]["name"] for c in targets if c in state.get("completed",[])]
    notify_log(
        f"✅ **[スタバ] 本日の処理完了** {today}\n"
        f"処理県: {', '.join(completed_names)}\n"
        f"取得: {total_ok}件 / スキップ: {total_skip}件 / 差分: {len(diffs)}件"
    )
    log.info("本日分完了。")


if __name__ == "__main__":
    main()