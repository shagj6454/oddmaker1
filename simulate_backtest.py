import argparse
import calendar
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

# 1〜8戦目 投資額テーブル（累計上限なし・8戦目不的中で損切りリセット）
STAGES = [
    {"stage": 1, "name": "1戦目", "early_bet": 700,   "late_bet": 500},
    {"stage": 2, "name": "2戦目", "early_bet": 1300,  "late_bet": 900},
    {"stage": 3, "name": "3戦目", "early_bet": 2100,  "late_bet": 1500},
    {"stage": 4, "name": "4戦目", "early_bet": 3700,  "late_bet": 2600},
    {"stage": 5, "name": "5戦目", "early_bet": 6300,  "late_bet": 4400},
    {"stage": 6, "name": "6戦目", "early_bet": 10600, "late_bet": 7500},
    {"stage": 7, "name": "7戦目", "early_bet": 18100, "late_bet": 12800},
    {"stage": 8, "name": "8戦目", "early_bet": 21800, "late_bet": 21800},
]

EXCLUDED_VENUES = ("帯広ば", "金沢", "笠松")
ALL_NAR_VENUES = (
    "帯広ば", "門別", "盛岡", "水沢", "浦和", "船橋", "大井",
    "川崎", "金沢", "笠松", "名古屋", "園田", "姫路", "高知", "佐賀",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run専用 枠複/馬複(2-6人気・合成1.8-2.5自動調整)・8戦損切りリセット版"
    )
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--month", type=str, default="7")
    parser.add_argument("--week", type=str, default="all", choices=["all", "1", "2", "3", "4"])
    parser.add_argument("--venue", type=str, default="all")
    parser.add_argument("--run-id", type=str, default="")
    parser.add_argument("--no-cache", action="store_true")
    return parser.parse_args()


def _normalize_venue(raw_text: str) -> Optional[str]:
    cleaned = re.sub(r"[\s\u3000]+", "", raw_text or "")
    for v in ALL_NAR_VENUES:
        if v == cleaned or v in cleaned:
            return v
    return None


def calc_synthetic_odds(odds_list: List[float]) -> float:
    valid = [o for o in odds_list if o > 0]
    if not valid:
        return 0.0
    inv_sum = sum(1.0 / o for o in valid)
    return round(1.0 / inv_sum, 2) if inv_sum > 0 else 0.0


def _adjust_picks_for_target_odds(
    pool: List[Dict],
    init_count: int = 5,
    min_odds: float = 1.80,
    max_odds: float = 2.50,
) -> Tuple[List[Dict], float]:
    """
    2〜6番人気（5点）をベースに合成オッズを 1.80〜2.50倍 へ自動調整:
      - 2.50倍超: 次人気（7, 8人気...）を1点ずつ追加
      - 1.80倍未満: 最下位人気を1点ずつ削除
    """
    if not pool:
        return [], 0.0

    picked = list(pool[:init_count])
    synth = calc_synthetic_odds([c["odds"] for c in picked])

    next_idx = len(picked)
    while synth > max_odds and next_idx < len(pool):
        cand = picked + [pool[next_idx]]
        cand_synth = calc_synthetic_odds([c["odds"] for c in cand])
        if cand_synth < min_odds:
            break
        picked = cand
        synth = cand_synth
        next_idx += 1

    while synth < min_odds and len(picked) > 1:
        picked.pop()
        synth = calc_synthetic_odds([c["odds"] for c in picked])

    return picked, synth


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
            if len(cells) < 4:
                continue
            leading_ints = [int(c) for c in cells[:4] if re.fullmatch(r"\d{1,2}", c)]
            if len(leading_ints) < 2:
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
    expected_jp = f"{target_date.year}年{target_date.month}月{target_date.day}日"
    expected_jp0 = f"{target_date.year}年{target_date.month:02d}月{target_date.day:02d}日"
    expected_slash = f"{target_date.year}/{target_date.month:02d}/{target_date.day:02d}"

    if (expected_jp not in v_txt) and (expected_jp0 not in v_txt) and (expected_slash not in v_txt):
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

        win_odds = int(m_win.group(2).replace(",", "")) / 100.0
        race_id = f"{race_id_prefix}{rnum:02d}" if len(race_id_prefix) == 16 else ""

        races.append({
            "timestamp": f"{date_label} {10 + rnum // 2:02d}:{(rnum * 25) % 60:02d}",
            "date_key": target_date.strftime("%Y%m%d"),
            "venue": v_name,
            "race_num": rnum,
            "race_id": race_id,
            "win_odds": win_odds,
            "fav1_win_odds": 0.0,
            "has_scratch": has_refund,
            "has_wf": m_wf is not None,
            "wf_pair": m_wf.group(1) if m_wf else None,
            "has_uf": m_uf is not None,
            "uf_pair": m_uf.group(1) if m_uf else None,
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
            if target_venue == "all" and v_name in EXCLUDED_VENUES:
                continue
            if target_venue != "all" and v_name != target_venue:
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

                # 枠複優先、枠複発売なしは馬複を取得
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
        print(f"⚠️ {ymd} 取得中に例外: {e}", flush=True)
    return races


def _worker_fetch_dates(worker_id: int, worker_dates: List[datetime], target_venue: str) -> Dict[str, List[Dict]]:
    from playwright.sync_api import sync_playwright

    day_map: Dict[str, List[Dict]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
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
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = len(data)
            days_count = len({r.get("date_key", "") for r in data if r.get("date_key")})
        except Exception:
            count, days_count = 0, 0
        label = f"{yr}年 年間" if mo == "all" else f"{yr}年{int(mo)}月"
        entries.append({
            "year": int(yr), "month": mo, "venue": vn, "label": label,
            "races": count, "days": days_count, "file": p.name, "cached": True,
        })

    with open(CACHE_INDEX_JSON, "w", encoding="utf-8") as f:
        json.dump({"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "cached_items": entries}, f, ensure_ascii=False, indent=2)

    lines = [
        "# 🟢 ローデータ キャッシュ済み一覧（即実行可能）", "",
        f"最終更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "",
        "| 状態 | 対象年月 | 競馬場 | 保存日数 | 保存レース数 | ファイル名 |",
        "| :--- | :--- | :--- | ---: | ---: | :--- |",
    ]
    for e in entries:
        lines.append(f"| 🟢 キャッシュ済 | **{e['label']}** | `{e['venue']}` | {e['days']}日 | {e['races']:,}R | `{e['file']}` |")

    with open(CACHE_LIST_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return entries


def evaluate_race_with_real_odds(race: Dict) -> Dict:
    """
    枠複／馬複 統一ロジック:
      - 取消/除外馬あり: 見送り
      - 単勝1番人気 1.5倍以下: 見送り
      - 枠複（ゾロ目除外）または馬複で 2〜6番人気（5点）をベースに合成オッズ 1.80〜2.50倍 自動調整
    """
    if race.get("has_scratch"):
        return {"participate": False, "skip_reason": "取消/除外馬あり(見送り)"}

    fav1_odds = float(race.get("fav1_win_odds") or 0.0)
    if 0.0 < fav1_odds <= 1.5:
        return {"participate": False, "skip_reason": f"単勝1人気{fav1_odds}倍<=1.5倍(見送り)"}

    kind = race.get("odds_kind", "wakufuku")
    combos = race.get("real_odds", [])

    if kind not in ("wakufuku", "umafuku") or not combos:
        return {"participate": False, "skip_reason": "枠複/馬複オッズ未取得(見送り)"}

    if kind == "wakufuku":
        label = "枠複"
        pool = [c for c in combos if c["w1"] != c["w2"] and c["pop"] >= 2]
        win_pair_str = race.get("wf_pair")
        bucket = "枠複(2-6人気ベース)"
    else:
        label = "馬複"
        pool = [c for c in combos if c["pop"] >= 2]
        win_pair_str = race.get("uf_pair")
        bucket = "馬複(枠複なし代替)"

    picked, synth = _adjust_picks_for_target_odds(pool, init_count=5, min_odds=1.80, max_odds=2.50)
    if not picked or synth < 1.80:
        return {"participate": False, "skip_reason": f"{label}調整後も実合成{synth}倍<1.80倍(見送り)"}

    is_hit = False
    if win_pair_str and "-" in win_pair_str:
        a_s, b_s = win_pair_str.split("-")
        w1, w2 = min(int(a_s), int(b_s)), max(int(a_s), int(b_s))
        is_hit = any(c["w1"] == w1 and c["w2"] == w2 for c in picked)

    pops_range = f"{picked[0]['pop']}-{picked[-1]['pop']}人気"
    return {
        "participate": True,
        "bucket": bucket,
        "bet_type": label,
        "combos_summary": f"{label}{pops_range}({len(picked)}点/実合成{synth}倍)",
        "synth_odds": synth,
        "is_hit": is_hit,
        "result_pair": win_pair_str or "-",
    }


def run_simulation(
    races: List[Dict],
    year: int,
    month: str,
    week_label: str,
    venue: str,
    run_id: str,
    label: str,
    data_source: str,
    cached_items: List[Dict],
) -> Dict:
    balance = INITIAL_BANKROLL
    set_loss = 0
    stage_idx = 0

    total_bet = 0
    total_payout = 0
    withdrawn_total = 0
    withdraw_count = 0
    stop_loss_count = 0
    hit_count = 0
    miss_count = 0
    skip_count = 0
    max_losing_streak = 0
    cur_losing_streak = 0

    prog_stats: Dict[str, Dict] = {}
    race_logs: List[Dict] = []

    for race in races:
        ev = evaluate_race_with_real_odds(race)
        race_title = f"{race['venue']} {race['race_num']}R"
        stage_name = STAGES[stage_idx]["name"]

        if not ev["participate"]:
            skip_count += 1
            race_logs.append({
                "timestamp": race["timestamp"], "race_title": race_title,
                "bet_type": "-", "stage_name": stage_name,
                "combos_summary": ev["skip_reason"], "status": "SKIP",
                "total_bet": 0, "payout": 0, "result_pair": "-", "balance": balance,
            })
            continue

        # 累計上限なし：1〜8戦目テーブル通りに投資
        st = STAGES[stage_idx]
        bet = st["late_bet"] if race["race_num"] >= 9 else st["early_bet"]

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
            # 8戦目不的中で損切りリセット（1戦目から再スタート）
            if stage_idx >= 7:
                stop_loss_count += 1
                set_loss = 0
                stage_idx = 0
                status = "STOP_LOSS"
            else:
                stage_idx += 1

        bkt = ev["bucket"]
        if bkt not in prog_stats:
            prog_stats[bkt] = {"races": 0, "hits": 0, "bet": 0, "payout": 0}
        prog_stats[bkt]["races"] += 1
        prog_stats[bkt]["bet"] += bet
        prog_stats[bkt]["payout"] += payout
        if ev["is_hit"]:
            prog_stats[bkt]["hits"] += 1

        race_logs.append({
            "timestamp": race["timestamp"], "race_title": race_title,
            "bet_type": ev["bet_type"], "stage_name": stage_name,
            "combos_summary": ev["combos_summary"], "status": status,
            "total_bet": bet, "payout": payout,
            "result_pair": ev["result_pair"], "balance": balance,
        })

    recovery_rate = round((total_payout / total_bet * 100.0), 1) if total_bet > 0 else 0.0
    net_profit = total_payout - total_bet
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    current_run_entry = {
        "run_id": run_id, "executed_at": now_str,
        "year": year, "month": month, "week_label": week_label,
        "venue": venue, "mode_label": label, "data_source": data_source,
        "total_races_fetched": len(races),
        "balance": balance, "withdrawn_total": withdrawn_total, "withdraw_count": withdraw_count,
        "daily_stats": {
            "total_bet": total_bet, "total_payout": total_payout,
            "net_profit": net_profit, "recovery_rate": recovery_rate,
            "hit_count": hit_count, "miss_count": miss_count, "skip_count": skip_count,
            "max_losing_streak": max_losing_streak,
            "stop_loss_count": stop_loss_count, "bankrupt_count": 0,
        },
        "prog_stats": prog_stats,
        "race_logs": race_logs[-120:],
    }

    prev_history: List[Dict] = []
    if DRY_RUN_STATE_PATH.exists():
        try:
            with open(DRY_RUN_STATE_PATH, "r", encoding="utf-8") as f:
                old_state = json.load(f)
            if isinstance(old_state.get("run_history"), list):
                prev_history = old_state["run_history"]
        except Exception:
            prev_history = []

    new_history = [h for h in prev_history if h.get("run_id") != run_id]
    new_history.insert(0, current_run_entry)
    new_history = new_history[:MAX_RUN_HISTORY]

    return {
        "run_id": run_id, "year": year, "month": month, "week_label": week_label,
        "mode_label": label, "data_source": data_source, "updated_at": now_str,
        "balance": balance, "withdrawn_total": withdrawn_total, "withdraw_count": withdraw_count,
        "bankrupt_count": 0, "set_loss": set_loss, "stage_idx": stage_idx,
        "active_ticket": None, "cached_items": cached_items,
        "prog_stats": prog_stats, "daily_stats": current_run_entry["daily_stats"],
        "race_logs": current_run_entry["race_logs"], "run_history": new_history,
    }


def _get_week_date_range(year: int, m_int: int, week: str, today: datetime):
    last_day = calendar.monthrange(year, m_int)[1]
    if week == "1":
        d1, d2 = 1, 7
    elif week == "2":
        d1, d2 = 8, 14
    elif week == "3":
        d1, d2 = 15, 21
    elif week == "4":
        d1, d2 = 22, last_day
    else:
        d1, d2 = 1, last_day

    start_dt = datetime(year, m_int, d1)
    end_dt = min(datetime(year, m_int, min(d2, last_day)), today - timedelta(days=1))
    return start_dt, end_dt


def main():
    args = parse_args()
    year = max(2012, min(2026, args.year))
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    run_id = args.run_id.strip() or datetime.now().strftime("local_%Y%m%d_%H%M%S")

    if args.month.lower() == "all":
        start_dt = datetime(year, 1, 1)
        end_dt = min(datetime(year, 12, 31), today - timedelta(days=1))
        period_tag, month_val, week_label = f"{year}_all", "all", "年間"
        label = f"{year}年 年間検証 [枠複/馬複 2-6人気・8戦損切]"
    else:
        m_int = max(1, min(12, int(args.month)))
        start_dt, end_dt = _get_week_date_range(year, m_int, args.week, today)
        period_tag, month_val = f"{year}_{m_int:02d}", f"{m_int:02d}"
        week_label = "全期間" if args.week == "all" else f"第{args.week}週まで反映"
        label = f"{year}年{m_int}月 検証 ({week_label}) [枠複/馬複 2-6人気・8戦損切]"

    if start_dt > end_dt:
        print(f"ℹ️ 対象期間 ({start_dt.strftime('%Y/%m/%d')}〜) は未来日のためスキップします。", flush=True)
        sys.exit(0)

    cache_path = DATA_DIR / f"raw_cache_{period_tag}_{args.venue}.json"

    cached_by_date: Dict[str, List[Dict]] = {}
    if (not args.no_cache) and cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            for r in existing:
                dk = r.get("date_key", "")
                if dk:
                    cached_by_date.setdefault(dk, []).append(r)
        except Exception:
            cached_by_date = {}

    target_dates: List[datetime] = []
    cur = start_dt
    while cur <= end_dt:
        target_dates.append(cur)
        cur += timedelta(days=1)

    missing_dates = [d for d in target_dates if args.no_cache or (d.strftime("%Y%m%d") not in cached_by_date)]
    used_cache_only = len(missing_dates) == 0

    if used_cache_only:
        print(f"🟢 [キャッシュから即読み込み] {cache_path.name} ({week_label})", flush=True)
    else:
        n_workers = min(3, len(missing_dates))
        chunks = [missing_dates[i::n_workers] for i in range(n_workers)]
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {
                executor.submit(_worker_fetch_dates, idx + 1, chunk, args.venue): idx + 1
                for idx, chunk in enumerate(chunks)
                if chunk
            }
            for fut in as_completed(futures):
                try:
                    res_map = fut.result()
                    for dk, draces in res_map.items():
                        cached_by_date[dk] = draces
                except Exception as e:
                    print(f"⚠️ ワーカー例外: {e}", flush=True)

        merged_all: List[Dict] = []
        for dk in sorted(cached_by_date.keys()):
            merged_all.extend(cached_by_date[dk])
        if merged_all:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(merged_all, f, ensure_ascii=False)
            print(f"💾 ローデータ保存完了: {cache_path.name} (累計 {len(merged_all):,}レース)", flush=True)

    all_races: List[Dict] = []
    for dk in sorted(cached_by_date.keys()):
        if dk <= end_dt.strftime("%Y%m%d"):
            all_races.extend(cached_by_date[dk])

    if len(all_races) == 0:
        print("❌ [異常検知] 取得レース数が0件でした。", flush=True)
        sys.exit(1)

    cached_items = update_cache_index()
    src_label = "🟢 ローデータキャッシュから読込" if used_cache_only else "楽天競馬 実データ＋実最終オッズ (Playwright 3並列)"
    result = run_simulation(
        races=all_races, year=year, month=month_val, week_label=week_label,
        venue=args.venue, run_id=run_id, label=label,
        data_source=src_label, cached_items=cached_items,
    )
    detail_path = DATA_DIR / f"sim_{period_tag}_real_odds.json"

    with open(DRY_RUN_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    st = result["daily_stats"]
    print("=" * 64, flush=True)
    print(f"🧪 [Dry-run 追い上げ検証完了] {result['mode_label']}", flush=True)
    print(f"データソース         : {result['data_source']}", flush=True)
    print(f"累計対象レース数     : {len(all_races):,}レース", flush=True)
    print(f"参戦 / 見送り(SKIP)  : {st['hit_count'] + st['miss_count']:,}戦 ({st['hit_count']}勝 {st['miss_count']}敗) / 見送り {st['skip_count']:,}件", flush=True)
    print(f"追い上げ総投資/払戻  : {st['total_bet']:,}円 / {st['total_payout']:,}円 (追い上げ回収率 {st['recovery_rate']:.1f}%)", flush=True)
    print(f"純収支 / 最終残高    : {st['net_profit']:+,}円 / {result['balance']:,}円", flush=True)
    print(f"8戦目損切り回数      : {st['stop_loss_count']}回 / 最大連敗: {st['max_losing_streak']}連敗", flush=True)
    print("-" * 64, flush=True)
    print("📊 [条件別・追い上げ回収率の内訳]", flush=True)
    for bkt, d in result["prog_stats"].items():
        roi = (d["payout"] / d["bet"] * 100.0) if d["bet"] > 0 else 0.0
        hr = (d["hits"] / d["races"] * 100.0) if d["races"] > 0 else 0.0
        print(f"  - {bkt}: {d['races']}戦 {d['hits']}勝 (的中率 {hr:.1f}%) / 投資 {d['bet']:,}円 → 払戻 {d['payout']:,}円 / 追い上げ回収率 {roi:.1f}%", flush=True)
    print("=" * 64, flush=True)


if __name__ == "__main__":
    main()
