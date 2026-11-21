import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List
from playwright.sync_api import sync_playwright

from core_v12 import (
    DATA_DIR,
    build_100_strategies,
    evaluate_v12_standard,
    run_progression_sim,
    save_dry_run_dashboard,
)
from jra_netkeiba_bettor import (
    fetch_jra_result_wakuren,
    fetch_netkeiba_race_and_odds,
    fetch_today_jra_race_ids,
)

JST = timezone(timedelta(hours=9))


def main():
    p = argparse.ArgumentParser(description="v12.3 JRA シミュレーター（netkeiba馬柱＋API完結・購入なし）")
    p.add_argument("--date", type=str, default="", help="対象日 YYYYMMDD（空なら当日）")
    p.add_argument("--mode", type=str, default="v12", choices=["v12", "grid100"])
    p.add_argument("--run-id", type=str, default="")
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    now_jst = datetime.now(JST)
    ymd = args.date.strip() or now_jst.strftime("%Y%m%d")
    run_id = args.run_id.strip() or f"jra_v12_3_{ymd}_{now_jst.strftime('%H%M%S')}"
    cache_path = DATA_DIR / f"raw_cache_jra_{ymd}.json"

    races: List[Dict] = []
    used_cache = False
    if (not args.no_cache) and cache_path.exists():
        try:
            loaded = json.load(open(cache_path, "r", encoding="utf-8"))
            # v12.3拡張フィールド(fav1_waku/waku_leg_scores)が揃っている場合のみキャッシュ採用
            if loaded and ("fav1_waku" in loaded[0]) and ("waku_leg_scores" in loaded[0]):
                races = loaded
                used_cache = True
        except Exception:
            races = []

    if not used_cache:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            race_ids = fetch_today_jra_race_ids(page, ymd)
            print(f"📅 [v12.3 JRA Sim] {ymd} 検出: {len(race_ids)}R", flush=True)
            for rid in race_ids:
                meta, combos = fetch_netkeiba_race_and_odds(page, rid, bet_type="枠連")
                wf_pair = fetch_jra_result_wakuren(page, rid)
                races.append({
                    "timestamp": f"{ymd[4:6]}/{ymd[6:8]} {meta.post_time or '00:00'}",
                    "date_key": ymd, "venue": meta.venue, "race_num": meta.race_num,
                    "race_name": meta.race_name, "race_class": meta.race_class,
                    "horse_count": meta.horse_count, "surface": meta.surface,
                    "track_condition": meta.track_condition, "has_scratch": meta.has_scratch,
                    "fav1_win_odds": meta.fav1_win_odds, "fav1_waku": meta.fav1_waku,
                    "fav1_leg_type": meta.fav1_leg_type, "horses_info": meta.horses_info,
                    "waku_leg_scores": meta.waku_leg_scores,
                    "odds_kind": "wakuren", "real_odds": combos, "wf_pair": wf_pair,
                })
            browser.close()
        races.sort(key=lambda x: x["timestamp"])
        if races:
            json.dump(races, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)

    if not races:
        print(f"ℹ️ {ymd} はJRA開催なしのため終了します。", flush=True)
        sys.exit(0)

    src = "🟢 JRAキャッシュ読込(v12.3)" if used_cache else "netkeiba 馬柱＋API (JRA Dry-run)"
    if args.mode == "grid100":
        results = [(name, run_progression_sim(races, fn)) for name, fn in build_100_strategies("枠連")]
        results.sort(key=lambda x: (x[1]["daily_stats"]["recovery_rate"], x[1]["daily_stats"]["net_profit"]), reverse=True)
        best_name, best_res = results[0]
        top20 = {
            f"TOP{i+1}: {nm}": {"races": rs["races"], "hits": rs["hits"], "bet": rs["daily_stats"]["total_bet"], "payout": rs["daily_stats"]["total_payout"]}
            for i, (nm, rs) in enumerate(results[:20])
        }
        save_dry_run_dashboard(
            run_id, int(ymd[:4]), ymd[4:6], "JRA 100種", "JRA",
            f"v12.3 JRA ({ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}) 100種1位 [{best_name}]",
            f"{src}＋100種総当たり", len(races), best_res, override_prog_stats=top20,
        )
        print(f"🏆 [v12.3 JRA 100種1位] {best_name} / 回収率 {best_res['daily_stats']['recovery_rate']}%", flush=True)
    else:
        sim_res = run_progression_sim(races, lambda r: evaluate_v12_standard(r, "枠連"), log_skips=True)
        save_dry_run_dashboard(
            run_id, int(ymd[:4]), ymd[4:6], "JRA v12.3", "JRA",
            f"v12.3 JRA ({ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}) 脚質・馬場重み検証 [購入なし]",
            src, len(races), sim_res,
        )
        st = sim_res["daily_stats"]
        print(f"🧪 [v12.3 JRA 完了] 投資{st['total_bet']:,}円 / 払戻{st['total_payout']:,}円 (回収率{st['recovery_rate']}%)", flush=True)


if __name__ == "__main__":
    main()
