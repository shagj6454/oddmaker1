"""
v14.10 ダッシュボード自動再構築スクリプト（Playwright不要・高速再計算）
data/ 配下の raw_cache_*.json をスキャンし、最新エンジンでシミュレーションを実行して
data/wakuren_dry_run_state_v11_9.json / data/cache_index.json / data/CACHE_LIST.md を更新します。
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core_v12 import (
    DATA_DIR,
    DRY_RUN_STATE_PATH,
    MAX_RUN_HISTORY,
    MODEL_VERSION,
    evaluate_v12_standard,
    get_model_config,
    run_progression_sim,
    sanitize_race_record,
    save_dry_run_dashboard,
    update_cache_index,
)

JST = timezone(timedelta(hours=9))


def _parse_cache_meta(p: Path) -> Optional[Dict]:
    name = p.name
    m_jra_m = re.match(r"^raw_cache_jra_(\d{4})_(\d{2})\.json$", name)
    if m_jra_m:
        yr, mo = int(m_jra_m.group(1)), m_jra_m.group(2)
        return {
            "kind": "JRA", "bet_label": "枠連", "year": yr, "month": mo,
            "week_label": "JRA 月次(土日)", "venue": "JRA",
            "period_label": f"{yr}年{int(mo)}月(土日)",
            "run_id": f"jra_{MODEL_VERSION.replace('.', '_')}_{yr}_{mo}",
            "sort_key": (yr, int(mo), 0, name),
        }
    m_jra_d = re.match(r"^raw_cache_jra_(\d{8})\.json$", name)
    if m_jra_d:
        ymd = m_jra_d.group(1)
        yr, mo, dy = int(ymd[:4]), ymd[4:6], ymd[6:8]
        return {
            "kind": "JRA", "bet_label": "枠連", "year": yr, "month": mo,
            "week_label": "JRA 単日", "venue": "JRA",
            "period_label": f"{yr}/{mo}/{dy}",
            "run_id": f"jra_{MODEL_VERSION.replace('.', '_')}_{ymd}",
            "sort_key": (yr, int(mo), int(dy), name),
        }
    m_nar = re.match(r"^raw_cache_(\d{4})_(all|\d{2})_(.+)\.json$", name)
    if m_nar:
        yr, mo, vn = int(m_nar.group(1)), m_nar.group(2), m_nar.group(3)
        mo_num = 13 if mo == "all" else int(mo)
        return {
            "kind": "NAR", "bet_label": "枠複", "year": yr, "month": mo,
            "week_label": "年間" if mo == "all" else "月次", "venue": vn,
            "period_label": f"{yr}年{'年間' if mo == 'all' else str(int(mo)) + '月'}",
            "run_id": f"nar_{MODEL_VERSION.replace('.', '_')}_{yr}_{mo}_{vn}",
            "sort_key": (yr, mo_num, 0, name),
        }
    return None


def _load_existing_run_ids() -> set:
    if not DRY_RUN_STATE_PATH.exists():
        return set()
    try:
        d = json.loads(DRY_RUN_STATE_PATH.read_text(encoding="utf-8"))
        hist = d.get("run_history", []) if isinstance(d, dict) else []
        return {str(h.get("run_id") or "") for h in hist if isinstance(h, dict)}
    except Exception:
        return set()


def _sync_index_only() -> None:
    entries = update_cache_index()
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    if DRY_RUN_STATE_PATH.exists():
        try:
            d = json.loads(DRY_RUN_STATE_PATH.read_text(encoding="utf-8"))
            d["cached_items"] = entries
            d["model_config"] = get_model_config()
            d["updated_at"] = now_str
            DRY_RUN_STATE_PATH.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            return
        except Exception:
            pass
    out = {
        "updated_at": now_str,
        "model_config": get_model_config(),
        "cached_items": entries,
        "run_history": [],
    }
    DRY_RUN_STATE_PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=f"{MODEL_VERSION} data/配下キャッシュからダッシュボード自動再構築")
    p.add_argument("--target-file", type=str, default="", help="特定キャッシュファイル名のみ再計算 (例: raw_cache_jra_2025_01.json)")
    p.add_argument("--year", type=int, default=0, help="対象年で絞り込み (0で全年)")
    p.add_argument("--only-missing", action="store_true", help="現バージョンの検証ログが未反映のキャッシュのみ再計算")
    p.add_argument("--limit", type=int, default=MAX_RUN_HISTORY, help=f"最大再計算件数 (既定: {MAX_RUN_HISTORY})")
    args = p.parse_args()

    entries = update_cache_index()
    target_name = Path(args.target_file.strip()).name if args.target_file.strip() else ""

    candidates: List[Tuple[Path, Dict]] = []
    for fpath in sorted(DATA_DIR.glob("raw_cache_*.json")):
        if target_name and fpath.name != target_name:
            continue
        meta = _parse_cache_meta(fpath)
        if not meta:
            continue
        if args.year and args.year > 0 and meta["year"] != args.year:
            continue
        candidates.append((fpath, meta))

    if not candidates:
        print(f"ℹ️ 対象のキャッシュが見つかりませんでした (cached={len(entries)}件)。インデックスのみ同期します。", flush=True)
        _sync_index_only()
        return

    # 新しいものから上限件数を選び、古い順に save_dry_run_dashboard へ投入することで最新が先頭に来るようにする
    candidates.sort(key=lambda x: x[1]["sort_key"], reverse=True)
    if args.limit > 0 and not target_name:
        candidates = candidates[: args.limit]
    candidates.reverse()

    existing_ids = _load_existing_run_ids() if args.only_missing else set()
    processed = 0

    for fpath, meta in candidates:
        if args.only_missing and meta["run_id"] in existing_ids:
            print(f"⏭️ スキップ(反映済): {fpath.name}", flush=True)
            continue
        try:
            raw = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ 読込失敗 {fpath.name}: {e}", flush=True)
            continue
        if not isinstance(raw, list) or not raw:
            print(f"ℹ️ 空キャッシュのためスキップ: {fpath.name}", flush=True)
            continue

        races = [sanitize_race_record(r, recompute_waku_scores=True) for r in raw if isinstance(r, dict)]
        if not races:
            continue

        bet_label = meta["bet_label"]
        sim_res = run_progression_sim(races, lambda r, bl=bet_label: evaluate_v12_standard(r, bl), log_skips=True)
        mode_lbl = (
            f"{MODEL_VERSION} JRA {meta['period_label']} 予測連対率20段階(T1-T20)検証 [キャッシュ再計算]"
            if meta["kind"] == "JRA"
            else f"{MODEL_VERSION} 地方 {meta['period_label']} ({meta['venue']}) 予測連対率20段階(T1-T20)検証 [キャッシュ再計算]"
        )
        src_lbl = f"🟢 {meta['kind']}キャッシュ自動読込({MODEL_VERSION} / {fpath.name})"
        save_dry_run_dashboard(
            meta["run_id"], meta["year"], meta["month"], meta["week_label"], meta["venue"],
            mode_lbl, src_lbl, len(races), sim_res,
        )
        st = sim_res["daily_stats"]
        print(
            f"✅ [{meta['kind']} {meta['period_label']}] {len(races)}R -> 参戦{st['played_count']}R / "
            f"投資{st['total_bet']:,}円 / 払戻{st['total_payout']:,}円 (回収率{st['recovery_rate']}% / 最大{st['max_losing_streak']}連敗)",
            flush=True,
        )
        processed += 1

    if processed == 0:
        _sync_index_only()
    print(f"🏁 ダッシュボード再構築完了: {processed}件再計算 / 全キャッシュ{len(entries)}件", flush=True)


if __name__ == "__main__":
    main()
