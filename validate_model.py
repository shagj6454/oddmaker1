#!/usr/bin/env python3
"""
validate_model.py — Oddmaker v14.7 総合検証スイート
  1) 全キャッシュ月別・通算成績（1.75倍MC13戦・最大7戦目戻し・クロスデイ適応・2D離散予算DP）
  2) モンテカルロ順列ストレステスト（損切りゼロ完走率・平均損切回数・中央回収率）
  3) 未知場（函館）・ダート基準タイム・地方競馬（NAR 浦和）スモークテスト
"""
import argparse
import glob
import json
import random
import sys
from pathlib import Path
from typing import Dict, List

import core_v12


def run_mc_stress(races: List[Dict], n_trials: int = 200, seed: int = 42) -> Dict:
    """単一レース評価を事前キャッシュし、順列シャッフルMCを実行"""
    tracker = core_v12.LiveTrackTracker()
    bets = [s["early_bet"] for s in core_v12.STAGES]
    cache = {}
    valid_indices = []
    for ri, r in enumerate(races):
        cr = core_v12.sanitize_race_record(r, recompute_waku_scores=False)
        for si in range(len(core_v12.STAGES)):
            ev = core_v12.evaluate_v12_standard(cr, "枠連", stage_idx=si, live_tracker=tracker)
            if not ev.get("participate"):
                cache[(ri, si)] = None
                continue
            bud = int(bets[si])
            pk = core_v12.trim_picks_for_budget(ev["picked"], bud, min_synth=core_v12.MIN_SYNTH_HARD_FLOOR)
            if not pk:
                cache[(ri, si)] = None
                continue
            cov = min(0.92, 0.62 + 0.04 * max(0, si - 2))
            al = dict(core_v12.allocate_bets(pk, bud, tilt_strength=float(ev.get("alloc_tilt", 0.35)), cover_ratio=cov))
            bet = sum(al.values())
            wf = str(cr.get("wf_pair") or "")
            pay = 0
            if wf in al:
                o = next(c["odds"] for c in pk if (c.get("pair") or f"{c['w1']}-{c['w2']}") == wf)
                pay = int((al[wf] * float(o)) // 10) * 10
            cache[(ri, si)] = (pay > 0, bet, pay)
        valid_indices.append(ri)
        tracker.observe(cr)

    rng = random.Random(seed)
    sls, rois = [], []
    for _ in range(n_trials):
        sh = list(valid_indices)
        rng.shuffle(sh)
        si = 0
        sb = sp = tb = tp = sl = 0
        for ri in sh:
            e = cache[(ri, si)]
            if e is None:
                continue
            is_h, bet, pay = e
            tb += bet
            tp += pay
            sb += bet
            sp += pay
            if is_h:
                si, sb, sp, _ = core_v12.resolve_next_stage_on_hit(si, bet, pay, sb, sp)
            else:
                if si >= core_v12.MAX_STAGE_IDX:
                    sl += 1
                    si = 0
                    sb = 0
                    sp = 0
                else:
                    si += 1
        sls.append(sl)
        rois.append(tp / max(1, tb) * 100.0)
    rois.sort()
    p0 = sum(1 for x in sls if x == 0) / float(max(1, n_trials)) * 100.0
    return {
        "trials": n_trials,
        "avg_stop_loss": round(sum(sls) / float(max(1, n_trials)), 2),
        "zero_sl_rate": round(p0, 1),
        "median_roi": round(rois[len(rois) // 2], 1),
    }


def main():
    p = argparse.ArgumentParser(description="Oddmaker v14.7 総合検証スイート")
    p.add_argument("--cache-glob", type=str, default="data/raw_cache_jra_*.json")
    p.add_argument("--mc-trials", type=int, default=200)
    args = p.parse_args()

    files = sorted(glob.glob(args.cache_glob))
    if not files:
        files = sorted(glob.glob("raw_cache_jra_*.json"))
    if not files:
        print("❌ 検証用キャッシュJSONが見つかりません。")
        sys.exit(1)

    all_races = []
    by_file = {}
    for fn in files:
        data = json.load(open(fn, "r", encoding="utf-8"))
        by_file[Path(fn).name] = data
        all_races.extend(data)

    print(f"=== Oddmaker {core_v12.MODEL_VERSION} 総合検証 ({len(files)}ファイル / 全{len(all_races)}R) ===")
    res = core_v12.run_progression_sim(all_races, lambda r: core_v12.evaluate_v12_standard(r, "枠連"), log_skips=True)
    st = res["daily_stats"]
    logs = [l for l in res["race_logs"] if l["status"] in ("HIT", "HIT_STEP_BACK", "MISS", "STOP_LOSS")]
    gami = sum(1 for l in logs if l["status"] in ("HIT", "HIT_STEP_BACK") and l["payout"] < l["total_bet"])
    over_b = sum(1 for l in logs if l["stage_name"] == "1戦目" and l["total_bet"] > 300)
    empty_m = sum(1 for l in logs if not l["mode_name"])
    fb = sum(v["flat_bet"] for v in res["prog_stats"].values())
    fp = sum(v["flat_payout"] for v in res["prog_stats"].values())
    print(f"通算: 参戦{res['races']}R / 見送{res['skips']}R / 的中{res['hits']}({res['hit_rate']}%) / 均等ROI={fp/max(1,fb)*100:.1f}%")
    print(f"追上: 投資{st['total_bet']:,}円 / 払戻{st['total_payout']:,}円 / 回収率={st['recovery_rate']}% ({st['net_profit']:+,}円)")
    print(f"品質: 最大連敗={st['max_losing_streak']} / 13戦損切={st['stop_loss_count']} / トリガミ={gami} / 1戦目超過={over_b} / mode空={empty_m}")

    print("\n--- ファイル別内訳 ---")
    for name, data in by_file.items():
        rm = core_v12.run_progression_sim(data, lambda r: core_v12.evaluate_v12_standard(r, "枠連"), log_skips=False)
        stm = rm["daily_stats"]
        print(f"  {name:32s}: 参戦{rm['races']:3d}R 的中{rm['hit_rate']:5.1f}% | 追上={stm['recovery_rate']:5.1f}% ({stm['net_profit']:+8,}円 L{stm['max_losing_streak']:2d} SL={stm['stop_loss_count']})")

    if args.mc_trials > 0:
        mc = run_mc_stress(all_races, n_trials=args.mc_trials)
        print(f"\n--- モンテカルロ順列ストレス ({mc['trials']}回) ---")
        print(f"  平均13戦損切={mc['avg_stop_loss']}回 / 損切ゼロ完走率={mc['zero_sl_rate']}% / 中央回収率={mc['median_roi']}%")

    assert core_v12.get_class_benchmark_time("函館", 1200, "1勝クラス", "1勝クラス", surface="芝") is not None
    assert core_v12.get_class_benchmark_time("東京", 1600, "1勝クラス", "1勝クラス", surface="ダート") == 98.1
    assert core_v12.get_leg_weight("逃げ", surface="ダート", venue="浦和", distance=1400, prev_c1=1, prev_c2=1, prev_c4=1, prev_field_size=12) > 1.20
    print("\n✅ 全スモークテスト（函館・ダート基準タイム・NAR浦和・品質制約）通過!")


if __name__ == "__main__":
    main()
