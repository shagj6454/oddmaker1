import argparse
import calendar
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from core_v12 import (
    DATA_DIR,
    calc_waku_leg_scores,
    build_100_strategies,
    evaluate_v12_standard,
    run_progression_sim,
    save_dry_run_dashboard,
)
from jra_netkeiba_bettor import (
    fetch_jra_result_info,
    fetch_netkeiba_race_and_odds,
    fetch_today_jra_race_ids,
)

JST = timezone(timedelta(hours=9))


def get_weekend_dates(year: int, month: int, max_date: datetime) -> List[str]:
    """指定年月の土曜日(5)・日曜日(6)のうち、max_date以前のYYYYMMDD一覧を返す"""
    last_day = calendar.monthrange(year, month)[1]
    dates: List[str] = []
    for d in range(1, last_day + 1):
        dt = datetime(year, month, d, tzinfo=JST)
        if dt.weekday() in (5, 6) and dt.date() <= max_date.date():
            dates.append(dt.strftime("%Y%m%d"))
    return dates


def _fetch_one_jra_day(page, ymd: str) -> List[Dict]:
    races: List[Dict] = []
    try:
        race_ids = fetch_today_jra_race_ids(page, ymd)
        if not race_ids:
            return []
        for rid in race_ids:
            meta, combos = fetch_netkeiba_race_and_odds(page, rid, bet_type="枠連")
            wf_pair, res_cond = fetch_jra_result_info(page, rid, meta.surface)
            if res_cond:
                meta.track_condition = res_cond
                meta.waku_leg_scores = calc_waku_leg_scores(
                    meta.horses_info, surface=meta.surface, track_condition=meta.track_condition,
                    venue=meta.venue, distance=meta.distance,
                )
            races.append({
                "timestamp": f"{ymd[4:6]}/{ymd[6:8]} {meta.post_time or '00:00'}",
                "date_key": ymd, "venue": meta.venue, "race_num": meta.race_num,
                "race_name": meta.race_name, "race_class": meta.race_class,
                "horse_count": meta.horse_count, "surface": meta.surface, "distance": meta.distance,
                "track_condition": meta.track_condition, "has_scratch": meta.has_scratch,
                "fav1_win_odds": meta.fav1_win_odds, "fav2_win_odds": meta.fav2_win_odds, "fav1_umaban": meta.fav1_umaban, "fav1_waku": meta.fav1_waku,
                "fav1_leg_type": meta.fav1_leg_type, "horses_info": meta.horses_info,
                "waku_leg_scores": meta.waku_leg_scores,
                "odds_kind": "wakuren", "real_odds": combos, "wf_pair": wf_pair,
            })
        races.sort(key=lambda x: x["timestamp"])
    except Exception as e:
        print(f"⚠️ [JRA {ymd}] 取得例外: {e}", flush=True)
    return races


def _worker_fetch_jra(wid: int, ymds: List[str]) -> Dict[str, List[Dict]]:
    from playwright.sync_api import sync_playwright
    out: Dict[str, List[Dict]] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        try:
            for ymd in ymds:
                dr = _fetch_one_jra_day(page, ymd)
                out[ymd] = dr
                print(f"📅 [JRA W{wid}] {ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}: {len(dr)}R", flush=True)
        finally:
            browser.close()
    return out


def main():
    p = argparse.ArgumentParser(description="v13.1 JRA 月次(土日) 3並列シミュレーター（netkeiba馬柱＋API完結）")
    p.add_argument("--year", type=int, default=2026, help="対象年 (例: 2026)")
    p.add_argument("--month", type=int, default=11, help="対象月 (1-12)")
    p.add_argument("--date", type=str, default="", help="単日指定 YYYYMMDD（指定時は単日モード）")
    p.add_argument("--mode", type=str, default="v12", choices=["v12", "grid100"])
    p.add_argument("--run-id", type=str, default="")
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    now_jst = datetime.now(JST)
    single_ymd = args.date.strip()

    if single_ymd:
        target_ymds = [single_ymd]
        year, month_int = int(single_ymd[:4]), int(single_ymd[4:6])
        month_str = f"{month_int:02d}"
        cache_path = DATA_DIR / f"raw_cache_jra_{single_ymd}.json"
        period_label = f"{year}/{month_str}/{single_ymd[6:8]}"
        week_label = "JRA 単日"
    else:
        year = max(2012, min(2026, args.year))
        month_int = max(1, min(12, args.month))
        month_str = f"{month_int:02d}"
        target_ymds = get_weekend_dates(year, month_int, now_jst)
        cache_path = DATA_DIR / f"raw_cache_jra_{year}_{month_str}.json"
        period_label = f"{year}年{month_int}月(土日)"
        week_label = "JRA 月次(土日)"

    if not target_ymds:
        print(f"ℹ️ {period_label} は対象となる土日開催日がありません。", flush=True)
        sys.exit(0)

    run_id = args.run_id.strip() or f"jra_v13_1_{year}{month_str}_{now_jst.strftime('%H%M%S')}"

    # 既存の月次キャッシュ＋日別キャッシュの両方から v13.1(distance付) データを読み込み
    cached_by_date: Dict[str, List[Dict]] = {}
    if not args.no_cache:
        if cache_path.exists():
            try:
                for r in json.load(open(cache_path, "r", encoding="utf-8")):
                    if r.get("date_key") and ("fav1_waku" in r) and ("distance" in r):
                        cached_by_date.setdefault(r["date_key"], []).append(r)
            except Exception:
                cached_by_date = {}
        # 単日キャッシュが既にあれば流用
        for ymd in target_ymds:
            if ymd not in cached_by_date:
                day_cp = DATA_DIR / f"raw_cache_jra_{ymd}.json"
                if day_cp.exists():
                    try:
                        loaded = json.load(open(day_cp, "r", encoding="utf-8"))
                        if loaded and ("fav1_waku" in loaded[0]) and ("distance" in loaded[0]):
                            cached_by_date[ymd] = loaded
                    except Exception:
                        pass

    missing = [d for d in target_ymds if args.no_cache or (d not in cached_by_date)]
    used_cache = len(missing) == 0

    if not used_cache:
        n_workers = min(3, len(missing))
        chunks = [missing[i::n_workers] for i in range(n_workers)]
        print(f"🚀 [v13.1 JRA Sim] {period_label}: 未取得 {len(missing)}日分を {n_workers}並列で取得開始", flush=True)
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(_worker_fetch_jra, i + 1, ch) for i, ch in enumerate(chunks) if ch]
            for fut in as_completed(futs):
                cached_by_date.update(fut.result())

        merged = [r for dk in sorted(cached_by_date.keys()) if dk in target_ymds for r in cached_by_date[dk]]
        if merged:
            json.dump(merged, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)

    all_races = [r for dk in sorted(cached_by_date.keys()) if dk in target_ymds for r in cached_by_date[dk]]
    if not all_races:
        print(f"ℹ️ {period_label} はJRA開催なしのため終了します。", flush=True)
        sys.exit(0)

    src = "🟢 JRAキャッシュ読込(v13.1)" if used_cache else "netkeiba 馬柱＋API (JRA 3並列 Dry-run)"
    if args.mode == "grid100":
        results = [(name, run_progression_sim(all_races, fn)) for name, fn in build_100_strategies("枠連")]
        results.sort(key=lambda x: (x[1]["daily_stats"]["recovery_rate"], x[1]["daily_stats"]["net_profit"]), reverse=True)
        best_name, best_res = results[0]
        top20 = {
            f"TOP{i+1}: {nm}": {"races": rs["races"], "hits": rs["hits"], "bet": rs["daily_stats"]["total_bet"], "payout": rs["daily_stats"]["total_payout"]}
            for i, (nm, rs) in enumerate(results[:20])
        }
        save_dry_run_dashboard(
            run_id, year, month_str, week_label, "JRA",
            f"v13.1 JRA {period_label} 100種1位 [{best_name}]",
            f"{src}＋100種総当たり", len(all_races), best_res, override_prog_stats=top20,
        )
        print(f"🏆 [v13.1 JRA 100種1位] {best_name} / 回収率 {best_res['daily_stats']['recovery_rate']}%", flush=True)
    else:
        sim_res = run_progression_sim(all_races, lambda r: evaluate_v12_standard(r, "枠連"), log_skips=True)
        save_dry_run_dashboard(
            run_id, year, month_str, week_label, "JRA",
            f"v13.1 JRA {period_label} 10段階ロジット(T1-T10)検証 [購入なし]",
            src, len(all_races), sim_res,
        )
        st = sim_res["daily_stats"]
        print(f"🧪 [v13.1 JRA {period_label} 完了] {len(all_races)}R / 投資{st['total_bet']:,}円 / 払戻{st['total_payout']:,}円 (回収率{st['recovery_rate']}%)", flush=True)


if __name__ == "__main__":
    main()
