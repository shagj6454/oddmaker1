import argparse
import calendar
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DRY_RUN_STATE_PATH = DATA_DIR / "wakuren_dry_run_state_v11_9.json"
CACHE_INDEX_JSON = DATA_DIR / "cache_index.json"

INITIAL_BANKROLL = 52000
WITHDRAW_THRESHOLD = 104000
WITHDRAW_AMOUNT = 52000
MAX_RUN_HISTORY = 10

STAGES = [
    {"name": "1戦目", "early_bet": 700,   "late_bet": 500},
    {"name": "2戦目", "early_bet": 1300,  "late_bet": 900},
    {"name": "3戦目", "early_bet": 2100,  "late_bet": 1500},
    {"name": "4戦目", "early_bet": 3700,  "late_bet": 2600},
    {"name": "5戦目", "early_bet": 6300,  "late_bet": 4400},
    {"name": "6戦目", "early_bet": 10600, "late_bet": 7500},
    {"name": "7戦目", "early_bet": 18100, "late_bet": 12800},
    {"name": "8戦目", "early_bet": 21800, "late_bet": 21800},
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="総当たり検証（25パターン・追い上げ回収率ランキング）")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--month", type=str, default="7")
    p.add_argument("--week", type=str, default="all")
    p.add_argument("--venue", type=str, default="all")
    p.add_argument("--run-id", type=str, default="")
    p.add_argument("--no-cache", action="store_true")
    return p.parse_args()


def calc_synth(odds_list: List[float]) -> float:
    valid = [o for o in odds_list if o > 0]
    if not valid:
        return 0.0
    inv = sum(1.0 / o for o in valid)
    return round(1.0 / inv, 2) if inv > 0 else 0.0


def _adjust_synth(pool: List[Dict], init_n: int, lo: float, hi: float) -> Tuple[List[Dict], float]:
    if not pool:
        return [], 0.0
    picked = list(pool[:init_n])
    s = calc_synth([c["odds"] for c in picked])
    idx = len(picked)
    while s > hi and idx < len(pool):
        cand = picked + [pool[idx]]
        cs = calc_synth([c["odds"] for c in cand])
        if cs < lo:
            break
        picked, s = cand, cs
        idx += 1
    while s < lo and len(picked) > 1:
        picked.pop()
        s = calc_synth([c["odds"] for c in picked])
    return picked, s


def _find_gap_idx(combos: List[Dict], ratio: float) -> int:
    for i in range(1, min(len(combos), 12)):
        prev_o = combos[i - 1]["odds"]
        cur_o = combos[i]["odds"]
        if prev_o > 0 and (cur_o / prev_o) >= ratio:
            return i
    return -1


def _make_pick(race: Dict, picked: List[Dict], min_synth: float = 1.50, label: str = "") -> Dict:
    if not picked:
        return {"participate": False, "skip_reason": "対象なし"}
    synth = calc_synth([c["odds"] for c in picked])
    if synth < min_synth:
        return {"participate": False, "skip_reason": f"実合成{synth}倍<{min_synth}倍"}
    win_str = race.get("wf_pair") or ""
    is_hit = False
    if "-" in win_str:
        a_s, b_s = win_str.split("-")
        w1, w2 = min(int(a_s), int(b_s)), max(int(a_s), int(b_s))
        is_hit = any(c["w1"] == w1 and c["w2"] == w2 for c in picked)
    pops = ",".join(str(c["pop"]) for c in picked[:6]) + (".." if len(picked) > 6 else "")
    return {
        "participate": True,
        "bet_type": "枠複",
        "combos_summary": f"{label}[{pops}人気]({len(picked)}点/合成{synth}倍)",
        "synth_odds": synth,
        "is_hit": is_hit,
        "result_pair": win_str or "-",
    }


def build_strategies() -> List[Tuple[str, Callable[[Dict], Dict]]]:
    def wf_only(r: Dict) -> Optional[List[Dict]]:
        if r.get("has_scratch") or r.get("odds_kind") != "wakufuku":
            return None
        c = r.get("real_odds") or []
        return c if len(c) >= 6 else None

    def by_pops(pops: List[int], no_zoro: bool = False, min_s: float = 1.50, fav1_min: float = 0.0, r_lo: int = 1, r_hi: int = 12):
        def fn(r: Dict) -> Dict:
            combos = wf_only(r)
            if not combos or not (r_lo <= r["race_num"] <= r_hi):
                return {"participate": False, "skip_reason": "対象外"}
            fav1 = float(r.get("fav1_win_odds") or r.get("win_odds") or 0.0)
            if fav1_min > 0 and fav1 < fav1_min:
                return {"participate": False, "skip_reason": "堅いレース見送り"}
            src = [c for c in combos if c["w1"] != c["w2"]] if no_zoro else combos
            picked = [c for c in src if c["pop"] in pops]
            return _make_pick(r, picked, min_synth=min_s)
        return fn

    def zoro_only(max_pop: int):
        def fn(r: Dict) -> Dict:
            combos = wf_only(r)
            if not combos:
                return {"participate": False, "skip_reason": "対象外"}
            picked = [c for c in combos if c["w1"] == c["w2"] and c["pop"] <= max_pop]
            return _make_pick(r, picked, min_synth=1.80, label="ゾロ目のみ")
        return fn

    def zoro_plus(top_n: int, start_pop: int):
        def fn(r: Dict) -> Dict:
            combos = wf_only(r)
            if not combos:
                return {"participate": False, "skip_reason": "対象外"}
            zoros = [c for c in combos if c["w1"] == c["w2"] and c["pop"] <= 10]
            if not zoros:
                return {"participate": False, "skip_reason": "上位ゾロ目なし"}
            others = [c for c in combos if c["w1"] != c["w2"] and c["pop"] >= start_pop][:top_n]
            return _make_pick(r, zoros[:2] + others, min_synth=1.80, label="ゾロ目逆張り")
        return fn

    def gap_strat(ratio: float, after: bool, n_pick: int):
        def fn(r: Dict) -> Dict:
            combos = wf_only(r)
            if not combos:
                return {"participate": False, "skip_reason": "対象外"}
            gi = _find_gap_idx(combos, ratio)
            if gi < 1:
                return {"participate": False, "skip_reason": "断層なし"}
            picked = combos[gi : gi + n_pick] if after else combos[max(0, gi - n_pick) : gi]
            return _make_pick(r, picked, min_synth=1.60, label=f"断層{'直後' if after else '直前'}")
        return fn

    def synth_band(start_pop: int, init_n: int, lo: float, hi: float):
        def fn(r: Dict) -> Dict:
            combos = wf_only(r)
            if not combos:
                return {"participate": False, "skip_reason": "対象外"}
            pool = [c for c in combos if c["pop"] >= start_pop]
            picked, _ = _adjust_synth(pool, init_n, lo, hi)
            return _make_pick(r, picked, min_synth=lo, label=f"合成{lo}-{hi}")
        return fn

    return [
        ("01_変則_2,4,5,6,7,8人気(ゾロ含)", by_pops([2, 4, 5, 6, 7, 8], False, 1.50)),
        ("02_変則_2,4,5,6,7,8人気(ゾロ除)", by_pops([2, 4, 5, 6, 7, 8], True, 1.50)),
        ("03_奇数人気のみ_1,3,5,7,9", by_pops([1, 3, 5, 7, 9], False, 1.50)),
        ("04_偶数人気のみ_2,4,6,8,10", by_pops([2, 4, 6, 8, 10], False, 1.50)),
        ("05_中飛ばし_1,4,5,6,7人気", by_pops([1, 4, 5, 6, 7], False, 1.50)),
        ("06_二極化_2,3,7,8,9人気", by_pops([2, 3, 7, 8, 9], False, 1.50)),
        ("07_一撃混在_3,4,5,6,12人気", by_pops([3, 4, 5, 6, 12], False, 1.50)),
        ("08_堅実上位_1-3人気", by_pops([1, 2, 3], False, 1.30)),
        ("09_基準_2-6人気(ゾロ含)", by_pops([2, 3, 4, 5, 6], False, 1.50)),
        ("10_中穴_4-8人気(5点)", by_pops([4, 5, 6, 7, 8], False, 1.80)),
        ("11_中穴_5-10人気(6点)", by_pops([5, 6, 7, 8, 9, 10], False, 2.00)),
        ("12_大穴_6-12人気(7点)", by_pops([6, 7, 8, 9, 10, 11, 12], False, 2.50)),
        ("13_超大穴_7-14人気(8点)", by_pops([7, 8, 9, 10, 11, 12, 13, 14], False, 3.00)),
        ("14_ゾロ目のみ(1-10人気内)", zoro_only(10)),
        ("15_ゾロ目のみ(1-15人気内)", zoro_only(15)),
        ("16_ゾロ目逆張り(ゾロ+2-5人気)", zoro_plus(4, 2)),
        ("17_ゾロ目中穴(ゾロ+5-8人気)", zoro_plus(4, 5)),
        ("18_断層直前3点(1.4倍差)", gap_strat(1.4, False, 3)),
        ("19_断層直後4点(1.4倍差)", gap_strat(1.4, True, 4)),
        ("20_断層直後5点(1.6倍差)", gap_strat(1.6, True, 5)),
        ("21_混戦限定(単2.5倍+)_2-6人気", by_pops([2, 3, 4, 5, 6], False, 1.50, fav1_min=2.5)),
        ("22_混戦限定(単2.5倍+)_4-9人気", by_pops([4, 5, 6, 7, 8, 9], False, 1.80, fav1_min=2.5)),
        ("23_高合成3.0-5.0倍(3人気~)", synth_band(3, 4, 3.0, 5.0)),
        ("24_高合成4.0-7.0倍(5人気~)", synth_band(5, 5, 4.0, 7.0)),
        ("25_後半9-12R限定_4-10人気", by_pops([4, 5, 6, 7, 8, 9, 10], False, 2.00, r_lo=9, r_hi=12)),
    ]


def simulate_one(races: List[Dict], strat_name: str, eval_fn: Callable[[Dict], Dict]) -> Dict:
    balance, stage_idx = INITIAL_BANKROLL, 0
    total_bet = total_payout = withdrawn = w_cnt = stop_loss = hits = misses = skips = 0
    max_ls = cur_ls = 0
    logs: List[Dict] = []

    for r in races:
        ev = eval_fn(r)
        st_name = STAGES[stage_idx]["name"]
        title = f"{r['venue']} {r['race_num']}R"
        if not ev["participate"]:
            skips += 1
            continue

        st = STAGES[stage_idx]
        bet = st["late_bet"] if r["race_num"] >= 9 else st["early_bet"]
        balance -= bet
        total_bet += bet

        if ev["is_hit"]:
            payout = int(round(bet * ev["synth_odds"] / 10.0) * 10)
            balance += payout
            total_payout += payout
            hits += 1
            cur_ls = 0
            stage_idx = 0
            status = "HIT"
            if balance >= WITHDRAW_THRESHOLD:
                balance -= WITHDRAW_AMOUNT
                withdrawn += WITHDRAW_AMOUNT
                w_cnt += 1
        else:
            payout = 0
            misses += 1
            cur_ls += 1
            max_ls = max(max_ls, cur_ls)
            if stage_idx >= 7:
                stop_loss += 1
                stage_idx = 0
                status = "STOP_LOSS"
            else:
                stage_idx += 1
                status = "MISS"

        logs.append({
            "timestamp": r["timestamp"], "race_title": title, "bet_type": ev["bet_type"],
            "stage_name": st_name, "combos_summary": ev["combos_summary"], "status": status,
            "total_bet": bet, "payout": payout, "result_pair": ev["result_pair"], "balance": balance,
        })

    roi = round((total_payout / total_bet * 100.0), 1) if total_bet > 0 else 0.0
    hr = round((hits / (hits + misses) * 100.0), 1) if (hits + misses) > 0 else 0.0
    return {
        "name": strat_name, "races": hits + misses, "hits": hits, "misses": misses, "skips": skips,
        "hit_rate": hr, "total_bet": total_bet, "total_payout": total_payout,
        "net_profit": total_payout - total_bet, "roi": roi, "balance": balance,
        "withdrawn": withdrawn, "withdraw_count": w_cnt,
        "stop_loss": stop_loss, "max_ls": max_ls, "logs": logs[-120:],
    }


def main():
    args = parse_args()
    period_tag = f"{args.year}_all" if args.month.lower() == "all" else f"{args.year}_{int(args.month):02d}"
    cache_path = DATA_DIR / f"raw_cache_{period_tag}_{args.venue}.json"

    if not cache_path.exists():
        print(f"❌ キャッシュが見つかりません: {cache_path.name}（先にキャッシュ済み年月を指定してください）", flush=True)
        sys.exit(1)

    with open(cache_path, "r", encoding="utf-8") as f:
        all_races = json.load(f)

    # 週指定がある場合は第4週目（またはall）だけ本実行し、1-3週目は高速スキップ
    if args.week in ("1", "2", "3"):
        print(f"⚡ [総当たりモード] 第{args.week}週はスキップし、第4週で全期間一括検証します。", flush=True)
        sys.exit(0)

    results = [simulate_one(all_races, name, fn) for name, fn in build_strategies()]
    results.sort(key=lambda x: (x["roi"], x["net_profit"]), reverse=True)

    print("=" * 84, flush=True)
    print(f"🏆 [総当たり検証 追い上げ回収率ランキング] 対象: {cache_path.name} ({len(all_races):,}R)", flush=True)
    print(f"{'順位':<4} {'回収率':>7} {'収支':>11} {'的中率':>7} {'参戦':>5} {'8戦損切':>6} {'最大連敗':>6}  パターン名", flush=True)
    print("-" * 84, flush=True)
    for idx, res in enumerate(results, 1):
        print(
            f"{idx:2d}位 {res['roi']:6.1f}% {res['net_profit']:+10,d}円 {res['hit_rate']:6.1f}% "
            f"{res['races']:4d}戦 {res['stop_loss']:5d}回 {res['max_ls']:5d}連  {res['name']}",
            flush=True,
        )
    print("=" * 84, flush=True)

    # 1位のパターンをDry-run JSONへ保存（index.htmlにも反映）
    best = results[0]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_id = args.run_id.strip() or datetime.now().strftime("grid_%Y%m%d_%H%M%S")
    cached_items = []
    if CACHE_INDEX_JSON.exists():
        try:
            cached_items = json.load(open(CACHE_INDEX_JSON, "r", encoding="utf-8")).get("cached_items", [])
        except Exception:
            pass

    prog_stats = {
        f"TOP{i+1}: {r['name']}": {"races": r["races"], "hits": r["hits"], "bet": r["total_bet"], "payout": r["total_payout"]}
        for i, r in enumerate(results[:10])
    }

    entry = {
        "run_id": run_id, "executed_at": now_str,
        "year": args.year, "month": "all" if args.month.lower() == "all" else f"{int(args.month):02d}",
        "week_label": "総当たり25種", "venue": args.venue,
        "mode_label": f"{args.year}年{args.month}月 総当たり1位 [{best['name']}]",
        "data_source": f"🟢 キャッシュ総当たり ({cache_path.name})",
        "total_races_fetched": len(all_races),
        "balance": best["balance"], "withdrawn_total": best["withdrawn"], "withdraw_count": best["withdraw_count"],
        "daily_stats": {
            "total_bet": best["total_bet"], "total_payout": best["total_payout"],
            "net_profit": best["net_profit"], "recovery_rate": best["roi"],
            "hit_count": best["hits"], "miss_count": best["misses"], "skip_count": best["skips"],
            "max_losing_streak": best["max_ls"], "stop_loss_count": best["stop_loss"], "bankrupt_count": 0,
        },
        "prog_stats": prog_stats,
        "race_logs": best["logs"],
    }

    prev_hist = []
    if DRY_RUN_STATE_PATH.exists():
        try:
            old = json.load(open(DRY_RUN_STATE_PATH, "r", encoding="utf-8"))
            prev_hist = old.get("run_history", []) if isinstance(old.get("run_history"), list) else []
        except Exception:
            pass
    new_hist = [h for h in prev_hist if h.get("run_id") != run_id]
    new_hist.insert(0, entry)

    out = {
        **entry, "updated_at": now_str, "bankrupt_count": 0, "set_loss": 0, "stage_idx": 0,
        "active_ticket": None, "cached_items": cached_items, "run_history": new_hist[:MAX_RUN_HISTORY],
    }
    with open(DRY_RUN_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
