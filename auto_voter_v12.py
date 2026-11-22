import argparse
import re
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

from core_v12 import (
    STAGES,
    allocate_bets,
    evaluate_v12_standard,
    load_prod_state,
    save_prod_state,
)
from jra_netkeiba_bettor import (
    BetOrder,
    execute_ipat_bet,
    fetch_netkeiba_race_and_odds,
    fetch_today_jra_race_ids,
    login_ipat,
)
from nar_rakuten_bettor import (
    NarBetOrder,
    execute_rakuten_bet,
    fetch_nar_horses_leg_info,
    fetch_nar_odds,
    fetch_nar_tan_and_meta,
    fetch_today_nar_races,
    login_rakuten_keiba,
)

JST = timezone(timedelta(hours=9))


def _build_bought_tickets_log(picked, tickets):
    amt_map = dict(tickets)
    out = []
    for c in picked or []:
        p_str = c.get("pair") or f"{c['w1']}-{c['w2']}"
        out.append({
            "pair": p_str,
            "pop": int(c.get("pop") or 0),
            "odds": float(c.get("odds") or 0.0),
            "amount": amt_map.get(p_str, 100),
            "is_hit": False,
        })
    return out


def is_in_bet_window(post_time: str, now_jst: datetime, ignore_time: bool = False) -> bool:
    if ignore_time:
        return True
    m = re.search(r"(\d{1,2}):(\d{2})", post_time or "")
    if not m:
        return False
    post_dt = now_jst.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    diff_min = (post_dt - now_jst).total_seconds() / 60.0
    return 2.0 <= diff_min <= 6.0


def main():
    p = argparse.ArgumentParser(description="v13.8 本番自動投票（JRA即PAT & 地方楽天競馬 統合）")
    p.add_argument("--target", type=str, default="all", choices=["all", "jra", "nar"])
    p.add_argument("--dry-run", action="store_true", help="購入直前で停止する安全モード")
    p.add_argument("--ignore-time", action="store_true", help="テスト用：発走時刻窓を無視して直近1Rのみ検証")
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
            print(f"🏇 [v13.8 JRA] {ymd} 検出: {len(rids)}R", flush=True)
            jra_logged_in = False
            for rid in rids:
                meta, combos = fetch_netkeiba_race_and_odds(page, rid, bet_type="枠連")
                if not is_in_bet_window(meta.post_time, now_jst, args.ignore_time):
                    continue
                if state["stage_idx"] == 9 and meta.race_num <= 8:
                    print(f"⏭️ [JRA見送り] {meta.venue}{meta.race_num}R: 10戦目は9R以降推奨", flush=True)
                    continue

                race_dict = {
                    "venue": meta.venue, "race_num": meta.race_num, "race_name": meta.race_name,
                    "race_class": meta.race_class, "horse_count": meta.horse_count,
                    "surface": meta.surface, "distance": meta.distance, "track_condition": meta.track_condition,
                    "has_scratch": meta.has_scratch, "fav1_win_odds": meta.fav1_win_odds, "fav2_win_odds": meta.fav2_win_odds,
                    "fav1_umaban": meta.fav1_umaban, "fav1_waku": meta.fav1_waku, "fav1_leg_type": meta.fav1_leg_type,
                    "horses_info": meta.horses_info, "waku_leg_scores": meta.waku_leg_scores,
                    "real_odds": combos,
                }
                ev = evaluate_v12_standard(race_dict, bet_label="枠連")
                if not ev["participate"]:
                    print(f"⏭️ [JRA見送り] {meta.venue}{meta.race_num}R: {ev['skip_reason']}", flush=True)
                    continue

                st = STAGES[state["stage_idx"]]
                budget = st["late_bet"] if meta.race_num >= 9 else st["early_bet"]
                tickets = allocate_bets(ev["picked"], budget)
                order = BetOrder(meta=meta, bet_type="枠連", tickets=tickets)
                print(f"🎯 [JRA参戦] {meta.venue}{meta.race_num}R ({st['name']}): {ev['combos_summary']} / {order.total_amount:,}円", flush=True)

                if not jra_logged_in:
                    login_ipat(page)
                    jra_logged_in = True
                if execute_ipat_bet(page, order, dry_run=args.dry_run):
                    state["race_logs"].insert(0, {
                        "timestamp": now_jst.strftime("%m/%d %H:%M"),
                        "race_title": f"{meta.venue} {meta.race_num}R",
                        "bet_type": order.bet_type, "stage_name": st["name"],
                        "mode_name": ev.get("bucket", "v13.8"), "p_waku": ev.get("p_waku"), "tier_id": ev.get("tier_id", "-"),
                        "surface_cond": f"{meta.surface}{meta.track_condition}",
                        "combos_summary": ev["combos_summary"],
                        "synth_odds": ev.get("synth_odds", 0.0),
                        "bought_tickets": _build_bought_tickets_log(ev.get("picked"), tickets),
                        "hit_ticket": None,
                        "status": "SKIP" if args.dry_run else "PENDING",
                        "total_bet": 0 if args.dry_run else order.total_amount,
                        "payout": 0, "result_pair": "-", "result_pop": None, "result_odds": None,
                    })
                    save_prod_state(state)
                if args.ignore_time:
                    break

        # --- NAR ---
        if args.target in ("all", "nar"):
            metas = fetch_today_nar_races(page, ymd)
            print(f"🏇 [v13.8 NAR] {ymd} 検出: {len(metas)}R", flush=True)
            nar_logged_in = False
            for meta in metas:
                if not is_in_bet_window(meta.post_time, now_jst, args.ignore_time):
                    continue
                if state["stage_idx"] == 9 and meta.race_num <= 8:
                    print(f"⏭️ [NAR見送り] {meta.venue}{meta.race_num}R: 10戦目は9R以降推奨", flush=True)
                    continue

                tm = fetch_nar_tan_and_meta(page, meta.race_id)
                horses_info, waku_scores, final_cond, final_dist = fetch_nar_horses_leg_info(
                    page, meta.race_id, meta.venue, tm["surface"], tm["track_condition"],
                    win_odds_map=tm.get("win_odds_map"), distance=tm.get("distance", 0),
                )
                fav1_h = next((h for h in horses_info if h["umaban"] == tm["fav1_umaban"]), None)
                combos = fetch_nar_odds(page, meta.race_id, "wakufuku")

                sorted_win_odds = sorted(v for v in (tm.get("win_odds_map") or {}).values() if v >= 1.0)
                fav2_win = sorted_win_odds[1] if len(sorted_win_odds) >= 2 else 0.0
                race_dict = {
                    "venue": meta.venue, "race_num": meta.race_num,
                    "race_name": tm["race_name"] or meta.race_name, "race_class": tm["race_class"],
                    "horse_count": tm["horse_count"], "surface": tm["surface"], "distance": final_dist,
                    "track_condition": final_cond, "has_scratch": meta.has_scratch,
                    "fav1_win_odds": tm["fav1_win_odds"], "fav2_win_odds": fav2_win,
                    "fav1_umaban": tm.get("fav1_umaban", 0), "fav1_waku": tm["fav1_waku"],
                    "fav1_leg_type": fav1_h["leg_type"] if fav1_h else "不明",
                    "horses_info": horses_info, "waku_leg_scores": waku_scores,
                    "real_odds": combos,
                }
                ev = evaluate_v12_standard(race_dict, bet_label="枠複")
                if not ev["participate"]:
                    print(f"⏭️ [NAR見送り] {meta.venue}{meta.race_num}R: {ev['skip_reason']}", flush=True)
                    continue

                st = STAGES[state["stage_idx"]]
                budget = st["late_bet"] if meta.race_num >= 9 else st["early_bet"]
                tickets = allocate_bets(ev["picked"], budget)
                order = NarBetOrder(meta=meta, bet_type="枠複", tickets=tickets)
                print(f"🎯 [NAR参戦] {meta.venue}{meta.race_num}R ({st['name']}): {ev['combos_summary']} / {order.total_amount:,}円", flush=True)

                if not nar_logged_in and not args.dry_run:
                    login_rakuten_keiba(page)
                    nar_logged_in = True
                if execute_rakuten_bet(page, order, dry_run=args.dry_run):
                    state["race_logs"].insert(0, {
                        "timestamp": now_jst.strftime("%m/%d %H:%M"),
                        "race_title": f"{meta.venue} {meta.race_num}R",
                        "bet_type": order.bet_type, "stage_name": st["name"],
                        "mode_name": ev.get("bucket", "v13.8"), "p_waku": ev.get("p_waku"), "tier_id": ev.get("tier_id", "-"),
                        "surface_cond": f"{tm['surface']}{final_cond}",
                        "combos_summary": ev["combos_summary"],
                        "synth_odds": ev.get("synth_odds", 0.0),
                        "bought_tickets": _build_bought_tickets_log(ev.get("picked"), tickets),
                        "hit_ticket": None,
                        "status": "SKIP" if args.dry_run else "PENDING",
                        "total_bet": 0 if args.dry_run else order.total_amount,
                        "payout": 0, "result_pair": "-", "result_pop": None, "result_odds": None,
                    })
                    save_prod_state(state)
                if args.ignore_time:
                    break

        browser.close()


if __name__ == "__main__":
    main()
