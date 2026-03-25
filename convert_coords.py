"""
既存の shops_XX.json と all_summary.json の座標を
旧日本測地系（Tokyo Datum）→ WGS84 に一括変換するワンショットスクリプト。

使い方:
  python3 convert_coords.py          # data/ 配下を変換（バックアップあり）
  python3 convert_coords.py --dry-run # 変換結果を表示するだけ（ファイル更新なし）
"""

import argparse
import json
import shutil
from pathlib import Path

# ============================================================
# 設定
# ============================================================
DATA_DIR = Path(__file__).parent / "data"

# ============================================================
# Tokyo Datum → WGS84 変換式（国土地理院準拠の簡易近似）
# 誤差: 最大約1m（実用上問題なし）
# ============================================================
def tokyo_to_wgs84(lat: float, lng: float) -> tuple[float, float]:
    lat_wgs = lat - 0.00010695 * lat + 0.000017464 * lng + 0.0046017
    lng_wgs = lng - 0.000046038 * lat - 0.000083043 * lng + 0.010040
    return lat_wgs, lng_wgs

def is_already_wgs84(lat: float, lng: float) -> bool:
    """
    変換済みかどうかを簡易判定。
    Tokyo Datum と WGS84 の差は緯度で約+0.004度、経度で約+0.010度。
    すでに変換済みなら再変換すると余計にズレるので、
    変換後の緯度が変換前より大きい（北にある）ことで判定する。
    ただし確実な判定は難しいので、バックアップを取ったうえで実行すること。
    """
    lat_wgs, _ = tokyo_to_wgs84(lat, lng)
    # 変換すると緯度が約+0.004上がる。すでに変換済みなら差が小さい
    # Tokyo Datum の変換差は常に正（北方向）なので、
    # 「変換後の差が期待値と大きくかけ離れている」ときはスキップ
    diff = lat_wgs - lat
    return not (0.003 < diff < 0.006)  # 差が0.003〜0.006の範囲外なら変換済みとみなす

# ============================================================
# メイン処理
# ============================================================
def convert_file(path: Path, dry_run: bool) -> tuple[int, int]:
    """ファイルを変換。(変換件数, スキップ件数) を返す"""
    shops = json.loads(path.read_text(encoding="utf-8"))
    converted = skipped = 0

    for shop in shops:
        coords = shop.get("coords")
        if not coords or len(coords) < 2:
            skipped += 1
            continue

        lat, lng = coords[0], coords[1]

        # 座標が日本の範囲外なら異常値としてスキップ
        if not (24.0 <= lat <= 46.0 and 122.0 <= lng <= 154.0):
            print(f"  [SKIP] 範囲外座標: {shop.get('name','?')} ({lat}, {lng})")
            skipped += 1
            continue

        # すでに変換済みと判定される場合はスキップ
        if is_already_wgs84(lat, lng):
            skipped += 1
            continue

        lat_new, lng_new = tokyo_to_wgs84(lat, lng)
        if dry_run:
            print(f"  [DRY] {shop.get('name','?')}: ({lat:.6f}, {lng:.6f}) → ({lat_new:.6f}, {lng_new:.6f})")
        else:
            shop["coords"] = [lat_new, lng_new]
        converted += 1

    if not dry_run and converted > 0:
        path.write_text(json.dumps(shops, ensure_ascii=False, separators=(",",":")), encoding="utf-8")

    return converted, skipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="変換結果を表示するだけ（ファイル更新なし）")
    args = parser.parse_args()

    if not DATA_DIR.exists():
        print(f"ERROR: {DATA_DIR} が見つかりません。スクリプトをプロジェクトルートに置いてください。")
        return

    # 対象ファイルを収集
    pref_files = sorted(DATA_DIR.glob("shops_??.json"))
    summary_file = DATA_DIR / "all_summary.json"

    targets = pref_files + ([summary_file] if summary_file.exists() else [])
    if not targets:
        print("対象ファイルが見つかりません。")
        return

    print(f"対象: {len(targets)} ファイル")

    # バックアップ
    if not args.dry_run:
        backup_dir = DATA_DIR / "_backup_before_wgs84"
        backup_dir.mkdir(exist_ok=True)
        for f in targets:
            dest = backup_dir / f.name
            if not dest.exists():  # 既存バックアップは上書きしない
                shutil.copy(f, dest)
        print(f"バックアップ保存先: {backup_dir}")

    # 変換実行
    total_converted = total_skipped = 0
    for path in targets:
        c, s = convert_file(path, args.dry_run)
        total_converted += c
        total_skipped   += s
        status = "DRY" if args.dry_run else "OK"
        print(f"  [{status}] {path.name}: 変換 {c}件 / スキップ {s}件")

    print(f"\n完了: 変換 {total_converted}件 / スキップ {total_skipped}件")
    if not args.dry_run and total_converted > 0:
        print("※ 元データのバックアップは data/_backup_before_wgs84/ にあります。")


if __name__ == "__main__":
    main()
