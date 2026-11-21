import argparse
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from playwright.sync_api import Page, sync_playwright

from core_v12 import (
    EXCLUDED_VENUES,
    STAGES,
    allocate_bets,
    calc_synth,
    load_prod_state,
    save_prod_state,
)
from jra_netkeiba_bettor import (
    BetOrder,
    RaceMeta,
    execute_ipat_bet,
    fetch_netkeiba_race_and_odds,
    fetch_today_jra_race_ids,
    login_ipat,
)
from nar_rakuten_bettor import (
    NarBetOrder,
    NarRaceMeta,
    execute_rakuten_bet,
    fetch_nar_odds,
    fetch_nar_tan_fav1,
    fetch_today_nar_races,
    login_rakuten_keiba,
)

JST = timezone(timedelta(hours=9))


def is_in_bet_window(post_time: str, now_jst: datetime, ignore_time: bool = False) -> bool:
    if ignore_time:
        return True
    m = re.search(r"(\d{1,2}):(\d{2})", post_time or "")
    if not m:
        return False
    post_dt = now_jst.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    diff_min = (post_dt - now_jst).total_seconds() / 60.0
    return 2.0 <= diff_min <= 6.0


def _build_jra_order(page: Page, meta: RaceMeta, fav1: float, stage_idx: int) -> Tuple[Optional[BetOrder], str]:
    if any(ex in meta.venue for ex in EXCLUDED_VENUES) or ("障害" in meta.race_name):
        return None, "除外競馬場/障害戦(見送り)"
    if meta.has_scratch:
        return None, "取消/除外馬あり(見送り)"
    if stage_idx == 7 and meta.race_num <= 8:
        return None, "8戦目は9R以降推奨のため見送り"

    if fav1 <= 1.9:
        bet_type = "馬単"
        _, combos, _ = fetch_netkeiba_race_and_odds(page, meta.race_id, bet_type="馬単")
        picked = [c for c in combos if 2 <= c["pop"] <= 4]
    else:
        _, combos, _ = fetch_netkeiba_race_and_odds(page, meta.race_id, bet_type="枠連")
        if not combos:
            bet_type = "馬単"
            _, combos, _ = fetch_netkeiba_race_and_odds(page, meta.race_id, bet_type="馬単")
            picked = [c for c in combos if 2 <= c["pop"] <= 5]
        else:
            bet_type = "枠連"
            non_zoro = [c for c in combos if c["w1"] != c["w2"]]
            picked = [c for c in non_zoro if c["pop"] >= (2 if meta.race_num <= 8 else 3)][: (5 if meta.race_num <= 8 else 6)]

    synth = calc_synth([c["odds"] for c in picked])
    if not picked or synth < 1.80:
        return None, f"{bet_type} 実合成{synth}倍<1.80倍(見送り)"

    st = STAGES[stage_idx]
    budget = st["late_bet"] if meta.race_num >= 9 else st["early_bet"]
    tickets = allocate_bets(picked, budget)
    return BetOrder(meta=meta, bet_type=bet_type, tickets=tickets), f"{bet_type} {len(tickets)}点(合成{synth}倍)"


def _build_nar_order(page: Page, meta: NarRaceMeta, fav1: float, stage_idx: int) -> Tuple[Optional[NarBetOrder], str]:
    if any(ex in meta.venue for ex in EXCLUDED_VENUES):
        return None, "除外競馬場(見送り)"
    if meta.has_scratch:
        return None, "取消/除外馬あり(見送り)"
    if stage_idx == 7 and meta.race_num <= 8:
        return None, "8戦目は9R以降推奨のため見送り"

    if fav1 <= 1.9:
        combos = fetch_nar_odds(page, meta.race_id, "wakutan")
        bet_type = "枠単" if combos else "馬単"
        if not combos:
            combos = fetch_nar_odds(page, meta.race_id, "umatan")
        picked = [c for c in combos if 2 <= c["pop"] <= 4]
    else:
        combos = fetch_nar_odds(page, meta.race_id, "wakufuku")
        if not combos:
            combos = fetch_nar_odds(page, meta.race_id, "wakutan")
            bet_type = "枠単" if combos else "馬単"
            if not combos:
                combos = fetch_nar_odds(page, meta.race_id, "umatan")
            picked = [c for c in combos if 2 <= c["pop"] <= 5]
        else:
            bet_type = "枠複"
            non_zoro = [c for c in combos if c["w1"] != c["w2"]]
            picked = [c for c in non_zoro if c["pop"] >= (2 if meta.race_num <= 8 else 3)][: (5 if meta.race_num <= 8 else 6)]

    synth = calc_synth([c["odds"] for c in picked])
    if not picked or synth < 1.80:
        return None, f"{bet_type} 実合成{synth}倍<1.80倍(見送り)"

    st = STAGES[stage_idx]
    budget = st["late_bet"] if meta.race_num >= 9 else st["early_bet"]
    tickets = allocate_bets(picked, budget)
    return NarBetOrder(meta=meta, bet_type=bet_type, tickets=tickets), f"{bet_type} {len(tickets)}点(合成{synth}倍)"


def main():
    p = argparse.ArgumentParser(description="v12 本番自動投票（JRA即PAT & 地方楽天競馬 統合）")
    p.add_argument("--target", type=str, default="all", choices=["all", "jra", "nar"])
    p.add_argument("--dry-run", action="store_true", help="購入直前で停止する安全モード")
    p.add_argument("--ignore-time", action="store_true", help="テスト用：時刻窓を無視して直近1Rのみ検証")
    args = p.parse_args()

    now_jst = datetime.now(JST)
    ymd = now_jst.strftime("%Y%m%d")
    state = load_prod_state()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        # --- JRA ---
        if args.target in ("all", "jra"):
            rids = fetch_today_jra_race_ids(page, ymd)
            print(f"🏇 [v12 JRA] {ymd} 検出: {len(rids)}R", flush=True)
            jra_logged_in = False
            for rid in rids:
                meta, _, fav1 = fetch_netkeiba_race_and_odds(page, rid, bet_type="枠連")
                if not is_in_bet_window(meta.post_time, now_jst, args.ignore_time):
                    continue
                order, summary = _build_jra_order(page, meta, fav1, state["stage_idx"])
                if not order:
                    print(f"⏭️ [JRA見送り] {meta.venue}{meta.race_num}R: {summary}", flush=True)
                    continue
                if not jra_logged_in:
                    login_ipat(page)
                    jra_logged_in = True
                if execute_ipat_bet(page, order, dry_run=args.dry_run):
                    state["race_logs"].insert(0, {
                        "timestamp": now_jst.strftime("%m/%d %H:%M"),
                        "race_title": f"{meta.venue} {meta.race_num}R",
                        "bet_type": order.bet_type,
                        "stage_name": STAGES[state["stage_idx"]]["name"],
                        "combos_summary": summary,
                        "status": "SKIP" if args.dry_run else "PENDING",
                        "total_bet": 0 if args.dry_run else order.total_amount,
                        "payout": 0, "result_pair": "-",
                    })
                    save_prod_state(state)
                if args.ignore_time:
                    break

        # --- NAR ---
        if args.target in ("all", "nar"):
            metas = fetch_today_nar_races(page, ymd)
            print(f"🏇 [v12 NAR] {ymd} 検出: {len(metas)}R", flush=True)
            nar_logged_in = False
            for meta in metas:
                if not is_in_bet_window(meta.post_time, now_jst, args.ignore_time):
                    continue
                fav1 = fetch_nar_tan_fav1(page, meta.race_id)
                order, summary = _build_nar_order(page, meta, fav1, state["stage_idx"])
                if not order:
                    print(f"⏭️ [NAR見送り] {meta.venue}{meta.race_num}R: {summary}", flush=True)
                    continue
                if not nar_logged_in and not args.dry_run:
                    login_rakuten_keiba(page)
                    nar_logged_in = True
                if execute_rakuten_bet(page, order, dry_run=args.dry_run):
                    state["race_logs"].insert(0, {
                        "timestamp": now_jst.strftime("%m/%d %H:%M"),
                        "race_title": f"{meta.venue} {meta.race_num}R",
                        "bet_type": order.bet_type,
                        "stage_name": STAGES[state["stage_idx"]]["name"],
                        "combos_summary": summary,
                        "status": "SKIP" if args.dry_run else "PENDING",
                        "total_bet": 0 if args.dry_run else order.total_amount,
                        "payout": 0, "result_pair": "-",
                    })
                    save_prod_state(state)
                if args.ignore_time:
                    break

        browser.close()


if __name__ == "__main__":
    main()
