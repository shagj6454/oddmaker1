import argparse
import calendar
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

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

# v11.9 戦目別・投資額＆目標払戻テーブル（8戦目累計52,000円上限）
STAGES = [
    {"stage": 1, "name": "1戦目", "target_payout": 1350,  "early_bet": 700,   "late_bet": 500},
    {"stage": 2, "name": "2戦目", "target_payout": 2400,  "early_bet": 1300,  "late_bet": 900},
    {"stage": 3, "name": "3戦目", "target_payout": 4050,  "early_bet": 2100,  "late_bet": 1500},
    {"stage": 4, "name": "4戦目", "target_payout": 7000,  "early_bet": 3700,  "late_bet": 2600},
    {"stage": 5, "name": "5戦目", "target_payout": 11900, "early_bet": 6300,  "late_bet": 4400},
    {"stage": 6, "name": "6戦目", "target_payout": 20250, "early_bet": 10600, "late_bet": 7500},
    {"stage": 7, "name": "7戦目", "target_payout": 34500, "early_bet": 18100, "late_bet": 12800},
    {"stage": 8, "name": "8戦目", "target_payout": 58800, "early_bet": 21800, "late_bet": 21800},
]

EXCLUDED_VENUES = ("帯広ば", "金沢", "笠松")
ALL_NAR_VENUES = (
    "帯広ば", "門別", "盛岡", "水沢", "浦和", "船橋", "大井",
    "川崎", "金沢", "笠松", "名古屋", "園田", "姫路", "高知", "佐賀",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run専用 4分割(週次反映)・Playwright 3並列・10回分履歴対応"
    )
    parser.add_argument("--year", type=int, default=2026, help="対象年 (2012〜2026)")
    parser.add_argument("--month", type=str, default="5", help="対象月 ('1'〜'12' または 'all' で年間)")
    parser.add_argument("--week", type=str, default="all", choices=["all", "1", "2", "3", "4"], help="週指定 (1=1-7日, 2=8-14日, 3=15-21日, 4=22日-月末)")
    parser.add_argument("--venue", type=str, default="all", help="対象競馬場 ('all' で除外場以外の全場対象)")
    parser.add_argument("--run-id", type=str, default="", help="同一実行を束ねるID（GitHub Actions run_id等）")
    parser.add_argument("--no-cache", action="store_true", help="キャッシュを使わず再取得する")
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


def _fetch_real_tan_fav1_odds(page, race_id: str) -> float:
    """
    単勝・複勝オッズページ（/odds/tanfuku/RACEID/{race_id}）から
    レース前の単勝1番人気オッズを取得する。
    """
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


def _extract_pair_odds_table(page, ordered: bool, max_num: int) -> List[Dict]:
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
                    if not ordered:
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
    ordered = bet_kind in ("wakutan", "umatan")
    max_num = 18 if bet_kind == "umatan" else 8
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(400)
        if "ページが表示できません" in page.inner_text("body"):
            return []

        combos = _extract_pair_odds_table(page, ordered=ordered, max_num=max_num)
        if len(combos) >= 3:
            return combos

        for sel in ('a:has-text("人気順")', 'a:has-text("人気高配当順")', 'button:has-text("人気順")'):
            loc = page.locator(f"{sel}:visible").first
            if loc.count() > 0:
                try:
                    loc.click(timeout=2500)
                    page.wait_for_timeout(400)
                    combos = _extract_pair_odds_table(page, ordered=ordered, max_num=max_num)
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
        m_wt = re.search(r"枠単\s+(\d-\d)\s+([\d,]+)\s*円(?:\s+(\d+)番人気)?", block)
        m_ut = re.search(r"馬単\s+(\d{1,2}-\d{1,2})\s+([\d,]+)\s*円(?:\s+(\d+)番人気)?", block)

        if not m_win:
            continue

        win_odds = int(m_win.group(2).replace(",", "")) / 100.0
        win_pop = int(m_win.group(3)) if m_win.group(3) else 1
        race_id = f"{race_id_prefix}{rnum:02d}" if len(race_id_prefix) == 16 else ""

        races.append({
            "timestamp": f"{date_label} {10 + rnum // 2:02d}:{(rnum * 25) % 60:02d}",
            "date_key": target_date.strftime("%Y%m%d"),
            "venue": v_name,
            "race_num": rnum,
            "race_id": race_id,
            "win_odds": win_odds,
            "win_pop": win_pop,
            "fav1_win_odds": 0.0,
            "has_scratch": has_refund,
            "has_wf": m_wf is not None,
            "wf_pair": m_wf.group(1) if m_wf else None,
            "wf_div": int(m_wf.group(2).replace(",", "")) if m_wf else 0,
            "wf_pop": int(m_wf.group(3)) if (m_wf and m_wf.group(3)) else 0,
            "has_wt": m_wt is not None,
            "wt_pair": m_wt.group(1) if m_wt else None,
            "wt_div": int(m_wt.group(2).replace(",", "")) if m_wt else 0,
            "wt_pop": int(m_wt.group(3)) if (m_wt and m_wt.group(3)) else 0,
            "ut_pair": m_ut.group(1) if m_ut else None,
            "ut_div": int(m_ut.group(2).replace(",", "")) if m_ut else 0,
            "ut_pop": int(m_ut.group(3)) if (m_ut and m_ut.group(3)) else 0,
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

                # レース前の単勝1番人気オッズを取得（未来の着順混入を防止）
                fav1_odds = _fetch_real_tan_fav1_odds(page, r["race_id"])
                r["fav1_win_odds"] = fav1_odds

                if (not r["has_wf"]) or (0.0 < fav1_odds < 2.0):
                    if r["has_wt"]:
                        r["odds_kind"] = "wakutan"
                        r["real_odds"] = _fetch_real_final_odds(page, r["race_id"], "wakutan")
                    else:
                        r["odds_kind"] = "umatan"
                        r["real_odds"] = _fetch_real_final_odds(page, r["race_id"], "umatan")
                else:
                    r["odds_kind"] = "wakufuku"
                    r["real_odds"] = _fetch_real_final_odds(page, r["race_id"], "wakufuku")

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
                print(
                    f"📅 [W{worker_id}] {d.strftime('%Y/%m/%d')}: {len(day_races)}レース取得",
                    flush=True,
                )
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
            dates_set = sorted({r.get("date_key", "") for r in data if r.get("date_key")})
            days_count = len(dates_set)
        except Exception:
            count, days_count = 0, 0
        label = f"{yr}年 年間" if mo == "all" else f"{yr}年{int(mo)}月"
        entries.append({
            "year": int(yr),
            "month": mo,
            "venue": vn,
            "label": label,
            "races": count,
            "days": days_count,
            "file": p.name,
            "cached": True,
        })

    with open(CACHE_INDEX_JSON, "w", encoding="utf-8") as f:
        json.dump(
            {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "cached_items": entries},
            f,
            ensure_ascii=False,
            indent=2,
        )

    lines = [
        "# 🟢 ローデータ キャッシュ済み一覧（即実行可能）",
        "",
        f"最終更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| 状態 | 対象年月 | 競馬場 | 保存日数 | 保存レース数 | ファイル名 |",
        "| :--- | :--- | :--- | ---: | ---: | :--- |",
    ]
    for e in entries:
        lines.append(
            f"| 🟢 キャッシュ済 | **{e['label']}** | `{e['venue']}` | {e['days']}日 | {e['races']:,}R | `{e['file']}` |"
        )

    with open(CACHE_LIST_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return entries


def evaluate_race_with_real_odds(race: Dict) -> Dict:
    """
    買い目判定（ゾロ目除外時は次人気へ繰り上げて必ず規定点数を確保）:
      - 単勝1倍台 (レース前1人気 1.9倍以下): 枠単または馬単 2〜4番人気（3点・合成1.80倍以上）
      - 少頭数 (枠複なし・単勝2.0倍以上): 枠単または馬単 2〜5番人気（4点・合成1.80倍以上）
      - 前半1〜8R (枠複): 2番人気以降の非ゾロ目を必ず5点確保（合成1.80倍以上）
      - 後半9〜12R (枠複): 3番人気以降の非ゾロ目を必ず6点確保（合成1.80倍以上）
    """
    if race.get("has_scratch"):
        return {"participate": False, "skip_reason": "取消/除外馬あり(見送り)"}

    rnum = race["race_num"]
    fav1_odds = float(race.get("fav1_win_odds") or race.get("win_odds") or 0.0)
    combos = race.get("real_odds", [])
    kind = race.get("odds_kind", "wakufuku")

    if not combos:
        return {"participate": False, "skip_reason": "最終オッズ未取得(見送り)"}

    if kind in ("wakutan", "umatan"):
        label = "枠単" if kind == "wakutan" else "馬単"
        if 0.0 < fav1_odds < 2.0:
            picked = [c for c in combos if c["pop"] >= 2][:3]
            bucket = f"単勝1倍台({label} 2-4人気・3点)"
            pop_label = "2-4人気"
        else:
            picked = [c for c in combos if c["pop"] >= 2][:4]
            bucket = f"少頭数({label} 2-5人気・4点)"
            pop_label = "2-5人気"

        synth = calc_synthetic_odds([c["odds"] for c in picked])
        if synth < 1.80:
            return {"participate": False, "skip_reason": f"{label}{pop_label}実合成{synth}倍<1.80倍(見送り)"}

        win_pair_str = race["wt_pair"] if kind == "wakutan" else race["ut_pair"]
        is_hit = False
        if win_pair_str and "-" in win_pair_str:
            a_s, b_s = win_pair_str.split("-")
            is_hit = any(c["w1"] == int(a_s) and c["w2"] == int(b_s) for c in picked)

        return {
            "participate": True,
            "bucket": bucket,
            "bet_type": label,
            "combos_summary": f"{label}{pop_label}({len(picked)}点/実合成{synth}倍)",
            "synth_odds": synth,
            "is_hit": is_hit,
            "result_pair": win_pair_str or "-",
        }

    # 枠複：ゾロ目を除外し、次の人気へ繰り上げて必ず規定点数（前半5点 / 後半6点）を確保
    non_zoro = [c for c in combos if c["w1"] != c["w2"]]

    if rnum <= 8:
        picked = [c for c in non_zoro if c["pop"] >= 2][:5]
        if len(picked) < 5:
            return {"participate": False, "skip_reason": f"前半枠複の対象買い目不足({len(picked)}点/見送り)"}
        synth = calc_synthetic_odds([c["odds"] for c in picked])
        if synth < 1.80:
            return {"participate": False, "skip_reason": f"前半枠複2-6人気実合成{synth}倍<1.80倍(見送り)"}

        win_pair_str = race["wf_pair"]
        is_hit = False
        if win_pair_str and "-" in win_pair_str:
            a_s, b_s = win_pair_str.split("-")
            w1, w2 = min(int(a_s), int(b_s)), max(int(a_s), int(b_s))
            is_hit = any(c["w1"] == w1 and c["w2"] == w2 for c in picked)

        pops_range = f"{picked[0]['pop']}-{picked[-1]['pop']}人気"
        return {
            "participate": True,
            "bucket": "前半1-8R(枠複 5点確保)",
            "bet_type": "枠複",
            "combos_summary": f"{pops_range}(5点/実合成{synth}倍)",
            "synth_odds": synth,
            "is_hit": is_hit,
            "result_pair": win_pair_str or "-",
        }

    picked = [c for c in non_zoro if c["pop"] >= 3][:6]
    if len(picked) < 6:
        return {"participate": False, "skip_reason": f"後半枠複の対象買い目不足({len(picked)}点/見送り)"}
    synth = calc_synthetic_odds([c["odds"] for c in picked])
    if synth < 1.80:
        return {"participate": False, "skip_reason": f"後半枠複3-8人気実合成{synth}倍<1.80倍(見送り)"}

    win_pair_str = race["wf_pair"]
    is_hit = False
    if win_pair_str and "-" in win_pair_str:
        a_s, b_s = win_pair_str.split("-")
        w1, w2 = min(int(a_s), int(b_s)), max(int(a_s), int(b_s))
        is_hit = any(c["w1"] == w1 and c["w2"] == w2 for c in picked)

    pops_range = f"{picked[0]['pop']}-{picked[-1]['pop']}人気"
    return {
        "participate": True,
        "bucket": "後半9-12R(枠複 6点確保)",
        "bet_type": "枠複",
        "combos_summary": f"{pops_range}(6点/実合成{synth}倍)",
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
    bankrupt_count = 0
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
                "timestamp": race["timestamp"],
