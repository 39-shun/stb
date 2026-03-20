"""
Starbucks Japan Store Price Scraper
=====================================
使い方:
  python3 stb_scraper.py              # フルモード
  python3 stb_scraper.py --mode=full
  python3 stb_scraper.py --mode=retry

cron設定例:
  0 14 * * * cd /home/pi/stb && /usr/bin/python3 stb_scraper.py --mode=full >> logs/scraper.log 2>&1
  0 17 * * * cd /home/pi/stb && /usr/bin/python3 stb_scraper.py --mode=retry >> logs/retry.log 2>&1

価格テーブル（スタバラテ Tall, 2025年調査値）:
  通常: テイクアウト¥491 / 店内¥500
  A:    テイクアウト¥510 / 店内¥520
  B:    テイクアウト¥501 / 店内¥510

store_type判明値:
  1: 通常店舗
  2: 特殊施設（空港・病院・SA等）
  3: ロードサイド独立店
  11: ROASTERY
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
BATCH_COUNT = 3

# ============================================================
# パス設定
# ============================================================
BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
LOG_DIR     = BASE_DIR / "logs"
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
# Discord Webhook
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

def notify_log(msg):   _discord_post(WEBHOOK_LOG,   msg)
def notify_error(context, exc=None):
    lines = [f"🚨 **[スタバ] エラー発生** — {context}"]
    if exc:
        lines.append(f"```{traceback.format_exc()[-1200:]}```")
    _discord_post(WEBHOOK_ERROR, "\n".join(lines))
def notify_diff(diffs):
    if not diffs or not WEBHOOK_DIFF:
        return
    lines = ["📢 **[スタバ] 差分検知レポート**"]
    for d in diffs[:20]:
        if   d["type"] == "new_open":       lines.append(f"🆕 新店: **{d['name']}**")
        elif d["type"] == "closed":         lines.append(f"🚫 閉店: **{d['name']}**")
        elif d["type"] == "price_change":   lines.append(f"💰 価格変動: **{d['name']}** {d['old_rank']} → {d['new_rank']}")
        elif d["type"] == "facility_change":
            label = {"reserve":"リザーブ","teavana":"ティバーナ","drive_thru":"DT"}.get(d["item"], d["item"])
            lines.append(f"🏪 設備変更: **{d['name']}** {label} {'追加✅' if d['new'] else '廃止❌'}")
    if len(diffs) > 20:
        lines.append(f"…他 {len(diffs)-20} 件")
    _discord_post(WEBHOOK_DIFF, "\n".join(lines))

# ============================================================
# 価格テーブル・定数
# ============================================================
PRICE_TABLE = {
    "A":      {"takeout": 510, "in_store": 520},
    "B":      {"takeout": 501, "in_store": 510},
    "normal": {"takeout": 491, "in_store": 500},
}

# store_type → ドライブスルー推定（None=不明）
STORE_TYPE_DT = {"1": None, "2": None, "3": True, "11": False}

BASE_API   = "https://hn8madehag.execute-api.ap-northeast-1.amazonaws.com/prd-2019-08-21"
DETAIL_URL = "https://store.starbucks.co.jp/detail-{store_id}/"

# ブラウザを模倣したヘッダー（Sec-Fetch系が認証の鍵）
HEADERS_API = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Origin":          "https://store.starbucks.co.jp",
    "Referer":         "https://store.starbucks.co.jp/",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "cross-site",
}
HEADERS_HTML = {
    "User-Agent":      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    "Accept":          "text/html,application/xhtml+xml",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer":         "https://store.starbucks.co.jp/",
}

MAX_RETRY = 3

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
# 失敗理由
# ============================================================
class FailureReason:
    NOT_SUPPORTED = "not_supported"
    TEMP_ERROR    = "temp_error"
    NO_PRICE      = "no_price_data"

# ============================================================
# 失敗店舗管理
# ============================================================
def load_failed() -> dict:
    return json.loads(FAILED_FILE.read_text()) if FAILED_FILE.exists() else {}

def save_failed(failed: dict):
    FAILED_FILE.write_text(json.dumps(failed, ensure_ascii=False, indent=2))

def record_failure(failed, store_id, name, reason):
    entry = failed.get(store_id, {"reason": reason, "count": 0, "name": name})
    entry["count"] += 1
    entry["reason"] = reason
    entry["last_failed"] = datetime.now().isoformat()
    failed[store_id] = entry
    log.warning(f"    失敗記録: {name} ({store_id}) - {reason} (累計{entry['count']}回)")

def should_skip(failed, store_id):
    entry = failed.get(store_id)
    if not entry: return False
    if entry["reason"] in (FailureReason.NOT_SUPPORTED, FailureReason.NO_PRICE): return True
    if entry["reason"] == FailureReason.TEMP_ERROR and entry["count"] >= MAX_RETRY:
        log.warning(f"    {store_id} は{MAX_RETRY}回失敗済み。スキップします。")
        return True
    return False

# ============================================================
# 状態管理
# ============================================================
def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"completed": [], "last_run_date": None}

def save_state(state): STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def get_next_prefectures(state) -> list[str]:
    all_codes = sorted(PREFECTURE_CONFIG.keys())
    completed = set(state.get("completed", []))
    pending   = [c for c in all_codes if c not in completed]
    if not pending:
        log.info("全都道府県完了。次のサイクル開始。")
        state["completed"] = []
        save_state(state)
        pending = all_codes
    return pending[:BATCH_COUNT]

# ============================================================
# APIクライアント（指数バックオフ付き）
# ============================================================
def api_get(url: str, params: dict = None) -> tuple[dict | list | None, str | None]:
    wait = 15
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = requests.get(url, headers=HEADERS_API, params=params, timeout=20)
            if resp.status_code == 200:
                return resp.json(), None
            if resp.status_code == 429:
                log.warning(f"    429 Too Many Requests (試行{attempt}) {wait}秒待機...")
                time.sleep(wait)
                wait = min(wait * 2, 180)
                continue
            log.warning(f"    HTTP {resp.status_code} (試行{attempt}/{MAX_RETRY})")
        except requests.Timeout:
            log.warning(f"    タイムアウト (試行{attempt}/{MAX_RETRY})")
        except requests.RequestException as e:
            log.warning(f"    接続エラー: {e} (試行{attempt}/{MAX_RETRY})")
        if attempt < MAX_RETRY:
            time.sleep(random.uniform(10, 20))
    return None, FailureReason.TEMP_ERROR

# ============================================================
# Step 1: 都道府県の店舗一覧取得（API）
# ============================================================
def fetch_stores_in_pref(pref_code: str) -> list[dict]:
    """
    pref_codeは数値文字列（例:"02"）だが、APIは整数として受け付ける。
    100件超の場合はstartパラメータでページング。
    """
    stores = []
    start  = 0
    pref_int = int(pref_code)  # APIは整数コードで受け付ける

    while True:
        params = {
            "size":     100,
            "q.parser": "structured",
            "q":        f"(and ver:10000 record_type:1 pref_code:{pref_int})",
            "fq":       "(and data_type:'prd')",
            "sort":     "zip_code asc,store_id asc",
            "start":    start,
        }
        data, reason = api_get(f"{BASE_API}/storesearch", params)
        if not data:
            log.error(f"  店舗一覧APIエラー (pref:{pref_code} start:{start})")
            break

        hits = data.get("hits", {})
        batch = hits.get("hit", [])
        stores.extend(batch)

        found = hits.get("found", 0)
        start += len(batch)
        if start >= found or not batch:
            break

        time.sleep(random.uniform(3, 6))

    return stores

# ============================================================
# Step 2: 詳細HTMLから価格ランク・ティバーナを取得
# ============================================================
def fetch_detail_html(store_id: str) -> tuple[str | None, str | None]:
    url = DETAIL_URL.format(store_id=store_id)
    wait = 10
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = requests.get(url, headers=HEADERS_HTML, timeout=20)
            if resp.status_code == 200:
                return resp.text, None
            if resp.status_code == 404:
                return None, FailureReason.NOT_SUPPORTED
            if resp.status_code == 429:
                log.warning(f"    429 詳細ページ (試行{attempt}) {wait}秒待機...")
                time.sleep(wait)
                wait = min(wait * 2, 120)
                continue
            log.warning(f"    詳細HTTP {resp.status_code} (試行{attempt}/{MAX_RETRY})")
        except requests.Timeout:
            log.warning(f"    詳細タイムアウト (試行{attempt}/{MAX_RETRY})")
        except requests.RequestException as e:
            log.warning(f"    詳細接続エラー: {e}")
        if attempt < MAX_RETRY:
            time.sleep(random.uniform(10, 20))
    return None, FailureReason.TEMP_ERROR

def parse_detail(html: str) -> dict:
    """詳細HTMLから価格ランクとティバーナフラグを抽出"""
    soup = BeautifulSoup(html, "html.parser")
    price_rank = "normal"
    for notice in soup.find_all("div", class_="notice-text"):
        m = re.search(r"特定立地価格\s*([A-Z])", notice.get_text())
        if m:
            price_rank = m.group(1)
            break
    is_teavana = bool(soup.find("a", class_="tea-logo"))
    return {"price_rank": price_rank, "is_teavana": is_teavana}

# ============================================================
# データ変換
# ============================================================
def build_shop_record(raw: dict, pref_code: str, detail: dict) -> dict:
    fields     = raw.get("fields", {})
    store_id   = fields.get("store_id", raw.get("id", "").replace("2001000011",""))
    store_type = fields.get("store_type", "")
    rank       = detail.get("price_rank", "normal") or "normal"
    prices     = PRICE_TABLE.get(rank, PRICE_TABLE["normal"])

    # 座標
    coords = None
    loc_str = fields.get("location_jp") or fields.get("location")
    if loc_str:
        try:
            lat, lng = [float(x) for x in loc_str.split(",")]
            coords = [lat, lng]
        except ValueError:
            pass

    # 営業時間
    hours = {}
    for day in ("mon","tue","wed","thu","fri","sat","sun","hol"):
        o = fields.get(f"{day}_open")
        c = fields.get(f"{day}_close")
        if o and c:
            hours[day] = {"open": o, "close": c}

    # ドライブスルー（store_typeから推定、詳細HTMLでは取得しない）
    drive_thru = STORE_TYPE_DT.get(store_type)

    return {
        "id":              fields.get("store_id", ""),
        "api_id":          raw.get("id", ""),
        "name":            fields.get("name", ""),
        "address":         fields.get("address_5", ""),
        "coords":          coords,
        "prefecture_code": int(pref_code),
        "price_rank":      rank,
        "price_takeout":   prices["takeout"],
        "price_instore":   prices["in_store"],
        "options": {
            "reserve":    fields.get("reserve_flg") == "1",
            "teavana":    detail.get("is_teavana", False),
            "drive_thru": bool(drive_thru) if drive_thru is not None else False,
            "wifi":       fields.get("public_wireless_service_flg") == "1",
        },
        "hours":           hours,
        "store_type":      store_type,
        "detail_fetched":  detail.get("fetch_ok", False),
        "scraped_at":      datetime.now().isoformat(),
    }

# ============================================================
# 県スクレイプ処理
# ============================================================
def scrape_prefecture(pref_code: str, failed: dict) -> tuple[bool, int, int]:
    name = PREFECTURE_CONFIG[pref_code]
    log.info(f"===== {pref_code}: {name} 開始 =====")

    raw_stores = fetch_stores_in_pref(pref_code)
    if not raw_stores:
        log.error(f"{name}: 店舗一覧取得失敗")
        notify_error(f"{name}（{pref_code}）の店舗一覧取得失敗")
        return False, 0, 0

    log.info(f"{name}: {len(raw_stores)}店舗")

    out_path = DATA_DIR / f"shops_{pref_code}.json"
    existing = {}
    if out_path.exists():
        for s in json.loads(out_path.read_text()):
            existing[s["id"]] = s

    # temp_errorの既知失敗店舗を先頭に（優先リトライ）
    retry_ids   = {r["fields"]["store_id"] for r in raw_stores
                   if r["fields"].get("store_id") in failed
                   and failed[r["fields"]["store_id"]]["reason"] == FailureReason.TEMP_ERROR}
    ordered     = sorted(raw_stores,
                         key=lambda r: 0 if r["fields"].get("store_id") in retry_ids else 1)

    shops_map    = {}
    success_count = skip_count = 0

    for i, raw in enumerate(ordered, 1):
        fields     = raw.get("fields", {})
        store_id   = fields.get("store_id", "")
        store_name = fields.get("name", "")
        log.info(f"  [{i}/{len(ordered)}] {store_name} ({store_id})")

        if should_skip(failed, store_id):
            log.info(f"    → スキップ")
            skip_count += 1
            shops_map[store_id] = existing.get(store_id) or build_shop_record(
                raw, pref_code, {"price_rank": "normal", "is_teavana": False, "fetch_ok": False}
            )
            continue

        html, reason = fetch_detail_html(store_id)
        if html:
            detail = parse_detail(html)
            detail["fetch_ok"] = True
            if store_id in failed:
                del failed[store_id]
                save_failed(failed)
            success_count += 1
            log.info(f"    価格ランク: {detail['price_rank']} / ティバーナ: {detail['is_teavana']}")
        else:
            detail = {"price_rank": "normal", "is_teavana": False, "fetch_ok": False}
            record_failure(failed, store_id, store_name, reason or FailureReason.TEMP_ERROR)
            save_failed(failed)

        shops_map[store_id] = build_shop_record(raw, pref_code, detail)
        time.sleep(random.uniform(8, 15))

    shops = list(shops_map.values())
    out_path.write_text(json.dumps(shops, ensure_ascii=False, indent=2))
    log.info(f"{name}: {len(shops)}件保存（成功{success_count}件 スキップ{skip_count}件）")
    return True, success_count, skip_count

# ============================================================
# リトライ処理
# ============================================================
def retry_failed_stores(failed: dict):
    targets = [(sid, e) for sid, e in list(failed.items())
               if e["reason"] == FailureReason.TEMP_ERROR and e["count"] < MAX_RETRY]
    if not targets:
        log.info("リトライ対象なし。")
        return
    log.info(f"===== リトライ: {len(targets)}件 =====")
    for store_id, entry in targets:
        name = entry.get("name", store_id)
        log.info(f"  リトライ: {name} ({store_id})")
        html, reason = fetch_detail_html(store_id)
        if html:
            detail = parse_detail(html)
            detail["fetch_ok"] = True
            log.info(f"    成功: ランク={detail['price_rank']}")
            del failed[store_id]
            save_failed(failed)
            # 既存JSONを更新
            for code in sorted(PREFECTURE_CONFIG.keys()):
                path = DATA_DIR / f"shops_{code}.json"
                if not path.exists(): continue
                shops = json.loads(path.read_text())
                updated = False
                for j, s in enumerate(shops):
                    if s["id"] == store_id:
                        shops[j]["price_rank"]     = detail["price_rank"]
                        shops[j]["price_takeout"]  = PRICE_TABLE.get(detail["price_rank"], PRICE_TABLE["normal"])["takeout"]
                        shops[j]["price_instore"]  = PRICE_TABLE.get(detail["price_rank"], PRICE_TABLE["normal"])["in_store"]
                        shops[j]["options"]["teavana"] = detail["is_teavana"]
                        shops[j]["detail_fetched"] = True
                        shops[j]["scraped_at"]     = datetime.now().isoformat()
                        updated = True
                        break
                if updated:
                    path.write_text(json.dumps(shops, ensure_ascii=False, indent=2))
                    break
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
        if not new_path.exists(): continue
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
                    ov, nv = old.get("options",{}).get(key), shop.get("options",{}).get(key)
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
            if not s.get("coords"): continue
            summary.append({
                "id":              s["id"],
                "coords":          s["coords"],
                "prefecture_code": s.get("prefecture_code"),
                "price_rank":      s.get("price_rank"),
                "store_type":      s.get("store_type"),
                "options":         s.get("options", {}),
            })
    out_path = DATA_DIR / "all_summary.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, separators=(",",":")))
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
        if diffs: notify_diff(diffs)
        notify_log(f"🔄 **[スタバ] リトライ完了** {today} 差分: {len(diffs)}件")
        return

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
            total_ok += s_ok; total_skip += s_skip
        else:
            notify_error(f"{PREFECTURE_CONFIG[pref_code]}の処理失敗")

        if pref_code != targets[-1]:
            wait = random.uniform(30, 60)
            log.info(f"次の県まで{wait:.0f}秒待機...")
            time.sleep(wait)

    retry_failed_stores(failed)
    diffs = generate_diff()
    generate_summary()
    if diffs: notify_diff(diffs)

    completed_names = [PREFECTURE_CONFIG[c] for c in targets if c in state.get("completed",[])]
    notify_log(
        f"✅ **[スタバ] 本日の処理完了** {today}\n"
        f"処理県: {', '.join(completed_names)}\n"
        f"取得: {total_ok}件 / スキップ: {total_skip}件 / 差分: {len(diffs)}件"
    )
    log.info("本日分完了。")

if __name__ == "__main__":
    main()