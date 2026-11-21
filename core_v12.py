import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

PROD_STATE_PATH = DATA_DIR / "wakuren_state_v11_9.json"
DRY_RUN_STATE_PATH = DATA_DIR / "wakuren_dry_run_state_v11_9.json"
CACHE_INDEX_JSON = DATA_DIR / "cache_index.json"
CACHE_LIST_MD = DATA_DIR / "CACHE_LIST.md"

INITIAL_BANKROLL = 52000
WITHDRAW_THRESHOLD = 104000
WITHDRAW_AMOUNT = 52000
MAX_RUN_HISTORY = 10

EXCLUDED_VENUES = ("金沢", "笠松", "帯広", "帯広ば", "ばんえい")

# v12 追い上げテーブル（1〜8戦目・8戦目損切りリセット）
STAGES = [
    {"stage": 1, "name": "1戦目", "target": 1350,  "early_bet": 700,   "late_bet": 500},
    {"stage": 2, "name": "2戦目", "target": 2400,  "early_bet": 1300,  "late_bet": 900},
    {"stage": 3, "name": "3戦目", "target": 4050,  "early_bet": 2100,  "late_bet": 1500},
    {"stage": 4, "name": "4戦目", "target": 7000,  "early_bet": 3700,  "late_bet": 2600},
    {"stage": 5, "name": "5戦目", "target": 11900, "early_bet": 6300,  "late_bet": 4400},
    {"stage": 6, "name": "6戦目", "target": 20250, "early_bet": 10600, "late_bet": 7500},
    {"stage": 7, "name": "7戦目", "target": 34500, "early_bet": 18100, "late_bet": 12800},
    {"stage": 8, "name": "8戦目", "target": 58800, "early_bet": 21800, "late_bet": 21800},
]


def normalize_text(s: str) -> str:
    s = re.sub(r"[\s\u3000]+", "", s or "")
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    return s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def parse_hhmm_to_minutes(hhmm: str) -> Optional[int]:
    m = re.search(r"(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


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
        pair_str = c.get("pair") or f"{c['w1']}-{c['w2']}"
        tickets.append((pair_str, amt))
    return tickets


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
    for i in range(1, min(len(combos), 14)):
        prev_o, cur_o = combos[i - 1]["odds"], combos[i]["odds"]
        if prev_o > 0 and (cur_o / prev_o) >= ratio:
            return i
    return -1


def _make_pick(race: Dict, picked: List[Dict], min_synth: float = 1.20, label: str = "", bet_label: str = "枠複") -> Dict:
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
        "bet_type": bet_label,
        "combos_summary": f"{label}[{pops}人気]({len(picked)}点/合成{synth}倍)",
        "synth_odds": synth,
        "is_hit": is_hit,
        "result_pair": win_str or "-",
        "picked": picked,
    }


def evaluate_v12_standard(race: Dict, bet_label: str = "枠連") -> Dict:
    """
    v12 標準ルール（中央・地方共通）:
      - 除外競馬場 / 障害戦 / 取消・除外馬あり -> 見送り
      - 前半 1-8R: 2〜6番人気（ゾロ目除外・5点確保・合成1.80倍+）
      - 後半 9-12R: 3〜8番人気（ゾロ目除外・6点確保・合成1.80倍+）
    """
    if any(ex in str(race.get("venue", "")) for ex in EXCLUDED_VENUES) or ("障害" in str(race.get("race_name", ""))):
        return {"participate": False, "skip_reason": "除外競馬場/障害戦(見送り)"}
    if race.get("has_scratch"):
        return {"participate": False, "skip_reason": "取消/除外馬あり(見送り)"}

    combos = race.get("real_odds") or []
    if len(combos) < 6:
        return {"participate": False, "skip_reason": "オッズ未取得(見送り)"}

    non_zoro = [c for c in combos if c["w1"] != c["w2"]]
    rnum = int(race["race_num"])
    if rnum <= 8:
        picked = [c for c in non_zoro if c["pop"] >= 2][:5]
        bucket = f"前半1-8R({bet_label} 2-6人気)"
    else:
        picked = [c for c in non_zoro if c["pop"] >= 3][:6]
        bucket = f"後半9-12R({bet_label} 3-8人気)"

    res = _make_pick(race, picked, min_synth=1.80, label=bet_label, bet_label=bet_label)
    if res["participate"]:
        res["bucket"] = bucket
    return res


def build_100_strategies(bet_label: str = "枠複") -> List[Tuple[str, Callable[[Dict], Dict]]]:
    """中央・地方共通の100パターン総当たりロジック"""
    strats: List[Tuple[str, Callable[[Dict], Dict]]] = []

    def wf(r: Dict) -> Optional[List[Dict]]:
        if r.get("has_scratch") or ("障害" in str(r.get("race_name", ""))):
            return None
        if r.get("odds_kind") not in (None, "wakufuku", "wakuren"):
            return None
        c = r.get("real_odds") or []
        return c if len(c) >= 6 else None

    def by_pops(pops: List[int], no_zoro=False, min_s=1.40, f1_min=0.0, f1_max=999.0, r_lo=1, r_hi=12):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c or not (r_lo <= int(r["race_num"]) <= r_hi):
                return {"participate": False, "skip_reason": "対象外"}
            f1 = float(r.get("fav1_win_odds") or r.get("win_odds") or 0.0)
            if f1_min > 0 and not (f1_min <= f1 <= f1_max):
                return {"participate": False, "skip_reason": "単勝条件外"}
            src = [x for x in c if x["w1"] != x["w2"]] if no_zoro else c
            return _make_pick(r, [x for x in src if x["pop"] in pops], min_synth=min_s, bet_label=bet_label)
        return fn

    def zoro_fn(mode: str, param1: int, param2: int = 0):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c:
                return {"participate": False, "skip_reason": "対象外"}
            zoros = [x for x in c if x["w1"] == x["w2"] and x["pop"] <= param1]
            if not zoros:
                return {"participate": False, "skip_reason": "ゾロ目なし"}
            if mode == "only":
                return _make_pick(r, zoros, min_synth=1.50, label="ゾロ目のみ", bet_label=bet_label)
            others = [x for x in c if x["w1"] != x["w2"] and x["pop"] >= param2][:3]
            return _make_pick(r, zoros[:2] + others, min_synth=1.60, label="ゾロ目+", bet_label=bet_label)
        return fn

    def gap_fn(ratio: float, after: bool, n_pick: int):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c:
                return {"participate": False, "skip_reason": "対象外"}
            gi = _find_gap_idx(c, ratio)
            if gi < 1:
                return {"participate": False, "skip_reason": "断層なし"}
            picked = c[gi : gi + n_pick] if after else c[max(0, gi - n_pick) : gi]
            return _make_pick(r, picked, min_synth=1.50, label=f"断層{'後' if after else '前'}", bet_label=bet_label)
        return fn

    def synth_fn(start_pop: int, init_n: int, lo: float, hi: float, no_zoro=False):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c:
                return {"participate": False, "skip_reason": "対象外"}
            src = [x for x in c if x["w1"] != x["w2"]] if no_zoro else c
            picked, _ = _adjust_synth([x for x in src if x["pop"] >= start_pop], init_n, lo, hi)
            return _make_pick(r, picked, min_synth=lo, label=f"合成{lo}-{hi}", bet_label=bet_label)
        return fn

    def custom_fn(mode: str, p1: float, p2: float):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c:
                return {"participate": False, "skip_reason": "対象外"}
            if mode == "odds_range":
                picked = [x for x in c if p1 <= x["odds"] <= p2][:6]
            elif mode == "inner":
                picked = [x for x in c if x["w1"] <= 4 and x["w2"] <= 4][:int(p1)]
            elif mode == "outer":
                picked = [x for x in c if x["w1"] >= 5 and x["w2"] >= 5][:int(p1)]
            elif mode == "waku_in":
                picked = [x for x in c if x["w1"] == int(p1) or x["w2"] == int(p1)][:int(p2)]
            else:
                picked = []
            return _make_pick(r, picked, min_synth=1.50, label=mode, bet_label=bet_label)
        return fn

    for lo, hi, nz in [
        (1, 3, False), (1, 4, False), (1, 5, False), (2, 4, False), (2, 5, False), (2, 6, False),
        (2, 6, True), (3, 6, False), (3, 7, False), (3, 8, False), (3, 8, True), (4, 8, False),
        (4, 9, False), (5, 10, False), (6, 12, False), (7, 14, False),
    ]:
        strats.append((f"連続_{lo}-{hi}人気{'(ゾロ除)' if nz else ''}", by_pops(list(range(lo, hi + 1)), no_zoro=nz)))

    for name, pops, nz in [
        ("変則_2,4,5,6,7,8(ゾロ含)", [2, 4, 5, 6, 7, 8], False), ("変則_2,4,5,6,7,8(ゾロ除)", [2, 4, 5, 6, 7, 8], True),
        ("変則_奇数1,3,5,7,9", [1, 3, 5, 7, 9], False), ("変則_偶数2,4,6,8,10", [2, 4, 6, 8, 10], False),
        ("変則_奇数3,5,7,9,11", [3, 5, 7, 9, 11], False), ("変則_偶数4,6,8,10,12", [4, 6, 8, 10, 12], False),
        ("変則_1,4,5,6,7", [1, 4, 5, 6, 7], False), ("変則_1,3,6,8,10", [1, 3, 6, 8, 10], False),
        ("変則_2,3,7,8,9", [2, 3, 7, 8, 9], False), ("変則_2,5,7,9,11", [2, 5, 7, 9, 11], False),
        ("変則_3,4,5,6,12", [3, 4, 5, 6, 12], False), ("変則_1,2,8,9,10", [1, 2, 8, 9, 10], False),
        ("変則_1,5,8,11,14", [1, 5, 8, 11, 14], False), ("変則_3,6,9,12,15", [3, 6, 9, 12, 15], False),
    ]:
        strats.append((name, by_pops(pops, no_zoro=nz)))

    for name, m, p1, p2 in [
        ("ゾロ目のみ(1-6人気)", "only", 6, 0), ("ゾロ目のみ(1-8人気)", "only", 8, 0),
        ("ゾロ目のみ(1-10人気)", "only", 10, 0), ("ゾロ目のみ(1-15人気)", "only", 15, 0),
        ("ゾロ目のみ(全人気)", "only", 36, 0), ("ゾロ目+上位1-3", "plus", 10, 1),
        ("ゾロ目+2-4人気", "plus", 10, 2), ("ゾロ目+3-5人気", "plus", 10, 3),
        ("ゾロ目+5-7人気", "plus", 12, 5), ("ゾロ目+7-9人気", "plus", 15, 7),
    ]:
        strats.append((name, zoro_fn(m, p1, p2)))

    for ratio, after, npick in [
        (1.3, False, 2), (1.3, False, 3), (1.3, True, 3), (1.3, True, 4), (1.4, False, 3), (1.4, True, 3),
        (1.4, True, 4), (1.5, False, 3), (1.5, True, 3), (1.5, True, 5), (1.6, False, 3), (1.6, True, 4),
        (1.8, True, 4), (2.0, True, 5),
    ]:
        strats.append((f"断層{ratio}倍_{'直後' if after else '直前'}{npick}点", gap_fn(ratio, after, npick)))

    for sp, init_n, lo, hi, nz in [
        (1, 3, 1.5, 2.0, False), (2, 4, 1.8, 2.5, False), (2, 4, 1.8, 2.5, True), (2, 4, 2.0, 3.0, False),
        (3, 4, 2.0, 3.0, False), (3, 4, 2.5, 4.0, False), (3, 4, 3.0, 5.0, False), (4, 5, 2.5, 4.0, False),
        (4, 5, 3.0, 5.0, False), (4, 5, 4.0, 6.0, False), (5, 5, 3.5, 5.5, False), (5, 5, 4.0, 7.0, False),
        (6, 6, 5.0, 8.0, False), (7, 6, 6.0, 10.0, False),
    ]:
        strats.append((f"合成{lo}-{hi}({sp}人気~{'ゾロ除' if nz else ''})", synth_fn(sp, init_n, lo, hi, nz)))

    for name, pops, f1_lo, f1_hi in [
        ("本命戦(単~1.8)_1-3人気", [1, 2, 3], 1.0, 1.8), ("本命戦(単~1.8)_2-5人気", [2, 3, 4, 5], 1.0, 1.8),
        ("本命戦(単~1.8)_4-8人気", [4, 5, 6, 7, 8], 1.0, 1.8), ("標準戦(単1.8-2.5)_2-6人気", [2, 3, 4, 5, 6], 1.8, 2.5),
        ("標準戦(単1.8-2.5)_4-9人気", [4, 5, 6, 7, 8, 9], 1.8, 2.5), ("混戦(単2.5+)_1-4人気", [1, 2, 3, 4], 2.5, 99.0),
        ("混戦(単2.5+)_2-6人気", [2, 3, 4, 5, 6], 2.5, 99.0), ("混戦(単2.5+)_4-9人気", [4, 5, 6, 7, 8, 9], 2.5, 99.0),
        ("混戦(単2.5+)_6-12人気", [6, 7, 8, 9, 10, 11, 12], 2.5, 99.0), ("大混戦(単3.5+)_2-6人気", [2, 3, 4, 5, 6], 3.5, 99.0),
        ("大混戦(単3.5+)_5-10人気", [5, 6, 7, 8, 9, 10], 3.5, 99.0), ("大混戦(単3.5+)_7-14人気", [7, 8, 9, 10, 11, 12, 13, 14], 3.5, 99.0),
    ]:
        strats.append((name, by_pops(pops, f1_min=f1_lo, f1_max=f1_hi)))

    for name, pops, r1, r2 in [
        ("前半1-6R_2-6人気", [2, 3, 4, 5, 6], 1, 6), ("前半1-6R_4-9人気", [4, 5, 6, 7, 8, 9], 1, 6),
        ("前半1-6R_6-12人気", [6, 7, 8, 9, 10, 11, 12], 1, 6), ("中盤5-8R_2-6人気", [2, 3, 4, 5, 6], 5, 8),
        ("中盤5-8R_4-9人気", [4, 5, 6, 7, 8, 9], 5, 8), ("後半9-12R_2-6人気", [2, 3, 4, 5, 6], 9, 12),
        ("後半9-12R_4-10人気", [4, 5, 6, 7, 8, 9, 10], 9, 12), ("後半9-12R_6-12人気", [6, 7, 8, 9, 10, 11, 12], 9, 12),
        ("メイン10-12R_3-8人気", [3, 4, 5, 6, 7, 8], 10, 12), ("メイン10-12R_5-12人気", [5, 6, 7, 8, 9, 10, 11, 12], 10, 12),
    ]:
        strats.append((name, by_pops(pops, r_lo=r1, r_hi=r2)))

    for name, m, p1, p2 in [
        ("オッズ帯_5-12倍", "odds_range", 5.0, 12.0), ("オッズ帯_8-20倍", "odds_range", 8.0, 20.0),
        ("オッズ帯_10-30倍", "odds_range", 10.0, 30.0), ("オッズ帯_15-40倍", "odds_range", 15.0, 40.0),
        ("オッズ帯_20-60倍", "odds_range", 20.0, 60.0), ("内枠1-4枠のみ上位4点", "inner", 4, 0),
        ("外枠5-8枠のみ上位4点", "outer", 4, 0), ("1枠絡み上位4点", "waku_in", 1, 4),
        ("8枠絡み上位4点", "waku_in", 8, 4), ("7枠絡み上位4点", "waku_in", 7, 4),
    ]:
        strats.append((name, custom_fn(m, p1, p2)))

    return strats[:100]


def run_progression_sim(races: List[Dict], eval_fn: Callable[[Dict], Dict], log_skips: bool = False) -> Dict:
    """追い上げシミュレーション共通エンジン"""
    balance, stage_idx = INITIAL_BANKROLL, 0
    total_bet = total_payout = withdrawn = w_cnt = stop_loss = hits = misses = skips = max_ls = cur_ls = 0
    prog_stats: Dict[str, Dict] = {}
    logs: List[Dict] = []

    for r in races:
        ev = eval_fn(r)
        st_name = STAGES[stage_idx]["name"]
        title = f"{r['venue']} {r['race_num']}R"
        if not ev["participate"]:
            skips += 1
            if log_skips:
                logs.append({
                    "timestamp": r["timestamp"], "race_title": title, "bet_type": "-",
                    "stage_name": st_name, "combos_summary": ev["skip_reason"],
                    "status": "SKIP", "total_bet": 0, "payout": 0, "result_pair": "-", "balance": balance,
                })
            continue

        st = STAGES[stage_idx]
        bet = st["late_bet"] if int(r["race_num"]) >= 9 else st["early_bet"]
        balance -= bet
        total_bet += bet

        if not r.get("wf_pair"):
            payout, status = 0, "PENDING"
        elif ev["is_hit"]:
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

        bkt = ev.get("bucket", "全体")
        prog_stats.setdefault(bkt, {"races": 0, "hits": 0, "bet": 0, "payout": 0})
        prog_stats[bkt]["races"] += 1
        prog_stats[bkt]["bet"] += bet
        prog_stats[bkt]["payout"] += payout
        if status == "HIT":
            prog_stats[bkt]["hits"] += 1

        logs.append({
            "timestamp": r["timestamp"], "race_title": title, "bet_type": ev["bet_type"],
            "stage_name": st_name, "combos_summary": ev["combos_summary"], "status": status,
            "total_bet": bet, "payout": payout, "result_pair": ev["result_pair"], "balance": balance,
        })

    roi = round((total_payout / total_bet * 100.0), 1) if total_bet > 0 else 0.0
    hr = round((hits / (hits + misses) * 100.0), 1) if (hits + misses) > 0 else 0.0
    return {
        "races": hits + misses, "hits": hits, "misses": misses, "skips": skips, "hit_rate": hr,
        "balance": balance, "withdrawn_total": withdrawn, "withdraw_count": w_cnt,
        "daily_stats": {
            "total_bet": total_bet, "total_payout": total_payout,
            "net_profit": total_payout - total_bet, "recovery_rate": roi,
            "hit_count": hits, "miss_count": misses, "skip_count": skips,
            "max_losing_streak": max_ls, "stop_loss_count": stop_loss, "bankrupt_count": 0,
        },
        "prog_stats": prog_stats, "race_logs": logs[-120:],
    }


def update_cache_index() -> List[Dict]:
    entries: List[Dict] = []
    for p in sorted(DATA_DIR.glob("raw_cache_*.json"), reverse=True):
        m_nar = re.match(r"raw_cache_(\d{4})_(all|\d{2})_(.+)\.json$", p.name)
        m_jra = re.match(r"raw_cache_jra_(\d{8})\.json$", p.name)
        try:
            data = json.load(open(p, "r", encoding="utf-8"))
            count = len(data)
            days_count = len({r.get("date_key", "") for r in data if r.get("date_key")}) or 1
        except Exception:
            count, days_count = 0, 0
        if m_nar:
            yr, mo, vn = m_nar.group(1), m_nar.group(2), m_nar.group(3)
            label = f"地方 {yr}年 年間" if mo == "all" else f"地方 {yr}年{int(mo)}月"
            entries.append({"year": int(yr), "month": mo, "venue": vn, "label": label, "races": count, "days": days_count, "file": p.name, "cached": True})
        elif m_jra:
            ymd = m_jra.group(1)
            entries.append({"year": int(ymd[:4]), "month": ymd[4:6], "venue": "JRA", "label": f"JRA {ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}", "races": count, "days": 1, "file": p.name, "cached": True})

    json.dump({"updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "cached_items": entries},
              open(CACHE_INDEX_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    lines = ["# 🟢 v12 ローデータ キャッシュ済み一覧", "", "| 状態 | 対象 | 区分 | 日数 | レース数 |", "| :--- | :--- | :--- | ---: | ---: |"]
    for e in entries:
        lines.append(f"| 🟢 キャッシュ済 | **{e['label']}** | `{e['venue']}` | {e['days']}日 | {e['races']:,}R |")
    open(CACHE_LIST_MD, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return entries


def save_dry_run_dashboard(
    run_id: str, year: int, month: str, week_label: str, venue: str,
    mode_label: str, data_source: str, total_races: int, sim_res: Dict,
    override_prog_stats: Optional[Dict] = None,
) -> None:
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    cached_items = update_cache_index()
    entry = {
        "run_id": run_id, "executed_at": now_str, "year": year, "month": month,
        "week_label": week_label, "venue": venue, "mode_label": mode_label,
        "data_source": data_source, "total_races_fetched": total_races,
        "balance": sim_res["balance"], "withdrawn_total": sim_res["withdrawn_total"],
        "withdraw_count": sim_res["withdraw_count"], "daily_stats": sim_res["daily_stats"],
        "prog_stats": override_prog_stats if override_prog_stats is not None else sim_res["prog_stats"],
        "race_logs": sim_res["race_logs"],
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
    json.dump(out, open(DRY_RUN_STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_prod_state() -> Dict:
    if PROD_STATE_PATH.exists():
        try:
            return json.load(open(PROD_STATE_PATH, "r", encoding="utf-8"))
        except Exception:
            pass
    return {
        "version": "v12",
        "updated_at": "",
        "balance": INITIAL_BANKROLL,
        "withdrawn_total": 0,
        "set_loss": 0,
        "stage_idx": 0,
        "daily_stats": {"total_bet": 0, "total_payout": 0},
        "race_logs": [],
    }


def save_prod_state(state: Dict) -> None:
    state["version"] = "v12"
    state["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    json.dump(state, open(PROD_STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
