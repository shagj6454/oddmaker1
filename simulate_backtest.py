import argparse
import calendar
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DRY_RUN_STATE_PATH = DATA_DIR / "wakuren_dry_run_state_v11_9.json"
CACHE_INDEX_JSON = DATA_DIR / "cache_index.json"
CACHE_LIST_MD = DATA_DIR / "CACHE_LIST.md"

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

EXCLUDED_VENUES = ("帯広ば", "金沢", "笠松")
ALL_NAR_VENUES = (
    "帯広ば", "門別", "盛岡", "水沢", "浦和", "船橋", "大井",
    "川崎", "金沢", "笠松", "名古屋", "園田", "姫路", "高知", "佐賀",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="未キャッシュ自動取得(3並列・週次)＋100パターン総当たり")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--month", type=str, default="7")
    p.add_argument("--week", type=str, default="all", choices=["all", "1", "2", "3", "4"])
    p.add_argument("--venue", type=str, default="all")
    p.add_argument("--run-id", type=str, default="")
    p.add_argument("--no-cache", action="store_true")
    return p.parse_args()


def _normalize_venue(raw_text: str) -> Optional[str]:
    cleaned = re.sub(r"[\s\u3000]+", "", raw_text or "")
    for v in ALL_NAR_VENUES:
        if v == cleaned or v in cleaned:
            return v
    return None


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
    for i in range(1, min(len(combos), 14)):
        prev_o, cur_o = combos[i - 1]["odds"], combos[i]["odds"]
        if prev_o > 0 and (cur_o / prev_o) >= ratio:
            return i
    return -1


def _fetch_real_tan_fav1_odds(page, race_id: str) -> float:
    url = f"https://keiba.rakuten.co.jp/odds/tanfuku/RACEID/{race_id}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(300)
        if "ページが表示できません" in page.inner_text("body"):
            return 0.0
        min_win = 9999.0
        rows = page.locator("table tr")
        for i in range(rows.count()):
            cells = [re.sub(r"\s+", " ", c).strip() for c in rows.nth(i).locator("th, td").all_inner_texts()]
            if len(cells) < 4 or len([c for c in cells[:4] if re.fullmatch(r"\d{1,2}", c)]) < 2:
                continue
            for cell in cells[2:]:
                if re.search(r"\d+\.\d+\s*[-－～~]\s*\d+\.\d+", cell):
                    continue
                m_o = re.search(r"\b(\d+\.\d+)\b", cell)
                if m_o:
                    val = float(m_o.group(1))
                    if 1.0 <= val < min_win:
                        min_win = val
                    break
        return min_win if min_win < 9999.0 else 0.0
    except Exception:
        return 0.0


def _extract_pair_odds_table(page, max_num: int) -> List[Dict]:
    combos: List[Dict] = []
    rows = page.locator("table tr")
    for i in range(rows.count()):
        cells = [re.sub(r"\s+", " ", c).strip() for c in rows.nth(i).locator("th, td").all_inner_texts()]
        idx = 0
        while idx + 2 < len(cells):
            m_pop = re.fullmatch(r"\d{1,3}", cells[idx])
            m_pair = re.search(r"\b(\d{1,2})\s*[-－→]\s*(\d{1,2})\b", cells[idx + 1])
            m_odds = re.search(r"\b(\d+(?:\.\d+)?)\b", cells[idx + 2])
            if m_pop and m_pair and m_odds:
                pop = int(m_pop.group(0))
                a, b, odds = int(m_pair.group(1)), int(m_pair.group(2)), float(m_odds.group(1))
                if 1 <= a <= max_num and 1 <= b <= max_num and 0.0 < odds < 9999:
                    a, b = min(a, b), max(a, b)
                    if not any(c["w1"] == a and c["w2"] == b for c in combos):
                        combos.append({"pop": pop, "w1": a, "w2": b, "odds": odds})
                idx += 3
            else:
                idx += 1
    combos.sort(key=lambda x: x["pop"])
    return combos


def _fetch_real_final_odds(page, race_id: str, bet_kind: str) -> List[Dict]:
    url = f"https://keiba.rakuten.co.jp/odds/{bet_kind}/RACEID/{race_id}"
    max_num = 18 if bet_kind == "umafuku" else 8
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(400)
        if "ページが表示できません" in page.inner_text("body"):
            return []
        combos = _extract_pair_odds_table(page, max_num=max_num)
        if len(combos) >= 3:
            return combos
        for sel in ('a:has-text("人気順")', 'a:has-text("人気高配当順")', 'button:has-text("人気順")'):
            loc = page.locator(f"{sel}:visible").first
            if loc.count() > 0:
                try:
                    loc.click(timeout=2500)
                    page.wait_for_timeout(400)
                    combos = _extract_pair_odds_table(page, max_num=max_num)
                    if len(combos) >= 3:
                        return combos
                except Exception:
                    continue
        return combos
    except Exception:
        return []


def _parse_dividend_body(v_txt: str, v_name: str, race_id_prefix: str, target_date: datetime) -> List[Dict]:
    races: List[Dict] = []
    date_label = target_date.strftime("%m/%d")
    e1 = f"{target_date.year}年{target_date.month}月{target_date.day}日"
    e2 = f"{target_date.year}年{target_date.month:02d}月{target_date.day:02d}日"
    e3 = f"{target_date.year}/{target_date.month:02d}/{target_date.day:02d}"
    if (e1 not in v_txt) and (e2 not in v_txt) and (e3 not in v_txt):
        return []
    for rnum in range(1, 13):
        m_r = re.search(rf"■\s*{rnum}R\b", v_txt)
        if not m_r:
            continue
        after = v_txt[m_r.start():]
        m_next = re.search(r"■\s*(?:\d{1,2}R|本日の発売情報)", after[4:])
        block = after[: m_next.start() + 4] if m_next else after
        has_refund = bool(re.search(r"返還票数[\s\S]{0,80}?[1-9]\d*", block))
        m_win = re.search(r"単勝\s+(\d{1,2})\s+([\d,]+)\s*円(?:\s+(\d+)番人気)?", block)
        m_wf = re.search(r"枠複\s+(\d-\d)\s+([\d,]+)\s*円(?:\s+(\d+)番人気)?", block)
        m_uf = re.search(r"馬複\s+(\d{1,2}-\d{1,2})\s+([\d,]+)\s*円(?:\s+(\d+)番人気)?", block)
        if not m_win:
            continue
        races.append({
            "timestamp": f"{date_label} {10 + rnum // 2:02d}:{(rnum * 25) % 60:02d}",
            "date_key": target_date.strftime("%Y%m%d"),
            "venue": v_name, "race_num": rnum,
            "race_id": f"{race_id_prefix}{rnum:02d}" if len(race_id_prefix) == 16 else "",
            "win_odds": int(m_win.group(2).replace(",", "")) / 100.0,
            "fav1_win_odds": 0.0, "has_scratch": has_refund,
            "has_wf": m_wf is not None, "wf_pair": m_wf.group(1) if m_wf else None,
            "has_uf": m_uf is not None, "uf_pair": m_uf.group(1) if m_uf else None,
        })
    return races


def _fetch_one_day_on_page(page, target_date: datetime, target_venue: str = "all") -> List[Dict]:
    races: List[Dict] = []
    ymd = target_date.strftime("%Y%m%d")
    try:
        url = f"https://keiba.rakuten.co.jp/race_dividend/list/RACEID/{ymd}0000000000"
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(500)
        body_txt = page.inner_text("body")
        if "ページが表示できません" in body_txt:
            return []
        active_venues: List[tuple] = []
        seen = set()
        m_cur_id = re.search(r"/RACEID/(\d{18})", page.url)
        m_title = re.search(r"([^\s\n]+?)競馬場\s*払戻金", body_txt)
        if m_title and m_cur_id and not m_cur_id.group(1).endswith("0000000000"):
            init_v = _normalize_venue(m_title.group(1))
            if init_v:
                seen.add(init_v)
                if (target_venue == "all" and init_v not in EXCLUDED_VENUES) or (init_v == target_venue):
                    active_venues.append((init_v, page.url, m_cur_id.group(1)[:16]))
        links = page.locator('a[href*="/race_dividend/list/RACEID/"]:visible')
        for i in range(links.count()):
            a_tag = links.nth(i)
            cls = a_tag.get_attribute("class") or ""
            if "disableItem" in cls or "disabled" in cls:
                continue
            v_name = _normalize_venue(a_tag.inner_text())
            href = a_tag.get_attribute("href") or ""
            m_id = re.search(r"/RACEID/(\d{18})", href)
            if not v_name or not m_id or v_name in seen or ymd not in href:
                continue
            if (target_venue == "all" and v_name in EXCLUDED_VENUES) or (target_venue != "all" and v_name != target_venue):
                continue
            seen.add(v_name)
            full_url = href if href.startswith("http") else f"https://keiba.rakuten.co.jp{href}"
            active_venues.append((v_name, full_url, m_id.group(1)[:16]))
        for v_name, v_url, prefix16 in active_venues:
            if page.url != v_url:
                page.goto(v_url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(400)
            v_races = _parse_dividend_body(page.inner_text("body"), v_name, prefix16, target_date)
            for r in v_races:
                if r["has_scratch"] or not r["race_id"]:
                    r["real_odds"] = []
                    continue
                r["fav1_win_odds"] = _fetch_real_tan_fav1_odds(page, r["race_id"])
                if r["has_wf"]:
                    r["odds_kind"] = "wakufuku"
                    r["real_odds"] = _fetch_real_final_odds(page, r["race_id"], "wakufuku")
                elif r["has_uf"]:
                    r["odds_kind"] = "umafuku"
                    r["real_odds"] = _fetch_real_final_odds(page, r["race_id"], "umafuku")
                else:
                    r["odds_kind"] = "none"
                    r["real_odds"] = []
            races.extend(v_races)
    except Exception as e:
        print(f"⚠️ {ymd} 取得例外: {e}", flush=True)
    return races


def _worker_fetch_dates(worker_id: int, worker_dates: List[datetime], target_venue: str) -> Dict[str, List[Dict]]:
    from playwright.sync_api import sync_playwright
    day_map: Dict[str, List[Dict]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        try:
            for d in worker_dates:
                day_races = _fetch_one_day_on_page(page, d, target_venue)
                day_map[d.strftime("%Y%m%d")] = day_races
                print(f"📅 [W{worker_id}] {d.strftime('%Y/%m/%d')}: {len(day_races)}レース取得", flush=True)
        finally:
            browser.close()
    return day_map


def update_cache_index() -> List[Dict]:
    entries: List[Dict] = []
    for p in sorted(DATA_DIR.glob("raw_cache_*.json"), reverse=True):
        m = re.match(r"raw_cache_(\d{4})_(all|\d{2})_(.+)\.json$", p.name)
        if not m:
            continue
        yr, mo, vn = m.group(1), m.group(2), m.group(3)
        try:
            data = json.load(open(p, "r", encoding="utf-8"))
            count, days_count = len(data), len({r.get("date_key", "") for r in data if r.get("date_key")})
        except Exception:
            count, days_count = 0, 0
        entries.append({
            "year": int(yr), "month": mo, "venue": vn,
            "label": f"{yr}年 年間" if mo == "all" else f"{yr}年{int(mo)}月",
            "races": count, "days": days_count, "file": p.name, "cached": True,
        })
    json.dump({"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "cached_items": entries},
              open(CACHE_INDEX_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    lines = ["# 🟢 ローデータ キャッシュ済み一覧", "", "| 状態 | 対象年月 | 競馬場 | 日数 | レース数 |", "| :--- | :--- | :--- | ---: | ---: |"]
    for e in entries:
        lines.append(f"| 🟢 キャッシュ済 | **{e['label']}** | `{e['venue']}` | {e['days']}日 | {e['races']:,}R |")
    open(CACHE_LIST_MD, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return entries


def _make_pick(race: Dict, picked: List[Dict], min_synth: float = 1.20, label: str = "") -> Dict:
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
        "participate": True, "bet_type": "枠複",
        "combos_summary": f"{label}[{pops}人気]({len(picked)}点/合成{synth}倍)",
        "synth_odds": synth, "is_hit": is_hit, "result_pair": win_str or "-",
    }


def build_100_strategies() -> List[Tuple[str, Callable[[Dict], Dict]]]:
    strats: List[Tuple[str, Callable[[Dict], Dict]]] = []

    def wf(r: Dict) -> Optional[List[Dict]]:
        if r.get("has_scratch") or r.get("odds_kind") != "wakufuku":
            return None
        c = r.get("real_odds") or []
        return c if len(c) >= 6 else None

    def by_pops(pops: List[int], no_zoro=False, min_s=1.40, f1_min=0.0, f1_max=999.0, r_lo=1, r_hi=12):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c or not (r_lo <= r["race_num"] <= r_hi):
                return {"participate": False, "skip_reason": "対象外"}
            f1 = float(r.get("fav1_win_odds") or r.get("win_odds") or 0.0)
            if not (f1_min <= f1 <= f1_max):
                return {"participate": False, "skip_reason": "単勝条件外"}
            src = [x for x in c if x["w1"] != x["w2"]] if no_zoro else c
            return _make_pick(r, [x for x in src if x["pop"] in pops], min_synth=min_s)
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
                return _make_pick(r, zoros, min_synth=1.50, label="ゾロ目のみ")
            others = [x for x in c if x["w1"] != x["w2"] and x["pop"] >= param2][:3]
            return _make_pick(r, zoros[:2] + others, min_synth=1.60, label="ゾロ目+")
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
            return _make_pick(r, picked, min_synth=1.50, label=f"断層{'後' if after else '前'}")
        return fn

    def synth_fn(start_pop: int, init_n: int, lo: float, hi: float, no_zoro=False):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c:
                return {"participate": False, "skip_reason": "対象外"}
            src = [x for x in c if x["w1"] != x["w2"]] if no_zoro else c
            picked, _ = _adjust_synth([x for x in src if x["pop"] >= start_pop], init_n, lo, hi)
            return _make_pick(r, picked, min_synth=lo, label=f"合成{lo}-{hi}")
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
            return _make_pick(r, picked, min_synth=1.50, label=mode)
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


def simulate_one(races: List[Dict], strat_name: str, eval_fn: Callable[[Dict], Dict]) -> Dict:
    balance, stage_idx = INITIAL_BANKROLL, 0
    total_bet = total_payout = withdrawn = w_cnt = stop_loss = hits = misses = skips = max_ls = cur_ls = 0
    logs: List[Dict] = []
    for r in races:
        ev = eval_fn(r)
        st_name = STAGES[stage_idx]["name"]
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
            "timestamp": r["timestamp"], "race_title": f"{r['venue']} {r['race_num']}R",
            "bet_type": ev["bet_type"], "stage_name": st_name, "combos_summary": ev["combos_summary"],
            "status": status, "total_bet": bet, "payout": payout, "result_pair": ev["result_pair"], "balance": balance,
        })
    roi = round((total_payout / total_bet * 100.0), 1) if total_bet > 0 else 0.0
    hr = round((hits / (hits + misses) * 100.0), 1) if (hits + misses) > 0 else 0.0
    return {
        "name": strat_name, "races": hits + misses, "hits": hits, "misses": misses, "skips": skips,
        "hit_rate": hr, "total_bet": total_bet, "total_payout": total_payout,
        "net_profit": total_payout - total_bet, "roi": roi, "balance": balance,
        "withdrawn": withdrawn, "withdraw_count": w_cnt, "stop_loss": stop_loss, "max_ls": max_ls, "logs": logs[-120:],
    }


def _get_week_date_range(year: int, m_int: int, week: str, today: datetime):
    last_day = calendar.monthrange(year, m_int)[1]
    d1, d2 = {"1": (1, 7), "2": (8, 14), "3": (15, 21), "4": (22, last_day)}.get(week, (1, last_day))
    return datetime(year, m_int, d1), min(datetime(year, m_int, min(d2, last_day)), today - timedelta(days=1))


def main():
    args = parse_args()
    year = max(2012, min(2026, args.year))
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    run_id = args.run_id.strip() or datetime.now().strftime("grid100_%Y%m%d_%H%M%S")

    if args.month.lower() == "all":
        start_dt, end_dt = datetime(year, 1, 1), min(datetime(year, 12, 31), today - timedelta(days=1))
        period_tag, month_val, week_label = f"{year}_all", "all", "年間"
    else:
        m_int = max(1, min(12, int(args.month)))
        start_dt, end_dt = _get_week_date_range(year, m_int, args.week, today)
        period_tag, month_val = f"{year}_{m_int:02d}", f"{m_int:02d}"
        week_label = "全期間" if args.week == "all" else f"第{args.week}週まで反映"

    if start_dt > end_dt:
        sys.exit(0)

    cache_path = DATA_DIR / f"raw_cache_{period_tag}_{args.venue}.json"
    cached_by_date: Dict[str, List[Dict]] = {}
    if (not args.no_cache) and cache_path.exists():
        try:
            for r in json.load(open(cache_path, "r", encoding="utf-8")):
                if r.get("date_key"):
                    cached_by_date.setdefault(r["date_key"], []).append(r)
        except Exception:
            cached_by_date = {}

    target_dates: List[datetime] = []
    cur = start_dt
    while cur <= end_dt:
        target_dates.append(cur)
        cur += timedelta(days=1)

    missing_dates = [d for d in target_dates if args.no_cache or (d.strftime("%Y%m%d") not in cached_by_date)]
    used_cache_only = len(missing_dates) == 0

    if used_cache_only and args.week in ("1", "2", "3"):
        print(f"⚡ [キャッシュ済] 第{args.week}週はスキップし第4週で一括検証します。", flush=True)
        sys.exit(0)

    if not used_cache_only:
        n_workers = min(3, len(missing_dates))
        chunks = [missing_dates[i::n_workers] for i in range(n_workers)]
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futs = [executor.submit(_worker_fetch_dates, i + 1, ch, args.venue) for i, ch in enumerate(chunks) if ch]
            for fut in as_completed(futs):
                try:
                    cached_by_date.update(fut.result())
                except Exception as e:
                    print(f"⚠️ ワーカー例外: {e}", flush=True)
        merged_all = [r for dk in sorted(cached_by_date.keys()) for r in cached_by_date[dk]]
        if merged_all:
            json.dump(merged_all, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
            print(f"💾 ローデータ保存: {cache_path.name} (累計 {len(merged_all):,}R)", flush=True)

    all_races = [r for dk in sorted(cached_by_date.keys()) if dk <= end_dt.strftime("%Y%m%d") for r in cached_by_date[dk]]
    if not all_races:
        print("❌ 取得レース数が0件でした。", flush=True)
        sys.exit(1)

    cached_items = update_cache_index()
    results = [simulate_one(all_races, name, fn) for name, fn in build_100_strategies()]
    results.sort(key=lambda x: (x["roi"], x["net_profit"]), reverse=True)

    print("=" * 88, flush=True)
    print(f"🏆 [100パターン総当たり TOP20] {cache_path.name} ({week_label} / {len(all_races):,}R)", flush=True)
    for idx, res in enumerate(results[:20], 1):
        print(f"{idx:2d}位 {res['roi']:6.1f}% {res['net_profit']:+10,d}円 的中{res['hit_rate']:5.1f}% ({res['races']:3d}戦/損切{res['stop_loss']:2d}) {res['name']}", flush=True)
    print("=" * 88, flush=True)

    best = results[0]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prog_stats = {
        f"TOP{i+1}: {r['name']}": {"races": r["races"], "hits": r["hits"], "bet": r["total_bet"], "payout": r["total_payout"]}
        for i, r in enumerate(results[:20])
    }
    entry = {
        "run_id": run_id, "executed_at": now_str, "year": year, "month": month_val,
        "week_label": week_label, "venue": args.venue,
        "mode_label": f"{year}年{args.month}月 100種総当たり1位 [{best['name']}] ({week_label})",
        "data_source": "🟢 キャッシュ100種総当たり" if used_cache_only else "楽天競馬 実取得(3並列)＋100種総当たり",
        "total_races_fetched": len(all_races),
        "balance": best["balance"], "withdrawn_total": best["withdrawn"], "withdraw_count": best["withdraw_count"],
        "daily_stats": {
            "total_bet": best["total_bet"], "total_payout": best["total_payout"],
            "net_profit": best["net_profit"], "recovery_rate": best["roi"],
            "hit_count": best["hits"], "miss_count": best["misses"], "skip_count": best["skips"],
            "max_losing_streak": best["max_ls"], "stop_loss_count": best["stop_loss"], "bankrupt_count": 0,
        },
        "prog_stats": prog_stats, "race_logs": best["logs"],
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


if __name__ == "__main__":
    main()
