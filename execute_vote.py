import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from env_loader import load_env_file
from logger_config import setup_logger
from wakuren_logic import WakurenRuleEngine, STAGES
from extract_odds import RealtimeOddsExtractor, FALLBACK_SCHEDULE, JRA_VENUES
from auto_vote import RakutenKeibaVoter, JraIpatVoter

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_env_file(ENV_PATH)

logger = setup_logger("execute_vote")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="JRA(土日) & 楽天競馬NAR(平日/毎日) 実残高判定 -> 3分前オッズ取得 -> 2分30秒前投票確定 (v11.9)"
    )
    parser.add_argument("--mode", type=str, default="both", help="実行モード (both / nar / jra)")
    parser.add_argument("--venue", type=str, default="auto", help="対象競馬場 ('auto' で直近の発走レースを自動選択)")
    parser.add_argument("--race_num", type=int, default=0, help="対象レース番号 (0 で直近レースを自動選択)")
    parser.add_argument("--race_id", type=str, default="auto", help="互換用引数")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run専用JSONへ保存するテストモード")
    parser.add_argument("--mock", action="store_true", help="待機時間をスキップして即座にシミュレーション検証するモード")
    return parser.parse_args()


def pick_upcoming_race(schedule, target_venue: str = "auto", target_race_num: int = 0):
    now = datetime.now()
    if target_venue != "auto" and target_race_num > 0:
        for r in schedule:
            if r["venue"] == target_venue and r["race_num"] == target_race_num:
                return r
        org = "JRA" if target_venue in JRA_VENUES else "NAR"
        return {
            "org": org,
            "venue": target_venue,
            "race_num": target_race_num,
            "post_time": (now + timedelta(minutes=3)).strftime("%H:%M"),
            "num_horses": 10,
        }

    candidates = []
    for r in schedule:
        hh, mm = map(int, r["post_time"].split(":"))
        post_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        diff_sec = (post_dt - now).total_seconds()
        if diff_sec >= -60:
            candidates.append((diff_sec, r))
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1] if candidates else None


def evaluate_previous_ticket_by_balance(
    engine: WakurenRuleEngine,
    extractor: RealtimeOddsExtractor,
    nar_voter: RakutenKeibaVoter,
    jra_voter: JraIpatVoter,
    page,
    mock: bool,
):
    active = engine.state.get("active_ticket")
    if not active:
        return

    p_org = active.get("org", "NAR")
    p_venue = active.get("venue", "")
    p_rnum = int(active.get("race_num", 0))
    p_btype = active.get("bet_type", "枠複")
    prev_bet = int(active.get("total_bet", 0))
    post_bet_bal = active.get("post_bet_balance")
    voter = jra_voter if p_org == "JRA" else nar_voter

    logger.info(f"🔎 前回購入馬券 ({p_org} {p_venue} {p_rnum}R / 投資{prev_bet:,}円) の的中・残高変動を確認します...")

    res = None if mock else extractor.fetch_race_result(page, p_org, p_venue, p_rnum, p_btype)
    if res:
        logger.info(f"📄 払戻金確定データを取得しました: {res['w1']}-{res['w2']} ({res['dividend_yen']}円)")
        engine.record_result(winning_w1=res["w1"], winning_w2=res["w2"], dividend_yen=res["dividend_yen"])
        return

    real_bal = None if mock else voter.get_real_balance(page)
    if real_bal is not None and post_bet_bal is not None:
        diff = real_bal - int(post_bet_bal)
        logger.info(f"💴 {p_org} 実残高チェック: 購入直後 {post_bet_bal:,}円 -> 現在 {real_bal:,}円 (差額: {diff:+,}円)")

        if diff > 0:
            engine.state["daily_stats"]["total_bet"] += prev_bet
            engine.state["daily_stats"]["total_payout"] += diff
            engine.state["balance"] = real_bal
            engine.state["set_loss"] = 0
            engine.state["stage_idx"] = 0
            for log_item in reversed(engine.state.get("race_logs", [])):
                if log_item.get("status") == "PENDING" and log_item.get("race_title") == active.get("race_title"):
                    log_item["status"] = "HIT"
                    log_item["result_pair"] = "残高増加確認"
                    log_item["payout"] = diff
                    log_item["balance"] = real_bal
                    break
            engine.state["active_ticket"] = None
            engine.save_state()
            logger.info(f"🎯 [実残高増加による的中判定] +{diff:,}円の増加を確認！ -> 即座に【1戦目】へリセットします。")
            return

    logger.info("❌ 残高の増加が確認されないため『不的中』として処理し、次の戦目（追い上げ）へ進みます。")
    engine.state["daily_stats"]["total_bet"] += prev_bet
    new_loss = engine.state["set_loss"] + prev_bet
    engine.state["balance"] = real_bal if real_bal is not None else (engine.state["balance"] - prev_bet)

    if engine.state["stage_idx"] >= 7:
        logger.warning("⚠️ 8戦目不的中のため損切りルール発動 -> 次回1戦目へリセット")
        engine.state["set_loss"] = 0
        engine.state["stage_idx"] = 0
    else:
        engine.state["set_loss"] = new_loss
        engine.state["stage_idx"] += 1

    for log_item in reversed(engine.state.get("race_logs", [])):
        if log_item.get("status") == "PENDING" and log_item.get("race_title") == active.get("race_title"):
            log_item["status"] = "MISS"
            log_item["result_pair"] = "不的中"
            log_item["payout"] = 0
            log_item["balance"] = engine.state["balance"]
            break

    engine.state["active_ticket"] = None
    engine.save_state()


def run_two_stage_cycle(mode: str, venue: str, race_num: int, headless: bool, dry_run: bool, mock: bool):
    now = datetime.now()
    is_weekend = now.weekday() in (5, 6)  # 5=土曜, 6=日曜
    use_jra = (mode == "jra") or (mode == "both" and is_weekend)
    use_nar = (mode == "nar") or (mode == "both")

    env_label = "DRY-RUN テスト環境" if dry_run else "本番運用環境"
    day_type = "土日（JRA＋NAR対応）" if is_weekend else "平日（NAR楽天競馬）"
    logger.info("=" * 64)
    logger.info(f"🚀 [{env_label} / {day_type}] 監視・投票ジョブ開始 (mode={mode}, dry_run={dry_run}, mock={mock})")

    engine = WakurenRuleEngine(logger=logger, dry_run=dry_run)
    extractor = RealtimeOddsExtractor(logger=logger, headless=headless)
    nar_voter = RakutenKeibaVoter(logger=logger, headless=headless, dry_run=dry_run)
    jra_voter = JraIpatVoter(logger=logger, headless=headless, dry_run=dry_run)

    pw_ctx, browser, page = None, None, None

    try:
        if not mock:
            from playwright.sync_api import sync_playwright

            pw_ctx = sync_playwright().start()
            browser = pw_ctx.chromium.launch(headless=headless)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()

        # 1. 当日スケジュールを統合取得（平日=NARのみ / 土日=JRA+NAR）
        schedule = []
        if mock:
            schedule = FALLBACK_SCHEDULE
        else:
            if use_jra:
                schedule.extend(extractor.fetch_jra_today_schedule(page))
            if use_nar:
                schedule.extend(extractor.fetch_nar_today_schedule(page))

        target_race = pick_upcoming_race(schedule, target_venue=venue, target_race_num=race_num)
        if not target_race:
            logger.info("本日の対象レースはすべて終了しています。")
            return

        t_org = target_race.get("org", "NAR")
        t_venue = target_race["venue"]
        t_rnum = target_race["race_num"]
        t_post = target_race["post_time"]
        race_title = f"{t_venue} {t_rnum}R"
        active_voter = jra_voter if t_org == "JRA" else nar_voter

        # 2. ログイン確認（対象レースの主催者へログイン）
        if not mock:
            logged_in = active_voter.ensure_logged_in(page)
            if not logged_in and not dry_run:
                return

        # 3. 前回購入済み馬券があれば実残高の増減で的中・不的中を確定
        evaluate_previous_ticket_by_balance(engine, extractor, nar_voter, jra_voter, page, mock=mock)

        cur_stage = STAGES[engine.state["stage_idx"]]["name"]
        logger.info(
            f"💰 [{env_label}] 現在の運用状態: 【{cur_stage}】 / 残高:{engine.state['balance']:,}円 / セット負債:{engine.state['set_loss']:,}円"
        )

        # 4. 同一レースの二重購入防止チェック
        today_prefix = now.strftime("%m/%d")
        already_done = any(
            log.get("race_title") == race_title
            and str(log.get("timestamp", "")).startswith(today_prefix)
            and log.get("status") in ("PENDING", "HIT", "MISS")
            for log in engine.state.get("race_logs", [])
        )
        if already_done:
            logger.info(f"⏭️ {race_title} はすでに本日投票済み（二重購入防止）のためスキップします。")
            return

        hh, mm = map(int, t_post.split(":"))
        post_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        rem_sec = (post_dt - datetime.now()).total_seconds()

        logger.info(f"🎯 次回監視対象レース: [{t_org}] {race_title} (発走時刻 {t_post} / 残り {int(rem_sec)}秒)")

        if not mock and rem_sec > 900:
            logger.info(f"⏳ 発走まであと {int(rem_sec // 60)}分 あるため、発走3分前付近の起動まで待機します。")
            return

        # 5. 【フェーズ1：発走3分前】事前オッズ取得
        if not mock and rem_sec > 180:
            wait_3m = rem_sec - 180
            logger.info(f"⏱️ 発走3分前 ({(post_dt - timedelta(minutes=3)).strftime('%H:%M:%S')}) まで {int(wait_3m)}秒 待機します...")
            time.sleep(wait_3m)

        logger.info(f"🔍 [フェーズ1：発走3分前オッズ取得] [{t_org}] {race_title} の最新オッズを取得します...")
        snap_3m = (
            {
                "org": t_org,
                "mode": t_org.lower(),
                "venue": t_venue,
                "race_num": t_rnum,
                "is_obstacle": False,
                "num_horses": target_race["num_horses"],
                "is_odds_released": True,
                "fav1_win_odds": 1.6,
                "fav1_frame_no": 4,
                "fav1_horse_no": 4,
                "has_scratch": False,
                "wakuren_combos": [],
                "wakutan_combos": [],
                "umatan_combos": [
                    {"w1": 4, "w2": 7, "odds": 5.2, "pop": 1},
                    {"w1": 4, "w2": 2, "odds": 7.8, "pop": 2},
                    {"w1": 4, "w2": 9, "odds": 10.4, "pop": 3},
                    {"w1": 4, "w2": 1, "odds": 13.5, "pop": 4},
                    {"w1": 4, "w2": 6, "odds": 17.2, "pop": 5},
                ],
            }
            if mock
            else extractor.fetch_race_snapshot(page, t_org, t_venue, t_rnum)
        )

        if not snap_3m["is_odds_released"]:
            logger.warning(f"⚠️ {race_title} はまだオッズ未発売（0.0表示）です。2分30秒前にもう一度確認します。")
        else:
            logger.info(
                f"📊 [3分前確認] {race_title} / {snap_3m['num_horses']}頭立 / "
                f"単勝1人気:{snap_3m['fav1_horse_no']}番({snap_3m['fav1_win_odds']}倍)"
            )

        # 6. 【フェーズ2：締切2分30秒前】最終オッズ再取得 -> v11.9計算 -> 投票確定
        rem_sec2 = (post_dt - datetime.now()).total_seconds()
        if not mock and rem_sec2 > 150:
            wait_2m = rem_sec2 - 150
            logger.info(f"⏱️ 締切2分30秒前 ({(post_dt - timedelta(seconds=150)).strftime('%H:%M:%S')}) まで {int(wait_2m)}秒 待機します...")
            time.sleep(wait_2m)

        logger.info(f"🔄 [フェーズ2：締切2分30秒前 最終オッズ再取得] [{t_org}] {race_title} の直前オッズを再取得します...")
        snap_final = snap_3m if mock else extractor.fetch_race_snapshot(page, t_org, t_venue, t_rnum)

        if not snap_final["is_odds_released"]:
            logger.warning(f"🚫 {race_title} は締切2分30秒前時点でもオッズ取得不可のため見送ります。")
            return

        is_ok, reject_reasons, use_axis_mode = engine.check_defensive_filters(
            venue=snap_final["venue"],
            is_obstacle=snap_final["is_obstacle"],
            num_horses=snap_final["num_horses"],
            fav1_win_odds=snap_final["fav1_win_odds"],
            has_scratch=snap_final["has_scratch"],
        )
        if not is_ok:
            for r in reject_reasons:
                logger.warning(f"🚫 参加見送り (防御フィルター): {r}")
            engine.append_race_log(
                race_title=race_title,
                bet_type="-",
                stage_name=f"{engine.state['stage_idx'] + 1}戦目",
                combos_summary=" / ".join(reject_reasons),
                status="SKIP",
            )
            engine.save_state()
            return

        ticket, msg = engine.build_ticket(
            race_num=snap_final["race_num"],
            wakuren_combos=snap_final["wakuren_combos"],
            wakutan_combos=snap_final.get("wakutan_combos"),
            umatan_combos=snap_final.get("umatan_combos"),
            use_axis_mode=use_axis_mode,
            fav1_frame_no=snap_final.get("fav1_frame_no"),
            fav1_horse_no=snap_final.get("fav1_horse_no"),
            venue=t_venue,
            org=t_org,
        )
        if not ticket:
            logger.warning(f"🚫 参加見送り (オッズ基準未達): {msg}")
            engine.append_race_log(
                race_title=race_title,
                bet_type="-",
                stage_name=f"{engine.state['stage_idx'] + 1}戦目",
                combos_summary=msg,
                status="SKIP",
            )
            engine.save_state()
            return

        success = active_voter.vote_on_open_page(page=page, race_data=snap_final, ticket=ticket)
        if success:
            after_bal = None if mock else active_voter.get_real_balance(page)
            if after_bal is None:
                after_bal = engine.state["balance"] - ticket["total_bet"]
            ticket["post_bet_balance"] = after_bal

            engine.state["active_ticket"] = ticket
            engine.append_race_log(
                race_title=ticket["race_title"],
                bet_type=ticket["bet_type"],
                stage_name=ticket["stage_name"],
                combos_summary=ticket["combos_summary"],
                status="PENDING",
                total_bet=ticket["total_bet"],
            )
            engine.save_state()
            logger.info(
                f"✅ [{env_label} / {t_org} 投票完了＆保存] {ticket['race_title']} / 投資 {ticket['total_bet']}円 / 購入後残高 {after_bal:,}円"
            )
        else:
            logger.error("❌ 投票確定に失敗したため、戦目は進めずに終了します。")

    finally:
        if browser:
            browser.close()
        if pw_ctx:
            pw_ctx.stop()


def main():
    args = parse_args()
    try:
        run_two_stage_cycle(
            mode=args.mode,
            venue=args.venue,
            race_num=args.race_num,
            headless=args.headless,
            dry_run=args.dry_run,
            mock=args.mock,
        )
    except Exception as e:
        logger.exception(f"予期せぬエラーが発生しました: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
