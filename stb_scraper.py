"""
Starbucks Japan Store Price Scraper
====================================
使い方:
  python3 stb_scraper.py              # フルモード
  python3 stb_scraper.py --mode=full
  python3 stb_scraper.py --mode=retry # 失敗店舗リトライのみ

cron設定例:
  0 14 * * * cd /home/pi/stb && /usr/bin/python3 stb_scraper.py --mode=full >> logs/scraper.log 2>&1
  0 17 * * * cd /home/pi/stb && /usr/bin/python3 stb_scraper.py --mode=retry >> logs/retry.log 2>&1

価格テーブル（スタバラテ Tall, 2025年調査値）:
  通常: テイクアウト¥491 / 店内¥500
  A:    テイクアウト¥510 / 店内¥520
  B:    テイクアウト¥501 / 店内¥510
  ※価格改定時はPRICE_TABLE を更新すること

store_type 判明値:
  1: 通常店舗
  3: ドライブスルーあり（要追加確認）
  ※不明値は raw_store_type に保持
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
BATCH_COUNT = 3   # 1日に処理する県数（429が多発するなら2に下げる）

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

# store_type → ドライブスルー判定（判明分のみ。不明は None）
STORE_TYPE_DT = {
    "1":  None,   # 通常店舗（DTは個別確認が必要）
    "2":  None,   # ライセンス店（空港・病院・SA等）DTは個別
    "3":  True,   # ロードサイド独立店（DTあり率高、要確認）
    "11": False,  # ロースタリー（DT不可）
}

# ============================================================
# 都道府県設定（JIS X 0401準拠）
# ============================================================
PREFECTURE_CONFIG = {
    "01": "北海道", "02": "青森県", "03": "岩手県", "04": "宮城県",
    "05": "秋田県", "06": "山形県", "07": "福島県", "08": "茨城県",
    "09": "栃木県", "10": "群馬県", "11": "埼玉県", "12": "千葉県",
    "13": "東京都", "14": "神奈川県","15": "新潟県", "16": "富山県",
    "17": "石川県", "18": "福井県", "19": "山梨県", "20": "長野県",
    "21": "岐阜県", "22": "静岡県", "23": "愛知県", "24": "三重県",
    "25": "滋賀県", "26": "京都府", "27": "大阪府", "28": "兵庫県",
    "29": "奈良県", "30": "和歌山県","31": "鳥取県", "32": "島根県",
    "33": "岡山県", "34": "広島県", "35": "山口県", "36": "徳島県",
    "37": "香川県", "38": "愛媛県", "39": "高知県", "40": "福岡県",
    "41": "佐賀県", "42": "長崎県", "43": "熊本県", "44": "大分県",
    "45": "宮崎県", "46": "鹿児島県","47": "沖縄県",
}

# ============================================================
# APIエンドポイント
# ============================================================
BASE_API = "https://hn8madehag.execute-api.ap-northeast-1.amazonaws.com/prd-2019-08-21"
DETAIL_URL = "https://store.starbucks.co.jp/detail-{store_id}/"

HEADERS_API = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    "Accept": "application/json",
    "Referer": "https://store.starbucks.co.jp/",
}
HEADERS_HTML = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    "Accept": "text/html,application/xhtml+xml",
    "Referer": "https://store.starbucks.co.jp/",
}

MAX_RETRY = 3

# ============================================================
# 失敗理由
# ============================================================
class FailureReason:
    NOT_FOUND  = "not_found"   # 404（永続スキップ）
    TEMP_ERROR = "temp_error"  # 一時的エラー（リトライ対象）
    PARSE_ERR  = "parse_error" # HTMLパース失敗（永続スキップ）

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
def get_with_backoff(url: str, headers: dict, params: dict = None) -> requests.Response | None:
    """429対応の指数バックオフ付きGET。Noneは完全失敗を意味する。"""
    wait = 10
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)

            if resp.status_code == 200:
                return resp

            if resp.status_code == 429:
                log.warning(f"    429 Too Many Requests (試行{attempt}/{MAX_RETRY}) {wait}秒待機...")
                time.sleep(wait)
                wait = min(wait * 2, 120)  # 最大120秒
                continue

            if resp.status_code == 404:
                log.warning(f"    404: {url}")
                return resp  # 呼び出し元で判断

            log.warning(f"    HTTP {resp.status_code} (試行{attempt}/{MAX_RETRY})")

        except requests.Timeout:
            log.warning(f"    タイムアウト (試行{attempt}/{MAX_RETRY})")
        except requests.RequestException as e:
            log.warning(f"    接続エラー: {e} (試行{attempt}/{MAX_RETRY})")

        time.sleep(random.uniform(15, 30))

    return None

# ============================================================
# Step 1: 市区町村リスト取得
# ============================================================
def fetch_cities(pref_code: str) -> list[str]:
    url = f"{BASE_API}/facet"
    params = {
        "size": "100",
        "q.parser": "structured",
        "q": f"(and ver:10000 record_type:1 pref_code:{pref_code})",
    }
    resp = get_with_backoff(url, HEADERS_API, params)
    if not resp or resp.status_code != 200:
        return []
    try:
        buckets = resp.json()["facets"]["address_2"]["buckets"]
        return [b["value"] for b in buckets]
    except (KeyError, TypeError):
        log.warning(f"市区町村リスト取得失敗: {pref_code}")
        return []

# ============================================================
# Step 2: 店舗一覧取得
# ============================================================
def fetch_stores_in_city(pref_code: str, city: str) -> list[dict]:
    url = f"{BASE_API}/storesearch"
    params = {
        "size": "100",
        "q.parser": "structured",
        "q": f"(and ver:10000 record_type:1 pref_code:{pref_code} city:'{city}')",
    }
    resp = get_with_backoff(url, HEADERS_API, params)
    if not resp or resp.status_code != 200:
        return []
    try:
        hits = resp.json()["hits"]["hit"]
        return hits
    except (KeyError, TypeError):
        return []

# ============================================================
# Step 3: 詳細HTMLパース（価格ランク・ティバーナ・ドライブスルー）
# ============================================================
def fetch_store_detail(store_id: str) -> dict:
    """
    戻り値: {
        "price_rank": "A" | "B" | "normal" | None（取得失敗）,
        "is_teavana": bool,
        "drive_thru": bool | None,  # HTMLから判定できない場合None
        "fetch_ok": bool,
    }
    """
    url = DETAIL_URL.format(store_id=store_id)
    resp = get_with_backoff(url, HEADERS_HTML)

    if resp is None:
        return {"price_rank": None, "is_teavana": False, "drive_thru": None, "fetch_ok": False}
    if resp.status_code == 404:
        return {"price_rank": None, "is_teavana": False, "drive_thru": None, "fetch_ok": False, "not_found": True}

    try:
        soup = BeautifulSoup(resp.text, "html.parser")

        # 価格ランク判定
        price_rank = "normal"
        for notice in soup.find_all("div", class_="notice-text"):
            text = notice.get_text()
            m = re.search(r"特定立地価格\s*([A-Z])", text)
            if m:
                price_rank = m.group(1)
                break

        # ティバーナ判定（<a class="tea-logo"> の存在のみ）
        is_teavana = bool(soup.find("a", class_="tea-logo"))

        # ドライブスルー判定（HTMLから取れる場合。store_typeで補完する）
        # TODO: 実際のHTMLでクラス名が判明したら更新
        drive_thru = None

        return {
            "price_rank": price_rank,
            "is_teavana": is_teavana,
            "drive_thru": drive_thru,
            "fetch_ok": True,
        }

    except Exception as e:
        log.warning(f"    HTMLパース失敗: {store_id} - {e}")
        return {"price_rank": None, "is_teavana": False, "drive_thru": None, "fetch_ok": False}

# ============================================================
# データ変換
# ============================================================
def build_store_record(raw: dict, detail: dict, pref_code: str) -> dict:
    fields = raw.get("fields", {})
    store_id = fields.get("store_id", "")
    store_type = fields.get("store_type", "")
    price_rank = detail.get("price_rank", "normal") or "normal"

    # store_typeからドライブスルー判定。HTMLで取れた場合はそちらを優先
    drive_thru_from_type = STORE_TYPE_DT.get(store_type)  # None=不明
    drive_thru = detail.get("drive_thru")
    if drive_thru is None:
        drive_thru = drive_thru_from_type

    # 座標（APIは "lat,lng" 形式の文字列）
    coords = None
    loc_str = fields.get("location_jp") or fields.get("location")
    if loc_str:
        try:
            lat, lng = [float(x) for x in loc_str.split(",")]
            coords = [lat, lng]
        except ValueError:
            pass

    # 営業時間（曜日ごと）
    hours = {}
    for day in ("mon","tue","wed","thu","fri","sat","sun","hol"):
        o = fields.get(f"{day}_open")
        c = fields.get(f"{day}_close")
        if o and c:
            hours[day] = {"open": o, "close": c}

    prices = PRICE_TABLE.get(price_rank, PRICE_TABLE["normal"])

    return {
        "id":             store_id,
        "api_id":         raw.get("id", ""),
        "name":           fields.get("name", ""),
        "address":        fields.get("address_5", ""),
        "coords":         coords,
        "prefecture_code": int(pref_code),
        "price_rank":     price_rank,           # "normal" | "A" | "B" | ...
        "price_takeout":  prices["takeout"],
        "price_instore":  prices["in_store"],
        "options": {
            "reserve":    fields.get("reserve_flg") == "1",
            "teavana":    detail.get("is_teavana", False),
            "drive_thru": bool(drive_thru) if drive_thru is not None else False,
            "wifi":       fields.get("public_wireless_service_flg") == "1",
        },
        "hours":          hours,
        "store_type":     store_type,           # raw値を保持（将来の解析用）
        "detail_fetched": detail.get("fetch_ok", False),
        "scraped_at":     datetime.now().isoformat(),
    }

# ============================================================
# 県スクレイプ処理
# ============================================================
def scrape_prefecture(pref_code: str, failed: dict) -> tuple[bool, int, int]:
    """戻り値: (success, 成功件数, スキップ件数)"""
    name = PREFECTURE_CONFIG[pref_code]
    log.info(f"===== {pref_code}: {name} 開始 =====")

    cities = fetch_cities(pref_code)
    if not cities:
        log.error(f"{name}: 市区町村リスト取得失敗")
        notify_error(f"{name}（{pref_code}）の市区町村リスト取得失敗")
        return False, 0, 0

    log.info(f"{name}: {len(cities)}市区町村")

    # 既存データ読み込み（リトライ成功時の上書き用）
    out_path = DATA_DIR / f"shops_{pref_code}.json"
    existing = {}
    if out_path.exists():
        for s in json.loads(out_path.read_text()):
            existing[s["id"]] = s

    shops_map = {}
    success_count = 0
    skip_count = 0

    for city in cities:
        raw_stores = fetch_stores_in_city(pref_code, city)
        log.info(f"  {city}: {len(raw_stores)}店舗")
        time.sleep(random.uniform(3, 6))  # 市区町村間

        for raw in raw_stores:
            fields = raw.get("fields", {})
            store_id = fields.get("store_id", "")
            store_name = fields.get("name", "")

            if not store_id:
                continue

            log.info(f"    [{store_name}] ({store_id})")

            if should_skip(failed, store_id):
                log.info(f"      → スキップ（過去の失敗記録）")
                skip_count += 1
                shops_map[store_id] = existing.get(store_id) or build_store_record(
                    raw, {"price_rank": None, "is_teavana": False, "drive_thru": None, "fetch_ok": False}, pref_code
                )
                continue

            # Step 3: 詳細HTML取得
            detail = fetch_store_detail(store_id)

            if not detail["fetch_ok"]:
                reason = FailureReason.NOT_FOUND if detail.get("not_found") else FailureReason.TEMP_ERROR
                record_failure(failed, store_id, store_name, reason)
                save_failed(failed)
            else:
                # リトライ成功なら失敗記録を削除
                if store_id in failed:
                    del failed[store_id]
                    save_failed(failed)
                success_count += 1

            shops_map[store_id] = build_store_record(raw, detail, pref_code)
            log.info(f"      価格ランク: {shops_map[store_id]['price_rank']} / ティバーナ: {detail.get('is_teavana')}")

            # Step 3はHTMLアクセスなので長めに待機
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
            log.info(f"    成功: ランク={detail['price_rank']}")
            del failed[store_id]
            save_failed(failed)
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
                    diffs.append({"type":"price_change","date":today_str,"name":shop["name"],"address":shop.get("address",""),"old_rank":old.get("price_rank"),"new_rank":shop.get("price_rank")})
                    log.info(f"[DIFF] 価格変動: {shop['name']} {old.get('price_rank')} → {shop.get('price_rank')}")
                for key in ("reserve","teavana","drive_thru"):
                    ov = old.get("options",{}).get(key)
                    nv = shop.get("options",{}).get(key)
                    if ov != nv and ov is not None:
                        diffs.append({"type":"facility_change","date":today_str,"name":shop["name"],"item":key,"old":ov,"new":nv})

        for sid, shop in old_shops.items():
            if sid not in new_shops:
                diffs.append({"type":"closed","date":today_str,"name":shop["name"],"address":shop.get("address",""),"prefecture_code":shop.get("prefecture_code")})
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
                "options":         s.get("options", {}),
            })
    out_path = DATA_DIR / "all_summary.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    log.info(f"サマリー: {len(summary)}店舗 / {out_path.stat().st_size/1024:.1f}KB")
    if missing:
        log.info(f"未取得県: {[PREFECTURE_CONFIG[c] for c in missing]}")

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

    # --- フルモード ---
    log.info("===== モード: フル =====")
    targets = get_next_prefectures(state)
    log.info(f"本日の対象 ({BATCH_COUNT}県): {[PREFECTURE_CONFIG[c] for c in targets]}")

    total_ok = total_skip = 0
    for pref_code in targets:
        ok, s_ok, s_skip = scrape_prefecture(pref_code, failed)
        if ok:
            state["completed"].append(pref_code)
            state["last_run_date"] = today
            save_state(state)
            total_ok += s_ok
            total_skip += s_skip
        else:
            notify_error(f"{PREFECTURE_CONFIG[pref_code]}の処理失敗")

        if pref_code != targets[-1]:
            wait = random.uniform(60, 120)  # 県間は長めに
            log.info(f"次の県まで{wait:.0f}秒待機...")
            time.sleep(wait)

    retry_failed_stores(failed)
    diffs = generate_diff()
    generate_summary()

    if diffs:
        notify_diff(diffs)

    completed_names = [PREFECTURE_CONFIG[c] for c in targets if c in state.get("completed", [])]
    notify_log(
        f"✅ **[スタバ] 本日の処理完了** {today}\n"
        f"処理県: {', '.join(completed_names)}\n"
        f"取得: {total_ok}件 / スキップ: {total_skip}件 / 差分: {len(diffs)}件"
    )
    log.info("本日分完了。")


if __name__ == "__main__":
    main()