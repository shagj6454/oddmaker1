import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from env_loader import load_env_file
from logger_config import setup_logger
from wakuren_logic import WakurenRuleEngine, STAGES
from extract_odds import RealtimeOddsExtractor, TODAY_SCHEDULE_20261120
from auto_vote import RakutenKeibaVoter

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_env_file(ENV_PATH)

logger = setup_logger("execute_vote")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="楽天競馬(NAR) 自動結果照合 -> 3分前オッズ取得 -> 2分前再取得＆投票確定 (v11.9)"
    )
    parser.add_argument("--mode", type=str, default="nar", help="実行モード (nar / both / jra)")
    parser.add_argument("--venue", type=str, default="auto", help="対象競馬場 ('auto' で直近の発走レースを自動選択)")
    parser.add_argument("--race_num", type=int, default=0, help="対象レース番号 (0 で直近レースを自動選択)")
    parser.add_argument("--race_id", type=str, default="auto", help="互換用引数")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="購入確定ボタンのみスキップするテストモード")
    parser.add_argument("--mock", action="store_true", help="待機時間をスキップして即座にシミュレーション検証するモード")
    return parser.parse_args()


def pick_upcoming_race(schedule, target_venue: str = "auto", target_race_num: int = 0):
    now = datetime.now()
    if target_venue != "auto" and target_race_num > 0:
        for r in schedule:
            if r["venue"] == target_venue and r["race_num"] == target_race_num:
                return r
        return {
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


def settle_pending_ticket(engine: WakurenRuleEngine, extractor: RealtimeOddsExtractor, page, mock: bool) -> bool:
    """
    前回の購入済みチケット(active_ticket)が残っている場合、払戻金一覧を自動照合して
    的中(1戦目リセット)または不的中(次の戦目へ進む)を確定させる。
    戻り値: True=照合完了(または未照合なし) / False=前レースがまだ確定前のため次レース見送り推奨
    """
    active = engine.state.get("active_ticket")
    if not active:
        return True

    p_venue = active.get("venue", "")
    p_rnum = int(active.get("race_num", 0))
    p_btype = active.get("bet_type", "枠複")
    logger.info(f"🔎 前回購入済み馬券の自動結果照合を実行します: {p_venue} {p_rnum}R ({p_btype})")

    if mock:
        res = {"w1": 4, "w2": 7, "dividend_yen": 520}
    else:
        res = extractor.fetch_nar_race_result(page, p_venue, p_rnum, p_btype)

    if not res:
        logger.warning(
            f"⏳ 前回の {p_venue} {p_rnum}R の払戻金がまだ確定していません。"
            "追い上げ戦目の重複・ズレを防ぐため、今回のレースは見送ります。"
        )
        return False

    engine.record_result(winning_w1=res["w1"], winning_w2=res["w2"], dividend_yen=res["dividend_yen"])
    return True


def run_two_stage_nar_cycle(venue: str, race_num: int, headless: bool, dry_run: bool, mock: bool):
    logger.info("=" * 64)
    logger.info(f"🚀 [楽天競馬 自動結果照合＆2段階投票ジョブ開始] dry_run={dry_run}, mock={mock}")

    engine = WakurenRuleEngine(logger=logger)
    extractor = RealtimeOddsExtractor(logger=logger, headless=headless)
    voter = RakutenKeibaVoter(logger=logger, headless=headless, dry_run=dry_run)

    pw_ctx = None
    browser = None
    page = None

    try:
        if not mock:
            from playwright.sync_api import sync_playwright

            pw_ctx = sync_playwright().start()
            browser = pw_ctx.chromium.launch(headless=headless)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()

        # 0. 【最優先】前回の購入済みチケットがあれば自動で払戻照合して戦目・負債を更新
        settled_ok = settle_pending_ticket(engine, extractor, page, mock=mock)
        if not settled_ok:
            return

        cur_stage = STAGES[engine.state["stage_idx"]]["name"]
        logger.info(
            f"💰 現在の運用状態: 【{cur_stage}】 / 残高:{engine.state['balance']:,}円 / セット負債:{engine.state['set_loss']:,}円"
        )

        # 1. ログイン状態確認＆楽天IDログイン
        if not mock:
            logged_in = voter.ensure_logged_in(page)
            if not logged_in and not dry_run:
                return

        # 2. 当日の発走時刻スケジュール取得
        schedule = TODAY_SCHEDULE_20261120 if mock else extractor.fetch_nar_today_schedule(page)
        target_race = pick_upcoming_race(schedule, target_venue=venue, target_race_num=race_num)
        if not target_race:
            logger.info("本日の対象レースはすべて終了しています。")
            return

        t_venue = target_race["venue"]
        t_rnum = target_race["race_num"]
        t_post = target_race["post_time"]
        race_title = f"{t_venue} {t_rnum}R"

        # 二重購入防止チェック（本日すでに同じレースを購入・記録済みならスキップ）
        today_prefix = datetime.now().strftime("%m/%d")
        already_done = any(
            log.get("race_title") == race_title
            and str(log.get("timestamp", "")).startswith(today_prefix)
            and log.get("status") in ("PENDING", "HIT", "MISS")
            for log in engine.state.get("race_logs", [])
        )
        if already_done:
            logger.info(f"⏭️ {race_title} はすでに本日投票・処理済みのためスキップします。")
            return

        now = datetime.now()
        hh, mm = map(int, t_post.split(":"))
        post_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        rem_sec = (post_dt - now).total_seconds()

        logger.info(f"🎯 次回監視対象レース: {race_title} (発走時刻 {t_post} / 残り {int(rem_sec)}秒)")

        if not mock and rem_sec > 900:
            logger.info(f"⏳ 発走まであと {int(rem_sec // 60)}分 あるため、発走3分前付近の起動まで待機します。")
            return

        # 3. 【フェーズ1：発走3分前】事前オッズスクレイピング＆予備判定
        if not mock and rem_sec > 180:
            wait_3m = rem_sec - 180
            logger.info(f"⏱️ 発走3分前 ({(post_dt - timedelta(minutes=3)).strftime('%H:%M:%S')}) まで {int(wait_3m)}秒 待機します...")
            time.sleep(wait_3m)

        logger.info(f"🔍 [フェーズ1：発走3分前オッズ取得] {race_title} の最新オッズを取得します...")
        snap_3m = (
            {
                "mode": "nar",
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
            else extractor.fetch_nar_race_snapshot(page, t_venue, t_rnum)
        )

        if not snap_3m["is_odds_released"]:
            logger.warning(f"⚠️ {race_title} はまだオッズ未発売（0.0表示）です。2分前にもう一度確認します。")
        else:
            logger.info(
                f"📊 [3分前確認] {race_title} / {snap_3m['num_horses']}頭立 / "
                f"単勝1人気:{snap_3m['fav1_horse_no']}番({snap_3m['fav1_win_odds']}倍)"
            )

        # 4. 【フェーズ2：締切（発走）2分前】最終オッズ再取得 -> v11.9計算 -> 投票確定！
        now2 = datetime.now()
        rem_sec2 = (post_dt - now2).total_seconds()
        if not mock and rem_sec2 > 120:
            wait_2m = rem_sec2 - 120
            logger.info(f"⏱️ 締切2分前 ({(post_dt - timedelta(minutes=2)).strftime('%H:%M:%S')}) まで {int(wait_2m)}秒 待機します...")
            time.sleep(wait_2m)

        logger.info(f"🔄 [フェーズ2：締切2分前 最終オッズ再取得] {race_title} の直前オッズを再スクレイピングします...")
        snap_final = snap_3m if mock else extractor.fetch_nar_race_snapshot(page, t_venue, t_rnum)

        if not snap_final["is_odds_released"]:
            logger.warning(f"🚫 {race_title} は締切2分前時点でもオッズ取得不可のため見送ります。")
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
            org="NAR",
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

        # 5. 直前オッズで再計算した最終チケットで投票確定！
        success = voter.vote_on_open_page(page=page, race_data=snap_final, ticket=ticket)
        if success:
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
            logger.info(f"✅ [締切2分前 投票完了＆JSON保存] {ticket['race_title']} / 投資 {ticket['total_bet']}円")
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
        run_two_stage_nar_cycle(
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
