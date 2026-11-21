import argparse
import calendar
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List

from core_v12 import (
    DATA_DIR,
    EXCLUDED_VENUES,
    build_100_strategies,
    evaluate_v12_standard,
    run_progression_sim,
    save_dry_run_dashboard,
)
from nar_rakuten_bettor import (
    fetch_nar_horses_leg_info,
    fetch_nar_odds,
    fetch_nar_tan_and_meta,
    normalize_nar_venue,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="v13.5 地方競馬シミュレーター（3並列キャッシュ＋脚質・馬場重み＋100種総当たり）")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--month", type=str, default="7")
    p.add_argument("--week", type=str, default="all", choices=["all", "1", "2", "3", "4"])
    p.add_argument("--venue", type=str, default="all")
    p.add_argument("--mode", type=str, default="v12", choices=["v12", "grid100"])
    p.add_argument("--run-id", type=str, default="")
    p.add_argument("--no-cache", action="store_true")
    return p.parse_args()


def _parse_dividend_body(v_txt: str, v_name: str, race_id_prefix: str, target_date: datetime) -> List[Dict]:
    races: List[Dict] = []
    date_label = target_date.strftime("%m/%d")
    e1 = f"{target_date.year}年{target_date.month}月{target_date.day}日"
    e2 = f"{target_date.year}年{target_date.month:02d}月{target_date.day:02d}日"
    e3 = f"{target_date.year}/{target_date.month:02d}/{target_date.day:02d}"
    if (e1 not in v_txt) and (e2 not in v_txt) and (e3 not in v_txt):
        return []
    m_day_cond = re.search(r"馬場(?:状態)?[:：\s]*(不良|稍重|良|稍|重|不)", v_txt[:1500])
    day_cond = ("稍重" if m_day_cond.group(1) == "稍" else ("不良" if m_day_cond.group(1) == "不" else m_day_cond.group(1))) if m_day_cond else ""
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
            "fav1_win_odds": 0.0, "fav1_waku": 0, "fav1_leg_type": "不明",
            "horse_count": 0, "race_name": "", "race_class": "",
            "surface": "ダート", "distance": 0, "track_condition": day_cond,
            "horses_info": [], "waku_leg_scores": {},
            "has_scratch": has_refund,
            "has_wf": m_wf is not None, "wf_pair": m_wf.group(1) if m_wf else None,
            "has_uf": m_uf is not None, "uf_pair": m_uf.group(1) if m_uf else None,
        })
    return races


def _fetch_one_day(page, target_date: datetime, target_venue: str) -> List[Dict]:
    races: List[Dict] = []
    ymd = target_date.strftime("%Y%m%d")
    try:
        page.goto(f"https://keiba.rakuten.co.jp/race_dividend/list/RACEID/{ymd}0000000000", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(500)
        body_txt = page.inner_text("body")
        if "ページが表示できません" in body_txt:
            return []
        active_venues, seen = [], set()
        m_cur_id = re.search(r"/RACEID/(\d{18})", page.url)
        m_title = re.search(r"([^\s\n]+?)競馬場\s*払戻金", body_txt)
        if m_title and m_cur_id and not m_cur_id.group(1).endswith("0000000000"):
            iv = normalize_nar_venue(m_title.group(1))
            if iv:
                seen.add(iv)
                if (target_venue == "all" and iv not in EXCLUDED_VENUES) or (iv == target_venue):
                    active_venues.append((iv, page.url, m_cur_id.group(1)[:16]))
        links = page.locator('a[href*="/race_dividend/list/RACEID/"]:visible')
        for i in range(links.count()):
            a_tag = links.nth(i)
            cls = a_tag.get_attribute("class") or ""
            if "disableItem" in cls or "disabled" in cls:
                continue
            v_name = normalize_nar_venue(a_tag.inner_text())
            href = a_tag.get_attribute("href") or ""
            m_id = re.search(r"/RACEID/(\d{18})", href)
            if not v_name or not m_id or v_name in seen or ymd not in href:
                continue
            if (target_venue == "all" and v_name in EXCLUDED_VENUES) or (target_venue != "all" and v_name != target_venue):
                continue
            seen.add(v_name)
            active_venues.append((v_name, href if href.startswith("http") else f"https://keiba.rakuten.co.jp{href}", m_id.group(1)[:16]))
        for v_name, v_url, prefix16 in active_venues:
            if page.url != v_url:
                page.goto(v_url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(400)
            v_races = _parse_dividend_body(page.inner_text("body"), v_name, prefix16, target_date)
            for r in v_races:
                if not r["race_id"]:
                    r["real_odds"] = []
                    continue
                tm = fetch_nar_tan_and_meta(page, r["race_id"])
                r["fav1_win_odds"] = tm["fav1_win_odds"]
                r["fav1_waku"] = tm["fav1_waku"]
                r["horse_count"] = tm["horse_count"]
                r["race_name"] = tm["race_name"]
                r["race_class"] = tm["race_class"]
                r["surface"] = tm["surface"]
                r["track_condition"] = tm["track_condition"]

                init_cond = tm["track_condition"] or r.get("track_condition") or ""
                horses_info, waku_scores, final_cond, final_dist = fetch_nar_horses_leg_info(
                    page, r["race_id"], v_name, tm["surface"], init_cond,
                    win_odds_map=tm.get("win_odds_map"), distance=tm.get("distance", 0),
                )
                r["track_condition"] = final_cond
                r["distance"] = final_dist
                r["horses_info"] = horses_info
                r["waku_leg_scores"] = waku_scores
                fav1_h = next((h for h in horses_info if h["umaban"] == tm["fav1_umaban"]), None)
                r["fav1_leg_type"] = fav1_h["leg_type"] if fav1_h else "不明"

                if r["has_wf"]:
                    r["odds_kind"] = "wakufuku"
                    r["real_odds"] = fetch_nar_odds(page, r["race_id"], "wakufuku")
                elif r["has_uf"]:
                    r["odds_kind"] = "umafuku"
                    r["real_odds"] = fetch_nar_odds(page, r["race_id"], "umafuku")
                else:
                    r["odds_kind"] = "none"
                    r["real_odds"] = []
            races.extend(v_races)
    except Exception as e:
        print(f"⚠️ {ymd} 取得例外: {e}", flush=True)
    return races


def _worker_fetch(wid: int, dates: List[datetime], target_venue: str) -> Dict[str, List[Dict]]:
    from playwright.sync_api import sync_playwright
    out: Dict[str, List[Dict]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        try:
            for d in dates:
                dr = _fetch_one_day(page, d, target_venue)
                out[d.strftime("%Y%m%d")] = dr
                print(f"📅 [W{wid}] {d.strftime('%Y/%m/%d')}: {len(dr)}R", flush=True)
        finally:
            browser.close()
    return out


def main():
    args = parse_args()
    year = max(2012, min(2026, args.year))
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    run_id = args.run_id.strip() or datetime.now().strftime("nar_v13_5_%Y%m%d_%H%M%S")

    if args.month.lower() == "all":
        start_dt, end_dt = datetime(year, 1, 1), min(datetime(year, 12, 31), today - timedelta(days=1))
        period_tag, month_val, week_label = f"{year}_all", "all", "年間"
    else:
        m_int = max(1, min(12, int(args.month)))
        last_d = calendar.monthrange(year, m_int)[1]
        d1, d2 = {"1": (1, 7), "2": (8, 14), "3": (15, 21), "4": (22, last_d)}.get(args.week, (1, last_d))
        start_dt, end_dt = datetime(year, m_int, d1), min(datetime(year, m_int, min(d2, last_d)), today - timedelta(days=1))
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

    target_dates, cur = [], start_dt
    while cur <= end_dt:
        target_dates.append(cur)
        cur += timedelta(days=1)

    missing = [d for d in target_dates if args.no_cache or (d.strftime("%Y%m%d") not in cached_by_date)]
    used_cache = len(missing) == 0

    if used_cache and args.week in ("1", "2", "3"):
        print(f"⚡ [キャッシュ済] 第{args.week}週はスキップし第4週で一括検証します。", flush=True)
        sys.exit(0)

    if not used_cache:
        n_workers = min(3, len(missing))
        chunks = [missing[i::n_workers] for i in range(n_workers)]
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(_worker_fetch, i + 1, ch, args.venue) for i, ch in enumerate(chunks) if ch]
            for fut in as_completed(futs):
                cached_by_date.update(fut.result())
        merged = [r for dk in sorted(cached_by_date.keys()) for r in cached_by_date[dk]]
        if merged:
            json.dump(merged, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)

    all_races = [r for dk in sorted(cached_by_date.keys()) if dk <= end_dt.strftime("%Y%m%d") for r in cached_by_date[dk]]
    if not all_races:
        print("❌ 取得レース数0件", flush=True)
        sys.exit(1)

    src = "🟢 地方キャッシュ読込" if used_cache else "楽天競馬 実取得(3並列/脚質・馬場付)"
    if args.mode == "grid100":
        results = [(name, run_progression_sim(all_races, fn)) for name, fn in build_100_strategies("枠複")]
        results.sort(key=lambda x: (x[1]["daily_stats"]["recovery_rate"], x[1]["daily_stats"]["net_profit"]), reverse=True)
        best_name, best_res = results[0]
        top20 = {
            f"TOP{i+1}: {nm}": {"races": rs["races"], "hits": rs["hits"], "bet": rs["daily_stats"]["total_bet"], "payout": rs["daily_stats"]["total_payout"]}
            for i, (nm, rs) in enumerate(results[:20])
        }
        save_dry_run_dashboard(
            run_id, year, month_val, week_label, args.venue,
            f"v13.5 地方 {year}年{args.month}月 100種1位 [{best_name}] ({week_label})",
            f"{src}＋100種総当たり", len(all_races), best_res, override_prog_stats=top20,
        )
        print(f"🏆 [v13.5 地方100種1位] {best_name} / 回収率 {best_res['daily_stats']['recovery_rate']}%", flush=True)
    else:
        sim_res = run_progression_sim(all_races, lambda r: evaluate_v12_standard(r, "枠複"), log_skips=True)
        save_dry_run_dashboard(
            run_id, year, month_val, week_label, args.venue,
            f"v13.5 地方 {year}年{args.month}月 10段階ロジット(T1-T10)検証 ({week_label})",
            f"{src}＋v13.5", len(all_races), sim_res,
        )
        print(f"🧪 [v13.5 地方標準] 回収率 {sim_res['daily_stats']['recovery_rate']}%", flush=True)


if __name__ == "__main__":
    main()
