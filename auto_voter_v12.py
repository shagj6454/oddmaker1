import argparse
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from playwright.sync_api import sync_playwright

from core_v12 import (
    MAX_CONCURRENT_LANES,
    MAX_STAGE_IDX,
    MIN_SYNTH_HARD_FLOOR,
    MODEL_VERSION,
    NAR_PROFILE,
    PROD_BREAKEVEN_MARGIN,
    STAGES,
    WITHDRAW_AMOUNT,
    WITHDRAW_THRESHOLD,
    allocate_bets,
    evaluate_v12_standard,
    load_live_tracker,
    load_prod_state,
    resolve_next_stage_on_hit,
    save_live_tracker,
    save_prod_state,
    trim_picks_for_budget,
)
from jra_netkeiba_bettor import (
    BetOrder,
    execute_ipat_bet,
    fetch_jra_result_detail,
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
    return 2.0 <= diff_min <= 6.5


def is_upcoming_within(post_time: str, now_jst: datetime, max_min: float = 10.0) -> bool:
    m = re.search(r"(\d{1,2}):(\d{2})", post_time or "")
    if not m:
        return False
    post_dt = now_jst.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    diff_min = (post_dt - now_jst).total_seconds() / 60.0
    return 2.0 <= diff_min <= max_min


def _fetch_nar_result_pair_and_pay(page, race_id: str, race_num: int) -> Tuple[Optional[str], int]:
    if not race_id or len(race_id) < 16:
        return None, 0
    try:
        url = f"https://keiba.rakuten.co.jp/race_dividend/list/RACEID/{race_id[:16]}00"
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(400)
        txt = page.inner_text("body")
        m_r = re.search(rf"■\s*{race_num}R\b", txt)
        if not m_r:
            return None, 0
        after = txt[m_r.start():]
        m_next = re.search(r"■\s*(?:\d{1,2}R|本日の発売情報)", after[4:])
        block = after[: m_next.start() + 4] if m_next else after
        m_wf = re.search(r"枠複\s+(\d-\d)\s+([\d,]+)\s*円", block)
        if m_wf:
            a, b = sorted(int(x) for x in m_wf.group(1).split("-"))
            return f"{a}-{b}", int(m_wf.group(2).replace(",", ""))
    except Exception:
        pass
    return None, 0


def _pick_lane_for_bet(state: Dict) -> Optional[Dict]:
    lanes: List[Dict] = state.setdefault("lanes", [])
    free = [ln for ln in lanes if not ln.get("pending_race_id")]
    if free:
        free.sort(key=lambda x: (-int(x.get("stage_idx") or 0), int(x.get("id") or 0)))
        return free[0]
    if len(lanes) < MAX_CONCURRENT_LANES:
        new_ln = {
            "id": len(lanes),
            "stage_idx": 0,
            "set_bet": 0,
            "set_payout": 0,
            "cur_lose": 0,
            "pending_race_id": None,
        }
        lanes.append(new_ln)
        return new_ln
    return None


def settle_pending_bets(page, state: Dict, tracker, ymd: str) -> int:
    settled_cnt = 0
    lanes_by_id = {int(ln.get("id", 0)): ln for ln in (state.get("lanes") or [])}
    ds = state.setdefault("daily_stats", {"total_bet": 0, "total_payout": 0})

    for lg in reversed(state.get("race_logs") or []):
        if lg.get("status") != "PENDING":
            continue
        rid = str(lg.get("race_id") or "")
        bet_type = str(lg.get("bet_type") or "枠連")
        if not rid:
            continue

        wf_pair: Optional[str] = None
        win_time_sec: Optional[float] = None
        pay_100: int = 0
        if bet_type == "枠連":
            wf_pair, _, win_time_sec, pay_100 = fetch_jra_result_detail(page, rid, str(lg.get("surface_cond") or ""))
        else:
            m_rn = re.search(r"(\d{1,2})R", str(lg.get("race_title") or ""))
            rnum = int(m_rn.group(1)) if m_rn else 0
            wf_pair, pay_100 = _fetch_nar_result_pair_and_pay(page, rid, rnum)

        if not wf_pair:
            continue

        payout = 0
        hit_ticket = None
        for t in (lg.get("bought_tickets") or []):
            if t.get("pair") == wf_pair:
                t["is_hit"] = True
                amt = int(t.get("amount") or 0)
                if pay_100 > 0:
                    payout = int((amt // 100) * pay_100)
                else:
                    payout = int((amt * float(t.get("odds") or 0.0)) // 10) * 10
                hit_ticket = dict(t)
                break

        bet = int(lg.get("total_bet") or 0)
        lane_id = int(lg.get("lane_id") or 0)
        ln = lanes_by_id.get(lane_id) or (state["lanes"][0] if state.get("lanes") else None)
        si = int(lg.get("stage_idx") if lg.get("stage_idx") is not None else (ln["stage_idx"] if ln else 0))

        state["balance"] = int(state.get("balance") or 0) + (payout - bet)
        ds["total_bet"] = int(ds.get("total_bet") or 0) + bet
        ds["total_payout"] = int(ds.get("total_payout") or 0) + payout

        if ln is not None:
            ln["set_bet"] = int(ln.get("set_bet") or 0) + bet
            ln["set_payout"] = int(ln.get("set_payout") or 0) + payout
            if ln.get("pending_race_id") == rid:
                ln["pending_race_id"] = None

            if payout > 0:
                ln["cur_lose"] = 0
                new_si, new_sb, new_sp, status = resolve_next_stage_on_hit(
                    si, bet, payout, ln["set_bet"], ln["set_payout"], float(lg.get("synth_odds") or 0.0)
                )
                ln["stage_idx"], ln["set_bet"], ln["set_payout"] = new_si, new_sb, new_sp
            else:
                ln["cur_lose"] = int(ln.get("cur_lose") or 0) + 1
                if si >= MAX_STAGE_IDX:
                    status = "STOP_LOSS"
                    ln["stage_idx"] = 0
                    ln["set_bet"] = 0
                    ln["set_payout"] = 0
                    ln["cur_lose"] = 0
                else:
                    status = "MISS"
                    ln["stage_idx"] = si + 1
        else:
            status = "HIT" if payout > 0 else "MISS"

        if state["balance"] >= WITHDRAW_THRESHOLD:
            state["balance"] -= WITHDRAW_AMOUNT
            state["withdrawn_total"] = int(state.get("withdrawn_total") or 0) + WITHDRAW_AMOUNT

        lg["status"] = status
        lg["payout"] = payout
        lg["result_pair"] = wf_pair
        lg["hit_ticket"] = hit_ticket
        lg["balance"] = state["balance"] + int(state.get("withdrawn_total") or 0)

        snap = lg.get("_tracker_snap")
        if isinstance(snap, dict) and tracker is not None:
            snap["wf_pair"] = wf_pair
            if win_time_sec is not None:
                snap["win_time_sec"] = win_time_sec
            tracker.observe(snap)
            lg.pop("_tracker_snap", None)

        settled_cnt += 1
        print(
            f"✅ [自動精算] {lg.get('race_title')} ({bet_type}): 結果={wf_pair} / 投資{bet:,}円 → 払戻{payout:,}円 ({status})",
            flush=True,
        )

    if settled_cnt > 0:
        save_prod_state(state)
        if tracker is not None:
            save_live_tracker(tracker, current_ymd=ymd)
    return settled_cnt


def main():
    p = argparse.ArgumentParser(description=f"{MODEL_VERSION} 本番自動投票（自動精算・動的2レーン・クロスデイ適応）")
    p.add_argument("--target", type=str, default="all", choices=["all", "jra", "nar"])
    p.add_argument("--dry-run", action="store_true", help="購入直前で停止する安全モード")
    p.add_argument("--ignore-time", action="store_true", help="テスト用：発走時刻窓を無視して直近1Rのみ検証")
    p.add_argument("--poll-seconds", type=int, default=0, help="発走直前ウィンドウを待つ最大ポーリング秒数 (cron遅延対策: 180推奨)")
    args = p.parse_args()

    now_jst = datetime.now(JST)
    ymd = now_jst.strftime("%Y%m%d")
    state = load_prod_state()
    tracker = load_live_tracker(current_ymd=ymd)
    start_ts = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        settle_pending_bets(page, state, tracker, ymd)

        jra_logged_in = False
        nar_logged_in = False

        while True:
            now_jst = datetime.now(JST)
            any_upcoming = False

            if args.target in ("all", "jra"):
                rids = fetch_today_jra_race_ids(page, ymd)
                print(f"🏇 [{MODEL_VERSION} JRA] {ymd} 検出: {len(rids)}R", flush=True)
                for rid in rids:
                    already_bet = any(
                        (lg.get("race_id") == rid and lg.get("status") != "SKIP")
                        for lg in (state.get("race_logs") or [])
                    )
                    if already_bet and not args.dry_run:
                        continue

                    meta, combos = fetch_netkeiba_race_and_odds(page, rid, bet_type="枠連")
                    if not is_in_bet_window(meta.post_time, now_jst, args.ignore_time):
                        if is_upcoming_within(meta.post_time, now_jst, max_min=9.5):
                            any_upcoming = True
                        continue

                    lane = _pick_lane_for_bet(state)
                    if lane is None:
                        print(f"⏭️ [JRAレーン満杯] {meta.venue}{meta.race_num}R: {MAX_CONCURRENT_LANES}レーンとも精算待ちのため見送り", flush=True)
                        continue
                    cur_si = int(lane["stage_idx"])

                    race_dict = {
                        "race_id": rid, "date_key": ymd,
                        "venue": meta.venue, "race_num": meta.race_num, "race_name": meta.race_name,
                        "race_class": meta.race_class, "horse_count": meta.horse_count,
                        "surface": meta.surface, "distance": meta.distance, "track_condition": meta.track_condition,
                        "has_scratch": meta.has_scratch, "scratched_umabans": meta.scratched_umabans,
                        "fav1_win_odds": meta.fav1_win_odds, "fav2_win_odds": meta.fav2_win_odds,
                        "fav1_umaban": meta.fav1_umaban, "fav1_waku": meta.fav1_waku, "fav1_leg_type": meta.fav1_leg_type,
                        "horses_info": meta.horses_info, "waku_leg_scores": meta.waku_leg_scores,
                        "real_odds": combos,
                    }
                    ev = evaluate_v12_standard(race_dict, bet_label="枠連", stage_idx=cur_si, live_tracker=tracker)
                    if not ev["participate"]:
                        print(f"⏭️ [JRA見送り] {meta.venue}{meta.race_num}R: {ev['skip_reason']}", flush=True)
                        continue

                    st = STAGES[cur_si]
                    budget = st["late_bet"] if meta.race_num >= 9 else st["early_bet"]
                    eff_picked = trim_picks_for_budget(ev["picked"], budget, min_synth=MIN_SYNTH_HARD_FLOOR)
                    eff_cover = min(0.92, 0.62 + 0.04 * max(0, cur_si - 2))
                    tickets = allocate_bets(
                        eff_picked, budget,
                        tilt_strength=float(ev.get("alloc_tilt") or 0.0),
                        cover_ratio=eff_cover,
                        breakeven_margin=PROD_BREAKEVEN_MARGIN,
                    )
                    ev["picked"] = eff_picked
                    order = BetOrder(meta=meta, bet_type="枠連", tickets=tickets)
                    print(f"🎯 [JRA参戦 L{lane['id']}] {meta.venue}{meta.race_num}R ({st['name']}): {ev['combos_summary']} / {order.total_amount:,}円", flush=True)

                    if not jra_logged_in:
                        login_ipat(page)
                        jra_logged_in = True
                    if execute_ipat_bet(page, order, dry_run=args.dry_run):
                        if not args.dry_run:
                            lane["pending_race_id"] = rid
                        state["race_logs"].insert(0, {
                            "race_id": rid, "lane_id": lane["id"], "stage_idx": cur_si,
                            "timestamp": now_jst.strftime("%m/%d %H:%M"),
                            "race_title": f"{meta.venue} {meta.race_num}R",
                            "bet_type": order.bet_type, "stage_name": st["name"],
                            "mode_name": ev.get("mode", ev.get("bucket", MODEL_VERSION)),
                            "p_waku": ev.get("p_waku"), "tier_id": ev.get("tier_id", "-"),
                            "surface_cond": f"{meta.surface}{meta.track_condition}",
                            "combos_summary": ev["combos_summary"],
                            "synth_odds": ev.get("synth_odds", 0.0),
                            "bought_tickets": _build_bought_tickets_log(ev.get("picked"), tickets),
                            "hit_ticket": None,
                            "status": "SKIP" if args.dry_run else "PENDING",
                            "total_bet": 0 if args.dry_run else order.total_amount,
                            "payout": 0, "result_pair": "-", "result_pop": None, "result_odds": None,
                            "env_explain": ev.get("env_explain", ""),
                            "_tracker_snap": None if args.dry_run else {
                                "race_id": rid, "date_key": ymd, "venue": meta.venue,
                                "surface": meta.surface, "distance": meta.distance,
                                "race_name": meta.race_name, "race_class": meta.race_class,
                                "horses_info": [{"waku": h.get("waku"), "win_odds": h.get("win_odds"), "prev_c1": h.get("prev_c1"), "prev_c2": h.get("prev_c2"), "prev_c4": h.get("prev_c4"), "prev_field_size": h.get("prev_field_size")} for h in meta.horses_info],
                                "real_odds": combos[:20],
                            },
                        })
                        save_prod_state(state)
                    if args.ignore_time:
                        break

            if args.target in ("all", "nar"):
                metas = fetch_today_nar_races(page, ymd)
                print(f"🏇 [{MODEL_VERSION} NAR] {ymd} 検出: {len(metas)}R", flush=True)
                for meta in metas:
                    already_bet = any(
                        (lg.get("race_id") == meta.race_id and lg.get("status") != "SKIP")
                        for lg in (state.get("race_logs") or [])
                    )
                    if already_bet and not args.dry_run:
                        continue
                    if not is_in_bet_window(meta.post_time, now_jst, args.ignore_time):
                        if is_upcoming_within(meta.post_time, now_jst, max_min=9.5):
                            any_upcoming = True
                        continue

                    lane = _pick_lane_for_bet(state)
                    if lane is None:
                        print(f"⏭️ [NARレーン満杯] {meta.venue}{meta.race_num}R: 見送り", flush=True)
                        continue
                    cur_si = int(lane["stage_idx"])

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
                        "race_id": meta.race_id, "date_key": ymd,
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
                    ev = evaluate_v12_standard(race_dict, bet_label="枠複", stage_idx=cur_si, live_tracker=tracker)
                    if not ev["participate"]:
                        print(f"⏭️ [NAR見送り] {meta.venue}{meta.race_num}R: {ev['skip_reason']}", flush=True)
                        continue

                    st = STAGES[cur_si]
                    raw_b = st["late_bet"] if meta.race_num >= 9 else st["early_bet"]
                    budget = max(200, int(round((raw_b * float(NAR_PROFILE["bet_scale"])) / 100.0) * 100))
                    eff_picked = trim_picks_for_budget(ev["picked"], budget, min_synth=MIN_SYNTH_HARD_FLOOR)
                    eff_cover = min(0.92, 0.62 + 0.04 * max(0, cur_si - 2))
                    tickets = allocate_bets(
                        eff_picked, budget,
                        tilt_strength=float(ev.get("alloc_tilt") or 0.0),
                        cover_ratio=eff_cover,
                        breakeven_margin=PROD_BREAKEVEN_MARGIN,
                    )
                    ev["picked"] = eff_picked
                    order = NarBetOrder(meta=meta, bet_type="枠複", tickets=tickets)
                    print(f"🎯 [NAR参戦 L{lane['id']}] {meta.venue}{meta.race_num}R ({st['name']}): {ev['combos_summary']} / {order.total_amount:,}円", flush=True)

                    if not nar_logged_in and not args.dry_run:
                        login_rakuten_keiba(page)
                        nar_logged_in = True
                    if execute_rakuten_bet(page, order, dry_run=args.dry_run):
                        if not args.dry_run:
                            lane["pending_race_id"] = meta.race_id
                        state["race_logs"].insert(0, {
                            "race_id": meta.race_id, "lane_id": lane["id"], "stage_idx": cur_si,
                            "timestamp": now_jst.strftime("%m/%d %H:%M"),
                            "race_title": f"{meta.venue} {meta.race_num}R",
                            "bet_type": order.bet_type, "stage_name": st["name"],
                            "mode_name": ev.get("mode", ev.get("bucket", MODEL_VERSION)),
                            "p_waku": ev.get("p_waku"), "tier_id": ev.get("tier_id", "-"),
                            "surface_cond": f"{tm['surface']}{final_cond}",
                            "combos_summary": ev["combos_summary"],
                            "synth_odds": ev.get("synth_odds", 0.0),
                            "bought_tickets": _build_bought_tickets_log(ev.get("picked"), tickets),
                            "hit_ticket": None,
                            "status": "SKIP" if args.dry_run else "PENDING",
                            "total_bet": 0 if args.dry_run else order.total_amount,
                            "payout": 0, "result_pair": "-", "result_pop": None, "result_odds": None,
                            "env_explain": ev.get("env_explain", ""),
                        })
                        save_prod_state(state)
                    if args.ignore_time:
                        break

            elapsed = time.time() - start_ts
            if args.ignore_time or args.poll_seconds <= 0 or (not any_upcoming) or (elapsed >= args.poll_seconds):
                break
            time.sleep(30)
            settle_pending_bets(page, state, tracker, ymd)

        browser.close()
        save_live_tracker(tracker, current_ymd=ymd)


if __name__ == "__main__":
    main()
