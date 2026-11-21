import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from playwright.sync_api import Page, sync_playwright

from jra_netkeiba_bettor import (
    BetOrder,
    RaceMeta,
    execute_ipat_bet,
    fetch_netkeiba_race_and_odds,
    login_ipat,
)

JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = DATA_DIR / "wakuren_state_v11_9.json"

INITIAL_BANKROLL = 52000
WITHDRAW_THRESHOLD = 104000
WITHDRAW_AMOUNT = 52000
EXCLUDED_VENUES = ("金沢", "笠松", "帯広", "ばんえい")

# ルール4：戦目別・投資額＆目標払戻テーブル
STAGES = [
    {"stage": 1, "name": "1戦目", "target": 1350,  "early": 700,   "late": 500},
    {"stage": 2, "name": "2戦目", "target": 2400,  "early": 1300,  "late": 900},
    {"stage": 3, "name": "3戦目", "target": 4050,  "early": 2100,  "late": 1500},
    {"stage": 4, "name": "4戦目", "target": 7000,  "early": 3700,  "late": 2600},
    {"stage": 5, "name": "5戦目", "target": 11900, "early": 6300,  "late": 4400},
    {"stage": 6, "name": "6戦目", "target": 20250, "early": 10600, "late": 7500},
    {"stage": 7, "name": "7戦目", "target": 34500, "early": 18100, "late": 12800},
    {"stage": 8, "name": "8戦目", "target": 58800, "early": 21800, "late": 21800},
]


def load_state() -> Dict:
    if STATE_PATH.exists():
        try:
            return json.load(open(STATE_PATH, "r", encoding="utf-8"))
        except Exception:
            pass
    return {
        "updated_at": "",
        "balance": INITIAL_BANKROLL,
        "withdrawn_total": 0,
        "set_loss": 0,
        "stage_idx": 0,
        "daily_stats": {"total_bet": 0, "total_payout": 0},
        "race_logs": [],
    }


def save_state(state: Dict) -> None:
    state["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def calc_synth(odds_list: List[float]) -> float:
    valid = [o for o in odds_list if o > 0]
    if not valid:
        return 0.0
    inv = sum(1.0 / o for o in valid)
    return round(1.0 / inv, 2) if inv > 0 else 0.0


def allocate_bets(picked: List[Dict], total_budget: int) -> List[Tuple[str, int]]:
    """合成オッズ均等配分（100円単位）で各組番の購入額を決定"""
    inv_sum = sum(1.0 / c["odds"] for c in picked if c["odds"] > 0)
    if inv_sum <= 0:
        return []
    tickets: List[Tuple[str, int]] = []
    for c in picked:
        raw = total_budget * ((1.0 / c["odds"]) / inv_sum)
        amt = max(100, int(round(raw / 100.0) * 100))
        tickets.append((c["pair"], amt))
    return tickets


def fetch_today_jra_race_ids(page: Page, ymd: str) -> List[str]:
    """netkeibaの当日レース一覧からJRA全レースID（12桁）を取得"""
    url = f"https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={ymd}"
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(600)
    html = page.content()
    ids = sorted(set(re.findall(r"race_id=(\d{12})", html)))
    return ids


def fetch_netkeiba_tan_fav1(page: Page, race_id: str) -> Tuple[float, bool]:
    """netkeibaから単勝1番人気オッズと枠連発売有無を取得"""
    url = f"https://race.netkeiba.com/odds/index.html?type=b1&race_id={race_id}"
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(500)
    body = page.inner_text("body")
    has_waku = "枠連" in body
    odds_vals = [float(x) for x in re.findall(r"\b(\d+\.\d)\b", body) if 1.0 <= float(x) <= 99.9]
    fav1 = min(odds_vals) if odds_vals else 9.9
    return fav1, has_waku


def build_v11_9_order(
    page: Page,
    meta: RaceMeta,
    fav1_odds: float,
    has_waku: bool,
    stage_idx: int,
) -> Tuple[Optional[BetOrder], str]:
    """
    v11.9 ルール2・3・4に従って買い目と金額を構築する。
      - 除外場 / 障害戦 / 取消・除外馬あり -> 見送り
      - 単勝1倍台(<=1.9): 枠単優先(なし時は馬単) 2-4番人気 (合成1.80+)
      - 少頭数(枠連なし & 単勝>=2.0): 枠単優先(なし時は馬単) 2-5番人気 (合成1.80+)
      - 前半1-8R: 枠連 2-6番人気 (ゾロ目除外・合成1.80+)
      - 後半9-12R: 枠連 3-8番人気 (ゾロ目除外・合成1.80+)
    """
    if any(ex in meta.venue for ex in EXCLUDED_VENUES) or ("障害" in meta.race_name):
        return None, "除外競馬場/障害戦(見送り)"
    if meta.has_scratch:
        return None, "取消/除外馬あり(見送り)"

    # 8戦目は9R以降推奨
    if stage_idx == 7 and meta.race_num <= 8:
        return None, "8戦目は9R以降推奨のため見送り"

    if fav1_odds <= 1.9:
        bet_type = "馬単"  # JRAは枠単がないため馬単へ自動切替
        _, combos = fetch_netkeiba_race_and_odds(page, meta.race_id, bet_type="馬連")
        picked = [c for c in combos if 2 <= c["pop"] <= 4]
    elif not has_waku:
        bet_type = "馬単"
        _, combos = fetch_netkeiba_race_and_odds(page, meta.race_id, bet_type="馬連")
        picked = [c for c in combos if 2 <= c["pop"] <= 5]
    else:
        bet_type = "枠連"
        _, combos = fetch_netkeiba_race_and_odds(page, meta.race_id, bet_type="枠連")
        non_zoro = [c for c in combos if c["w1"] != c["w2"]]
        if meta.race_num <= 8:
            picked = [c for c in non_zoro if c["pop"] >= 2][:5]
        else:
            picked = [c for c in non_zoro if c["pop"] >= 3][:6]

    synth = calc_synth([c["odds"] for c in picked])
    if not picked or synth < 1.80:
        return None, f"{bet_type} 実合成{synth}倍<1.80倍(見送り)"

    st = STAGES[stage_idx]
    budget = st["late"] if meta.race_num >= 9 else st["early"]
    tickets = allocate_bets(picked, budget)
    return BetOrder(meta=meta, bet_type=bet_type, tickets=tickets), f"{bet_type} {len(tickets)}点(合成{synth}倍)"


def is_in_bet_window(post_time: str, now_jst: datetime, ignore_time: bool = False) -> bool:
    """発走3〜5分前（テスト時はignore_time=Trueで全レース対象）"""
    if ignore_time:
        return True
    m = re.search(r"(\d{1,2}):(\d{2})", post_time or "")
    if not m:
        return False
    post_dt = now_jst.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    diff_min = (post_dt - now_jst).total_seconds() / 60.0
    return 2.0 <= diff_min <= 6.0


def main():
    parser = argparse.ArgumentParser(description="v11.9 中央(netkeiba+即PAT) 自動投票")
    parser.add_argument("--dry-run", action="store_true", help="購入直前で停止する安全モード")
    parser.add_argument("--ignore-time", action="store_true", help="発走時刻フィルタを無視して直近1Rを検証")
    args = parser.parse_args()

    now_jst = datetime.now(JST)
    ymd = now_jst.strftime("%Y%m%d")
    state = load_state()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        race_ids = fetch_today_jra_race_ids(page, ymd)
        print(f"📅 {ymd} JRA検出レース数: {len(race_ids)}R", flush=True)
        if not race_ids:
            print("ℹ️ 本日のJRA開催はありません。", flush=True)
            browser.close()
            return

        logged_in = False
        for rid in race_ids:
            meta, _ = fetch_netkeiba_race_and_odds(page, rid, bet_type="枠連")
            if not is_in_bet_window(meta.post_time, now_jst, ignore_time=args.ignore_time):
                continue

            fav1, has_waku = fetch_netkeiba_tan_fav1(page, rid)
            order, summary = build_v11_9_order(page, meta, fav1, has_waku, state["stage_idx"])

            race_title = f"{meta.venue} {meta.race_num}R"
            stage_name = STAGES[state["stage_idx"]]["name"]

            if not order:
                print(f"⏭️ [見送り] {race_title} ({meta.post_time}): {summary}", flush=True)
                continue

            print(f"🎯 [参戦対象] {race_title} ({meta.post_time}) {stage_name}: {summary} / 合計{order.total_amount:,}円", flush=True)

            if not logged_in:
                login_ipat(page)
                logged_in = True

            ok = execute_ipat_bet(page, order, dry_run=args.dry_run)
            if ok:
                state["race_logs"].append({
                    "timestamp": now_jst.strftime("%m/%d %H:%M"),
                    "race_title": race_title,
                    "bet_type": order.bet_type,
                    "stage_name": stage_name,
                    "combos_summary": summary,
                    "status": "PENDING" if not args.dry_run else "SKIP",
                    "total_bet": order.total_amount,
                    "payout": 0,
                    "result_pair": "-",
                })
                save_state(state)

            # テスト（時刻無視）時は1レース確認したら終了
            if args.ignore_time:
                break

        browser.close()


if __name__ == "__main__":
    main()
