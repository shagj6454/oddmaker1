"""
v15.5 既存JRAローデータキャッシュ一括修復スクリプト
使い方:
  python fix_jra_cache.py              # data/raw_cache_jra_*.json を自動検出して上書き修復
  python fix_jra_cache.py path/to.json # 指定JSONファイルを修復
"""
import json
import sys
from pathlib import Path

from core_v12 import DATA_DIR, sanitize_race_record, update_cache_index


def fix_file(path: Path) -> int:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return 0
    fixed = [sanitize_race_record(r, recompute_waku_scores=True) for r in raw]
    path.write_text(json.dumps(fixed, ensure_ascii=False), encoding="utf-8")
    return len(fixed)


def main():
    targets = [Path(a) for a in sys.argv[1:]] if len(sys.argv) > 1 else sorted(DATA_DIR.glob("raw_cache_jra_*.json"))
    if not targets:
        print("ℹ️ 対象の JRA キャッシュファイルが見つかりません。")
        return
    for p in targets:
        if not p.exists():
            print(f"⚠️ 未検出: {p}")
            continue
        cnt = fix_file(p)
        print(f"✅ 修復完了: {p} ({cnt}R)")
    update_cache_index()


if __name__ == "__main__":
    main()
