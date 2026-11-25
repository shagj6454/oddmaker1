"""
v16.2 自動ファインチューニング・エンジン (auto_tune.py)
data/raw_cache_*.json の実データを用い、9連敗回避を最優先制約として
T1〜T20 のパラメータ（T20最大点数・大穴オッズ帯・EV重視指数・EV足切り・近接度指数）を
自動探索し、最適値を data/tuned_params.json へ保存します。
"""
import argparse
import itertools
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from core_v12 import (
    DATA_DIR,
    MODEL_VERSION,
    evaluate_v12_standard,
    run_progression_sim,
    sanitize_race_record,
)
from engine.master_data import TUNED_PARAMS_PATH

JST = timezone(timedelta(hours=9))


def load_all_jra_races(max_files: int = 12) -> List[Dict]:
    files = sorted(DATA_DIR.glob("raw_cache_jra_*.json"), reverse=True)[:max_files]
    files.reverse()
    races: List[Dict] = []
    for f in files:
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                races.extend(sanitize_race_record(r, recompute_waku_scores=True) for r in raw if isinstance(r, dict))
        except Exception:
            continue
    return races


def score_candidate(sim_res: Dict) -> float:
    """9連敗・損切りを強くペナルティしつつ回収率を最大化する目的関数"""
    st = sim_res["daily_stats"]
    roi = float(st.get("recovery_rate") or 0.0)
    max_lose = int(st.get("max_losing_streak") or 0)
    sl = int(st.get("stop_loss_count") or 0)
    penalty = sl * 80.0 + max(0, max_lose - 8) * 25.0
    return round(roi - penalty, 3)


def main() -> None:
    p = argparse.ArgumentParser(description=f"{MODEL_VERSION} 実キャッシュ自動ファインチューニング")
    p.add_argument("--max-files", type=int, default=12, help="使用する最新JRAキャッシュ月数")
    args = p.parse_args()

    races = load_all_jra_races(max_files=args.max_files)
    if len(races) < 30:
        print(f"ℹ️ キャッシュ不足({len(races)}R)のためファインチューニングをスキップします。", flush=True)
        return

    grid = {
        "t20_max_picks": [12, 16, 18],
        "t20_ev_cutoff": [0.25, 0.32, 0.42],
        "t20_ev_exp":    [0.70, 0.85],
        "proximity_pow": [0.25, 0.35, 0.50],
    }
    keys = list(grid.keys())
    combos = list(itertools.product(*(grid[k] for k in keys)))
    print(f"🔧 [{MODEL_VERSION} Auto-Tune] 対象={len(races)}R / 探索候補={len(combos)}通り", flush=True)

    best_score = -1e9
    best_params: Dict = {}
    best_stats: Dict = {}

    for vals in combos:
        cand = dict(zip(keys, vals))
        cand.update({"t20_min_odds": 10.0, "t20_max_odds": 100.0, "base_ev_exp": 0.45, "base_max_picks": 10, "base_ev_cutoff": 0.58})
        TUNED_PARAMS_PATH.write_text(json.dumps(cand, ensure_ascii=False, indent=2), encoding="utf-8")
        res = run_progression_sim(
            races,
            lambda r, si, tr: evaluate_v12_standard(r, "枠連", stage_idx=si, live_tracker=tr),
            log_skips=False,
        )
        sc = score_candidate(res)
        if sc > best_score:
            best_score = sc
            best_params = dict(cand)
            best_stats = dict(res["daily_stats"])
            print(
                f"  ★ 新記録 score={sc:.2f} | ROI={best_stats['recovery_rate']}% "
                f"最大{best_stats['max_losing_streak']}連敗 SL={best_stats['stop_loss_count']} | {vals}",
                flush=True,
            )

    best_params["tuned_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    best_params["races_used"] = len(races)
    best_params["best_score"] = best_score
    best_params["best_roi"] = best_stats.get("recovery_rate", 0.0)
    best_params["best_max_losing_streak"] = best_stats.get("max_losing_streak", 0)
    TUNED_PARAMS_PATH.write_text(json.dumps(best_params, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ ファインチューニング完了 -> {TUNED_PARAMS_PATH}", flush=True)


if __name__ == "__main__":
    main()
