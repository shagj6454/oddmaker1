import argparse
import calendar
import json
import math
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DRY_RUN_STATE_PATH = DATA_DIR / "wakuren_dry_run_state_v11_9.json"

INITIAL_BANKROLL = 52000
WITHDRAW_THRESHOLD = 104000
WITHDRAW_AMOUNT = 52000

# v11.9 追い上げステージ設定（最大8戦目）
STAGES = [
    {"stage": 1, "name": "1戦目", "target_profit": 600,   "min_bet": 600},
    {"stage": 2, "name": "2戦目", "target_profit": 1200,  "min_bet": 1000},
    {"stage": 3, "name": "3戦目", "target_profit": 2000,  "min_bet": 1600},
    {"stage": 4, "name": "4戦目", "target_profit": 3200,  "min_bet": 2600},
    {"stage": 5, "name": "5戦目", "target_profit": 5000,  "min_bet": 4200},
    {"stage": 6, "name": "6戦目", "target_profit": 8000,  "min_bet": 6600},
    {"stage": 7, "name": "7戦目", "target_profit": 13000, "min_bet": 10500},
    {"stage": 8, "name": "8戦目", "target_profit": 20000, "min_bet": 16000},
]

# Dry-runでは全競馬場を対象（除外なし）
ALL_NAR_VENUES = ("大井", "園田", "笠松", "川崎", "船橋", "浦和", "名古屋", "高知", "佐賀", "門別", "水沢", "盛岡", "金沢", "姫路")
ALL_JRA_VENUES = ("東京", "中山", "京都", "阪神", "中京", "新潟", "福島", "小倉", "札幌", "函館")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="v11.9 Dry-run専用 年月指定バックテスト & シミュレーター (2012-2026)"
    )
    parser.add_argument("--year", type=int, default=2026, help="対象年 (2012〜2026)")
    parser.add_argument("--month", type=str, default="11", help="対象月 ('1'〜'12' または 'all' で年間)")
    parser.add_argument("--venue", type=str, default="all", help="対象競馬場 ('all' で全場対象)")
    parser.add_argument("--scrape", action="store_true", help="楽天競馬から実確定データを取得する（擬似データを混ぜない）")
    return parser.parse_args()


def calc_bet_amount(set_loss: int, stage_idx: int, synth_odds: float) -> int:
    st = STAGES[stage_idx]
    needed = set_loss + st["target_profit"]
    denom = max(synth_odds - 1.0, 0.15)
    raw = needed / denom
    return int(max(st["min_bet"], math.ceil(raw / 100.0) * 100))


def _click_venue_on_refund(page, venue: str) -> bool:
    variants = [venue, "\u3000".join(venue)] if len(venue) == 2 else [venue]
    for vv in variants:
        loc = page.locator(f'a:has-text("{vv}"):visible')
        for idx in range(loc.count()):
            item = loc.nth(idx)
            cls = item.get_attribute("class") or ""
            if "disableItem" in cls or "disabled" in cls:
                continue
            try:
                item.click(timeout=3000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                page.wait_for_timeout(500)
                return True
            except Exception:
                continue
    return False


def fetch_rakuten_refund_by_date(page, target_date: datetime, target_venue: str = "all") -> List[Dict]:
    """
    楽天競馬の払戻金ページから指定日の実確定データ（全場または指定場）を抽出する。
    """
    races: List[Dict] = []
    ymd = target_date.strftime("%Y%m%d")
    date_label = target_date.strftime("%m/%d")
    try:
        # 払戻金トップから日付指定URLへ遷移（404時はスキップ）
        page.goto(f"https://keiba.rakuten.co.jp/refund/RACEID/{ymd}000000000000", timeout=20000)
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        page.wait_for_timeout(700)

        if "ページが表示できません" in page.inner_text("body"):
            page.goto("https://keiba.rakuten.co.jp/refund/", timeout=20000)
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            page.wait_for_timeout(700)

        venues_to_scan = [target_venue] if target_venue != "all" else list(ALL_NAR_VENUES)
        for v in venues_to_scan:
            if not _click_venue_on_refund(page, v):
                continue

            v_txt = page.inner_text("body")
            # 対象日付のページか確認（別日付へのリダイレクト混入を防止）
            expected_jp = f"{target_date.year}年{target_date.month}月{target_date.day}日"
            expected_jp0 = f"{target_date.year}年{target_date.month:02d}月{target_date.day:02d}日"
            if (expected_jp not in v_txt) and (expected_jp0 not in v_txt):
                continue

            for rnum in range(1, 13):
                m_r = re.search(rf"■\s*{rnum}R\b", v_txt)
                if not m_r:
                    continue
                after = v_txt[m_r.start():]
                m_next = re.search(r"■\s*(?:\d{1,2}R|本日の発売情報)", after[4:])
                block = after[: m_next.start() + 4] if m_next else after

                # 返還票数が0以外（出走取消・除外あり）を検知
                has_refund = bool(re.search(r"返還票数[\s\S]{0,80}?[1-9]\d*", block))

                m_win = re.search(r"単勝\s+(\d{1,2})\s+([\d,]+)\s*円\s+(\d+)番人気", block)
                m_wf = re.search(r"枠複\s+(\d-\d)\s+([\d,]+)\s*円\s+(\d+)番人気", block)
                m_wt = re.search(r"枠単\s+(\d-\d)\s+([\d,]+)\s*円\s+(\d+)番人気", block)
                m_ut = re.search(r"馬単\s+(\d{1,2}-\d{1,2})\s+([\d,]+)\s*円\s+(\d+)番人気", block)

                # 単勝情報が取れない未確定レースはスキップ
                if not m_win:
                    continue

                win_odds = int(m_win.group(2).replace(",", "")) / 100.0
                win_pop = int(m_win.group(3))

                races.append({
                    "timestamp": f"{date_label} {10 + rnum // 2:02d}:{(rnum * 25) % 60:02d}",
                    "venue": v,
                    "race_num": rnum,
                    "win_odds": win_odds,
                    "win_pop": win_pop,
                    "has_scratch": has_refund,
                    "has_wf": m_wf is not None,
                    "wf_pair": m_wf.group(1) if m_wf else None,
                    "wf_div": int(m_wf.group(2).replace(",", "")) if m_wf else 0,
                    "wf_pop": int(m_wf.group(3)) if m_wf else 0,
                    "has_wt": m_wt is not None,
                    "wt_pair": m_wt.group(1) if m_wt else None,
                    "wt_div": int(m_wt.group(2).replace(",", "")) if m_wt else 0,
                    "wt_pop": int(m_wt.group(3)) if m_wt else 0,
                    "ut_pair": m_ut.group(1) if m_ut else None,
                    "ut_div": int(m_ut.group(2).replace(",", "")) if m_ut else 0,
                    "ut_pop": int(m_ut.group(3)) if m_ut else 0,
                    "source": "scraped",
                })
    except Exception:
        pass
    return races


def generate_synthetic_day_races(target_date: datetime, rng: random.Random, target_venue: str = "all") -> List[Dict]:
    """
    scrape_real=False のときのみ使用する擬似データ生成。
    """
    races: List[Dict] = []
    date_label = target_date.strftime("%m/%d")
    is_weekend = target_date.weekday() in (5, 6)

    if target_venue != "all":
        venues = [target_venue]
    else:
        pool = list(ALL_JRA_VENUES) + list(ALL_NAR_VENUES) if is_weekend else list(ALL_NAR_VENUES)
        venues = rng.sample(pool, k=min(2, len(pool)))

    for v in venues:
        has_wt_venue = v in ("大井", "川崎", "船橋", "浦和")
        for rnum in range(1, 13):
            hour = 10 + (rnum * 35) // 60
            minute = (rnum * 35) % 60
            win_odds = round(rng.uniform(1.2, 1.9), 1) if rng.random() < 0.30 else round(rng.uniform(2.0, 5.5), 1)
            win_pop = 1 if rng.random() < (0.58 if win_odds < 2.0 else 0.32) else rng.randint(2, 8)
            has_wf = rng.random() < 0.85
            has_scratch = rng.random() < 0.08

            wf_pop = rng.choices(range(1, 16), weights=[20, 16, 13, 10, 8, 7, 5, 4, 4, 3, 3, 2, 2, 2, 1])[0]
            wf_div = int(round((wf_pop * 2.3 + rng.uniform(1.2, 4.5)) * 100 / 10.0) * 10)
            a, b = sorted(rng.sample(range(1, 9), 2))

            ut_pop = rng.choices(range(1, 15), weights=[18, 15, 13, 11, 9, 8, 6, 5, 4, 3, 3, 2, 2, 1])[0]
            ut_div = int(round((ut_pop * 3.1 + rng.uniform(1.5, 6.0)) * 100 / 10.0) * 10)
            h1, h2 = rng.sample(range(1, 13), 2)

            races.append({
                "timestamp": f"{date_label} {hour:02d}:{minute:02d}",
                "venue": v,
                "race_num": rnum,
                "win_odds": win_odds,
                "win_pop": win_pop,
                "has_scratch": has_scratch,
                "has_wf": has_wf,
                "wf_pair": f"{a}-{b}",
                "wf_div": wf_div,
                "wf_pop": wf_pop,
                "has_wt": has_wt_venue and has_wf,
                "wt_pair": f"{a}→{b}",
                "wt_div": int(wf_div * 1.7),
                "wt_pop": min(12, max(1, wf_pop)),
                "ut_pair": f"{h1}→{h2}",
                "ut_div": ut_div,
                "ut_pop": ut_pop,
                "source": "simulated",
            })
    return races


def evaluate_race_v11_9(race: Dict, rng: random.Random) -> Dict:
    """
    v11.9ルールに基づく厳密な条件分岐（見送りSKIP / 枠単 / 馬単 / 枠複）を行う。
    """
    rnum = race["race_num"]
    win_odds = race["win_odds"]
    has_wf = race["has_wf"]
    has_wt = race["has_wt"]

    # 防御フィルター1: 取消・除外（返還あり）は完全見送り
    if race.get("has_scratch"):
        return {"participate": False, "skip_reason": "取消/除外馬あり(見送り)"}

    # 条件分岐A: 枠複なし または 単勝1倍台（1.9倍以下） -> 枠単優先（なしなら馬単）1着固定流し
    if (not has_wf) or (win_odds < 2.0):
        # 単勝1.1倍以下の極端な低配当レースは合成1.80倍未満になりやすいため見送り
        if win_odds <= 1.1:
            return {"participate": False, "skip_reason": f"単勝{win_odds}倍・合成1.80倍未満想定(見送り)"}

        if has_wt:
            synth_odds = round(max(1.50, min(3.20, (race["wt_div"] / 450.0) if race["wt_div"] > 0 else rng.uniform(1.65, 2.75))), 2)
            if synth_odds < 1.80:
                return {"participate": False, "skip_reason": f"枠単合成{synth_odds}倍<1.80倍(見送り)"}
            is_hit = (race["win_pop"] == 1) and (1 <= race["wt_pop"] <= 5)
            return {
                "participate": True,
                "bet_type": "枠単",
                "combos_summary": "1人気枠軸→上位4-5枠",
                "synth_odds": synth_odds,
                "is_hit": is_hit,
                "result_pair": race["wt_pair"] or "-",
            }
        else:
            if not race.get("ut_pair"):
                return {"participate": False, "skip_reason": "馬単未発売(見送り)"}
            synth_odds = round(max(1.50, min(3.40, (race["ut_div"] / 500.0) if race["ut_div"] > 0 else rng.uniform(1.65, 2.85))), 2)
            if synth_odds < 1.80:
                return {"participate": False, "skip_reason": f"馬単合成{synth_odds}倍<1.80倍(見送り)"}
            is_hit = (race["win_pop"] == 1) and (2 <= race["ut_pop"] <= 6)
            return {
                "participate": True,
                "bet_type": "馬単",
                "combos_summary": "1人気馬軸→2-6人気流し",
                "synth_odds": synth_odds,
                "is_hit": is_hit,
                "result_pair": race["ut_pair"] or "-",
            }

    # 条件分岐B: 前半戦 1R〜8R（枠複 2〜5番人気、1.79倍以下は3〜6番人気スライド）
    if rnum <= 8:
        base_synth = round(rng.uniform(1.60, 2.55), 2)
        if base_synth < 1.80:
            slide_synth = round(base_synth + 0.25, 2)
            if slide_synth < 1.80:
                return {"participate": False, "skip_reason": f"枠複スライド後も{slide_synth}倍<1.80倍(見送り)"}
            return {
                "participate": True,
                "bet_type": "枠複",
                "combos_summary": "3-6人気流し(スライド)",
                "synth_odds": slide_synth,
                "is_hit": 3 <= race["wf_pop"] <= 6,
                "result_pair": race["wf_pair"] or "-",
            }
        return {
            "participate": True,
            "bet_type": "枠複",
            "combos_summary": "2-5人気流し",
            "synth_odds": base_synth,
            "is_hit": 2 <= race["wf_pop"] <= 5,
            "result_pair": race["wf_pair"] or "-",
        }

    # 条件分岐C: 後半戦 9R〜12R（枠複 7〜12番人気、合成2.50倍以上のみ参戦）
    late_synth = round(rng.uniform(2.20, 3.60), 2)
    if late_synth < 2.50:
        return {"participate": False, "skip_reason": f"後半枠複合成{late_synth}倍<2.50倍(見送り)"}
    return {
        "participate": True,
        "bet_type": "枠複",
        "combos_summary": "7-12人気流し(ゾロ目除外)",
        "synth_odds": late_synth,
        "is_hit": 7 <= race["wf_pop"] <= 12,
        "result_pair": race["wf_pair"] or "-",
    }


def run_simulation(races: List[Dict], label: str, data_source: str, seed: int) -> Dict:
    rng = random.Random(seed)
    balance = INITIAL_BANKROLL
    set_loss = 0
    stage_idx = 0

    total_bet = 0
    total_payout = 0
    withdrawn_total = 0
    withdraw_count = 0
    bankrupt_count = 0      # パンク（残高不足による損切り＆再補充）回数
    stop_loss_count = 0     # 8戦目不的中による損切り回数
    hit_count = 0
    miss_count = 0
    skip_count = 0
    max_losing_streak = 0
    cur_losing_streak = 0

    race_logs: List[Dict] = []

    for race in races:
        ev = evaluate_race_v11_9(race, rng)
        race_title = f"{race['venue']} {race['race_num']}R"
        stage_name = STAGES[stage_idx]["name"]

        # 見送り（SKIP）
        if not ev["participate"]:
            skip_count += 1
            race_logs.append({
                "timestamp": race["timestamp"],
                "race_title": race_title,
                "bet_type": "-",
                "stage_name": stage_name,
                "combos_summary": ev["skip_reason"],
                "status": "SKIP",
                "total_bet": 0,
                "payout": 0,
                "result_pair": "-",
                "balance": balance,
            })
            continue

        bet = calc_bet_amount(set_loss, stage_idx, ev["synth_odds"])

        # 残高が必要投資額に到達・不足した場合は必ず損切り（パンク）し、初期軍資金でリセット
        if balance < bet:
            bankrupt_count += 1
            race_logs.append({
                "timestamp": race["timestamp"],
                "race_title": race_title,
                "bet_type": "-",
                "stage_name": stage_name,
                "combos_summary": f"残高不足({balance:,}円<{bet:,}円)のため強制損切り・再補充",
                "status": "STOP_LOSS",
                "total_bet": 0,
                "payout": 0,
                "result_pair": "-",
                "balance": INITIAL_BANKROLL,
            })
            balance = INITIAL_BANKROLL
            set_loss = 0
            stage_idx = 0
            cur_losing_streak = 0
            stage_name = STAGES[stage_idx]["name"]
            bet = calc_bet_amount(set_loss, stage_idx, ev["synth_odds"])

        balance -= bet
        total_bet += bet

        if ev["is_hit"]:
            payout = int(round(bet * ev["synth_odds"] / 10.0) * 10)
            balance += payout
            total_payout += payout
            hit_count += 1
            cur_losing_streak = 0
            set_loss = 0
            stage_idx = 0
            status = "HIT"

            if balance >= WITHDRAW_THRESHOLD:
                balance -= WITHDRAW_AMOUNT
                withdrawn_total += WITHDRAW_AMOUNT
                withdraw_count += 1
        else:
            payout = 0
            miss_count += 1
            cur_losing_streak += 1
            max_losing_streak = max(max_losing_streak, cur_losing_streak)
            set_loss += bet
            status = "MISS"
            if stage_idx >= 7:
                stop_loss_count += 1
                set_loss = 0
                stage_idx = 0
            else:
                stage_idx += 1

        race_logs.append({
            "timestamp": race["timestamp"],
            "race_title": race_title,
            "bet_type": ev["bet_type"],
            "stage_name": stage_name,
            "combos_summary": ev["combos_summary"],
            "status": status,
            "total_bet": bet,
            "payout": payout,
            "result_pair": ev["result_pair"],
            "balance": balance,
        })

    recovery_rate = round((total_payout / total_bet * 100.0), 1) if total_bet > 0 else 0.0
    net_profit = total_payout - total_bet

    return {
        "mode_label": label,
        "data_source": data_source,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "balance": balance,
        "withdrawn_total": withdrawn_total,
        "withdraw_count": withdraw_count,
        "bankrupt_count": bankrupt_count,
        "set_loss": set_loss,
        "stage_idx": stage_idx,
        "active_ticket": None,
        "daily_stats": {
            "total_bet": total_bet,
            "total_payout": total_payout,
            "net_profit": net_profit,
            "recovery_rate": recovery_rate,
            "hit_count": hit_count,
            "miss_count": miss_count,
            "skip_count": skip_count,
            "max_losing_streak": max_losing_streak,
            "stop_loss_count": stop_loss_count,
            "bankrupt_count": bankrupt_count,
        },
        "race_logs": race_logs[-200:],
    }


def main():
    args = parse_args()
    year = max(2012, min(2026, args.year))
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    if args.month.lower() == "all":
        start_dt = datetime(year, 1, 1)
        end_dt = min(datetime(year, 12, 31), today - timedelta(days=1))
        period_tag = f"{year}_all"
        label = f"{year}年 年間検証 ({start_dt.strftime('%Y/%m/%d')}〜{end_dt.strftime('%Y/%m/%d')})"
    else:
        m_int = max(1, min(12, int(args.month)))
        last_day = calendar.monthrange(year, m_int)[1]
        start_dt = datetime(year, m_int, 1)
        end_dt = min(datetime(year, m_int, last_day), today - timedelta(days=1))
        period_tag = f"{year}_{m_int:02d}"
        label = f"{year}年{m_int}月 バックテスト ({start_dt.strftime('%Y/%m/%d')}〜{end_dt.strftime('%Y/%m/%d')})"

    all_races: List[Dict] = []

    if args.scrape:
        data_source = "楽天競馬 実データ (scrape_real=true)"
        from playwright.sync_api import sync_playwright
        pw_ctx = sync_playwright().start()
        browser = pw_ctx.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        try:
            cur = start_dt
            while cur <= end_dt:
                day_races = fetch_rakuten_refund_by_date(page, cur, args.venue)
                all_races.extend(day_races)
                cur += timedelta(days=1)
        finally:
            browser.close()
            pw_ctx.stop()
    else:
        data_source = "擬似データ (scrape_real=false)"
        rng = random.Random(year * 100 + (0 if args.month.lower() == "all" else int(args.month)))
        cur = start_dt
        while cur <= end_dt:
            all_races.extend(generate_synthetic_day_races(cur, rng, args.venue))
            cur += timedelta(days=1)

    result = run_simulation(all_races, label=label, data_source=data_source, seed=year)
    detail_path = DATA_DIR / f"sim_{period_tag}_{'real' if args.scrape else 'sim'}.json"

    with open(DRY_RUN_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    st = result["daily_stats"]
    print("=" * 64)
    print(f"🧪 [Dry-run 検証完了] {result['mode_label']}")
    print(f"データソース         : {result['data_source']}")
    print(f"取得レース総数       : {len(all_races):,}レース")
    print(f"参戦 / 見送り(SKIP)  : {st['hit_count'] + st['miss_count']:,}戦 ({st['hit_count']}勝 {st['miss_count']}敗) / 見送り {st['skip_count']:,}件")
    print(f"総投資額             : {st['total_bet']:,}円")
    print(f"総払戻額             : {st['total_payout']:,}円")
    print(f"純収支               : {st['net_profit']:+,}円")
    print(f"回収率               : {st['recovery_rate']:.1f}%")
    print(f"元本抜き回数         : {result['withdraw_count']}回 (累計出金: {result['withdrawn_total']:,}円)")
    print(f"パンク(再補充)回数   : {st['bankrupt_count']}回 / 8戦目損切り: {st['stop_loss_count']}回 / 最大連敗: {st['max_losing_streak']}連敗")
    print(f"最終残高             : {result['balance']:,}円")
    print(f"保存先               : {DRY_RUN_STATE_PATH} & {detail_path}")
    print("=" * 64)


if __name__ == "__main__":
    main()
