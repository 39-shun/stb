"""
既存の shops_XX.json の座標を、詳細HTMLのlatitude/longitudeで上書きするワンショットスクリプト。
APIのlocation_jpより詳細HTMLの座標の方が正確なことが判明したため。

使い方:
  python3 fix_coords.py               # 全県を順番に処理
  python3 fix_coords.py --pref 12     # 千葉県(12)だけ処理
  python3 fix_coords.py --dry-run     # ファイル更新なし（確認用）

注意:
  1店舗あたり8〜15秒待機するため、全国処理には数時間かかります。
  cronを止めてから実行することを推奨します。
"""

import argparse
import json
import random
import re
import time
import shutil
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ============================================================
# 設定
# ============================================================
DATA_DIR   = Path(__file__).parent / "data"
DETAIL_URL = "https://store.starbucks.co.jp/detail-{store_id}/"
MAX_RETRY  = 3

HEADERS_HTML = {
    "User-Agent":      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    "Accept":          "text/html,application/xhtml+xml",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer":         "https://store.starbucks.co.jp/",
}

PREFECTURE_CONFIG = {
    "01":"北海道","02":"青森県","03":"岩手県","04":"宮城県","05":"秋田県",
    "06":"山形県","07":"福島県","08":"茨城県","09":"栃木県","10":"群馬県",
    "11":"埼玉県","12":"千葉県","13":"東京都","14":"神奈川県","15":"新潟県",
    "16":"富山県","17":"石川県","18":"福井県","19":"山梨県","20":"長野県",
    "21":"岐阜県","22":"静岡県","23":"愛知県","24":"三重県","25":"滋賀県",
    "26":"京都府","27":"大阪府","28":"兵庫県","29":"奈良県","30":"和歌山県",
    "31":"鳥取県","32":"島根県","33":"岡山県","34":"広島県","35":"山口県",
    "36":"徳島県","37":"香川県","38":"愛媛県","39":"高知県","40":"福岡県",
    "41":"佐賀県","42":"長崎県","43":"熊本県","44":"大分県","45":"宮崎県",
    "46":"鹿児島県","47":"沖縄県",
}

# ============================================================
# HTML から座標を取得
# ============================================================
def fetch_coords_from_html(store_id: str) -> list | None:
    url = DETAIL_URL.format(store_id=store_id)
    wait = 10
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = requests.get(url, headers=HEADERS_HTML, timeout=20)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return parse_coords(resp.text)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                print(f"    429 待機 {wait}秒...")
                time.sleep(wait)
                wait = min(wait * 2, 120)
                continue
            print(f"    HTTP {resp.status_code} (試行{attempt})")
        except requests.Timeout:
            print(f"    タイムアウト (試行{attempt})")
        except requests.RequestException as e:
            print(f"    接続エラー: {e}")
        if attempt < MAX_RETRY:
            time.sleep(random.uniform(10, 20))
    return None

def parse_coords(html: str) -> list | None:
    """HTMLからlatitude/longitudeを抽出してWGS84座標を返す"""
    soup = BeautifulSoup(html, "html.parser")
    lat = lng = None

    # JSON-LD（構造化データ）から取得
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and "latitude" in data:
                lat = float(data["latitude"])
                lng = float(data["longitude"])
                break
            for item in (data if isinstance(data, list) else []):
                if isinstance(item, dict) and "latitude" in item:
                    lat = float(item["latitude"])
                    lng = float(item["longitude"])
                    break
        except Exception:
            pass

    # JSON-LDになければregexでHTMLからパース
    if lat is None:
        m = re.search(r'"latitude"\s*:\s*([\d.]+)', html)
        if m:
            lat = float(m.group(1))
        m = re.search(r'"longitude"\s*:\s*([\d.]+)', html)
        if m:
            lng = float(m.group(1))

    # 日本の範囲内かチェック
    if lat and lng and (24.0 <= lat <= 46.0) and (122.0 <= lng <= 154.0):
        return [lat, lng]
    return None

# ============================================================
# メイン処理
# ============================================================
def fix_pref(pref_code: str, dry_run: bool) -> tuple[int, int, int]:
    """1県を処理。(更新, スキップ, 失敗) 件数を返す"""
    path = DATA_DIR / f"shops_{pref_code}.json"
    if not path.exists():
        print(f"  {PREFECTURE_CONFIG[pref_code]}: ファイルなし")
        return 0, 0, 0

    shops = json.loads(path.read_text(encoding="utf-8"))
    updated = skipped = failed = 0

    for i, shop in enumerate(shops, 1):
        store_id = shop.get("id", "")
        name     = shop.get("name", store_id)
        old_coords = shop.get("coords")

        print(f"  [{i}/{len(shops)}] {name} ({store_id})", end="", flush=True)

        new_coords = fetch_coords_from_html(store_id)

        if new_coords is None:
            print(f" → 取得失敗")
            failed += 1
        elif new_coords == old_coords:
            print(f" → 変化なし {new_coords}")
            skipped += 1
        else:
            print(f" → {old_coords} → {new_coords}")
            if not dry_run:
                shops[i-1]["coords"] = new_coords
                shops[i-1]["scraped_at"] = datetime.now().isoformat()
            updated += 1

        time.sleep(random.uniform(8, 15))

    if not dry_run and updated > 0:
        # バックアップ
        backup = DATA_DIR / "_backup_coords" / path.name
        backup.parent.mkdir(exist_ok=True)
        if not backup.exists():
            shutil.copy(path, backup)
        path.write_text(json.dumps(shops, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  → {path.name} 保存（更新{updated}件）")

    return updated, skipped, failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pref", help="対象県コード（例: 12）。省略時は全県")
    parser.add_argument("--dry-run", action="store_true", help="ファイル更新なし（確認用）")
    args = parser.parse_args()

    targets = [args.pref.zfill(2)] if args.pref else sorted(PREFECTURE_CONFIG.keys())

    print(f"対象: {[PREFECTURE_CONFIG[c] for c in targets]}")
    if args.dry_run:
        print("【DRY RUN モード: ファイルは更新されません】")

    total_updated = total_skipped = total_failed = 0

    for code in targets:
        print(f"\n===== {code}: {PREFECTURE_CONFIG[code]} =====")
        u, s, f = fix_pref(code, args.dry_run)
        total_updated += u
        total_skipped += s
        total_failed  += f
        print(f"  小計: 更新{u} / 変化なし{s} / 失敗{f}")

        # サマリーも再生成（全県処理後）
    if not args.dry_run and total_updated > 0:
        print("\n===== all_summary.json を再生成 =====")
        summary = []
        for code in sorted(PREFECTURE_CONFIG.keys()):
            p = DATA_DIR / f"shops_{code}.json"
            if not p.exists(): continue
            for s in json.loads(p.read_text(encoding="utf-8")):
                if not s.get("coords"): continue
                summary.append({
                    "id":              s["id"],
                    "coords":          s["coords"],
                    "prefecture_code": s.get("prefecture_code"),
                    "price_rank":      s.get("price_rank"),
                    "store_type":      s.get("store_type"),
                    "options":         s.get("options", {}),
                    "hours":           s.get("hours", {}),
                })
        out = DATA_DIR / "all_summary.json"
        out.write_text(json.dumps(summary, ensure_ascii=False, separators=(",",":")), encoding="utf-8")
        print(f"  {len(summary)}店舗 → {out}")

    print(f"\n===== 完了 =====")
    print(f"更新: {total_updated} / 変化なし: {total_skipped} / 失敗: {total_failed}")
    if not args.dry_run and total_updated > 0:
        print("元データのバックアップ: data/_backup_coords/")


if __name__ == "__main__":
    main()
