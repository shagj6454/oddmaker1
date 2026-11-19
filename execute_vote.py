import argparse
import sys
from datetime import datetime
from pathlib import Path

from env_loader import load_env_file
from logger_config import setup_logger
from wakuren_logic import WakurenRuleEngine
from extract_odds import RealtimeOddsExtractor
from auto_vote import IPATVoter, RakutenKeibaVoter

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_env_file(ENV_PATH)

logger = setup_logger("execute_vote")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GitHub Actions クラウド自動実行対応: JRA(即PAT)・地方競馬(楽天競馬) 自動投票スクリプト (v11.9)"
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["jra", "nar", "both"],
        default="both",
        help="投票対象モード: 'jra', 'nar', 'both'",
    )
    parser.add_argument(
        "--race_id",
        type=str,
        default="auto",
        help="対象レースID ('auto' の場合は現在時刻から発走3〜5分前のレースを自動検出)",
    )
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="馬券購入をスキップして計算・ログ保存のみ実行")
    parser.add_argument("--mock", action="store_true", help="平日・時間外テスト用の擬似オッズを使用")
    return parser.parse_args()


def resolve_auto_race_id(mode: str, mock: bool) -> str:
    now = datetime.now()
    if mock:
        return "202605050301" if mode == "jra" else f"{now.strftime('%Y%m%d')}20120304"
    # 実運用時は開催スケジュールから発走直前のrace_idを返す
    return f"{now.strftime('%Y%m%d')}01"


def execute_single_race(mode: str, race_id: str, headless: bool, dry_run: bool, mock: bool):
    resolved_id = resolve_auto_race_id(mode, mock) if race_id == "auto" else race_id
    logger.info("=" * 60)
    logger.info(
        f"☁️ [GitHub Actions Cloud] ジョブ開始: mode={mode.upper()}, race_id={resolved_id}, dry_run={dry_run}, mock={mock}"
    )

    engine = WakurenRuleEngine(logger=logger)
    extractor = RealtimeOddsExtractor(logger=logger, headless=headless)

    # 1. オッズ＆レース条件の取得
    race_data = extractor.fetch_race_data(mode=mode, race_id=resolved_id, mock=mock)
    logger.info(
        f"取得レース情報: {race_data['venue']}{race_data['race_num']}R / "
        f"{race_data['num_horses']}頭立 / 単勝1人気:{race_data['fav1_win_odds']}倍"
    )

    # 2. v11.9 防御フィルター判定 (金沢・笠松除外、11頭以下/1倍台は枠単/馬単切替、障害・ばんえい・取消馬除外)
    is_ok, reject_reasons, use_axis_mode = engine.check_defensive_filters(
        venue=race_data["venue"],
        is_obstacle=race_data["is_obstacle"],
        num_horses=race_data["num_horses"],
        fav1_win_odds=race_data["fav1_win_odds"],
        has_scratch=race_data["has_scratch"],
    )
    if not is_ok:
        for r in reject_reasons:
            logger.warning(f"🚫 参加見送り (防御フィルター発動): {r}")
        engine.append_race_log(
            race_title=f"{race_data['venue']} {race_data['race_num']}R",
            bet_type="-",
            stage_name=f"{engine.state['stage_idx'] + 1}戦目",
            combos_summary=" / ".join(reject_reasons),
            status="SKIP",
        )
        engine.save_state()
        logger.info(f"戦目は進めず現在の【{engine.state['stage_idx'] + 1}戦目】を維持して終了します。")
        return

    # 3. 枠連/枠複/枠単/馬単 買い目構築と追い上げ資金配分計算
    ticket, msg = engine.build_ticket(
        race_num=race_data["race_num"],
        wakuren_combos=race_data["wakuren_combos"],
        wakutan_combos=race_data.get("wakutan_combos"),
        umatan_combos=race_data.get("umatan_combos"),
        use_axis_mode=use_axis_mode,
        fav1_frame_no=race_data.get("fav1_frame_no"),
        fav1_horse_no=race_data.get("fav1_horse_no"),
        venue=race_data["venue"],
        org="JRA" if mode == "jra" else "NAR",
    )
    if not ticket:
        logger.warning(f"🚫 参加見送り (オッズ条件未達): {msg}")
        engine.append_race_log(
            race_title=f"{race_data['venue']} {race_data['race_num']}R",
            bet_type="-",
            stage_name=f"{engine.state['stage_idx'] + 1}戦目",
            combos_summary=msg,
            status="SKIP",
        )
        engine.save_state()
        return

    # 4. 投票実行 (JRA 即PAT または 楽天競馬)
    if mode == "jra":
        voter = IPATVoter(logger=logger, headless=headless, dry_run=dry_run)
        success = voter.vote_wakuren(race_data=race_data, ticket=ticket)
    else:
        voter = RakutenKeibaVoter(logger=logger, headless=headless, dry_run=dry_run)
        success = voter.vote_wakufuku(race_data=race_data, ticket=ticket)

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
        logger.info(
            f"✅ 馬券投票＆GitHub状態ファイル(wakuren_state_v11_9.json)保存完了 (投資額: {ticket['total_bet']}円)"
        )
    else:
        logger.error("❌ 投票処理が中断または失敗したため、戦目状態は変更しません。")


def main():
    args = parse_args()
    modes = ["jra", "nar"] if args.mode == "both" else [args.mode]
    for m in modes:
        try:
            execute_single_race(
                mode=m,
                race_id=args.race_id,
                headless=args.headless,
                dry_run=args.dry_run,
                mock=args.mock,
            )
        except Exception as e:
            logger.exception(f"予期せぬエラーが発生しました (mode={m}): {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
