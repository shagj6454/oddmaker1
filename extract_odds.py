import re
from datetime import datetime
from typing import Dict, List, Optional

# 取得失敗時または --mock 実行時のフォールバック用スケジュール
FALLBACK_SCHEDULE = [
    {"org": "NAR", "venue": "園田", "race_num": 1,  "post_time": "10:40", "num_horses": 10},
    {"org": "NAR", "venue": "笠松", "race_num": 1,  "post_time": "10:50", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 2,  "post_time": "11:10", "num_horses": 8},
    {"org": "NAR", "venue": "笠松", "race_num": 2,  "post_time": "11:20", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 3,  "post_time": "11:40", "num_horses": 8},
    {"org": "NAR", "venue": "笠松", "race_num": 3,  "post_time": "11:55", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 4,  "post_time": "12:10", "num_horses": 10},
    {"org": "NAR", "venue": "笠松", "race_num": 4,  "post_time": "12:25", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 5,  "post_time": "12:40", "num_horses": 10},
    {"org": "NAR", "venue": "笠松", "race_num": 5,  "post_time": "13:00", "num_horses": 9},
    {"org": "NAR", "venue": "笠松", "race_num": 6,  "post_time": "13:10", "num_horses": 9},
    {"org": "NAR", "venue": "笠松", "race_num": 7,  "post_time": "13:30", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 7,  "post_time": "13:40", "num_horses": 10},
    {"org": "NAR", "venue": "笠松", "race_num": 8,  "post_time": "14:05", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 8,  "post_time": "14:15", "num_horses": 9},
    {"org": "NAR", "venue": "大井", "race_num": 1,  "post_time": "14:30", "num_horses": 13},
    {"org": "NAR", "venue": "笠松", "race_num": 9,  "post_time": "14:40", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 9,  "post_time": "14:50", "num_horses": 9},
    {"org": "NAR", "venue": "大井", "race_num": 2,  "post_time": "15:05", "num_horses": 14},
    {"org": "NAR", "venue": "笠松", "race_num": 10, "post_time": "15:15", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 10, "post_time": "15:25", "num_horses": 12},
    {"org": "NAR", "venue": "大井", "race_num": 3,  "post_time": "15:40", "num_horses": 14},
    {"org": "NAR", "venue": "笠松", "race_num": 11, "post_time": "15:50", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 11, "post_time": "16:00", "num_horses": 12},
    {"org": "NAR", "venue": "大井", "race_num": 4,  "post_time": "16:15", "num_horses": 14},
    {"org": "NAR", "venue": "笠松", "race_num": 12, "post_time": "16:25", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 12, "post_time": "16:35", "num_horses": 12},
    {"org": "NAR", "venue": "大井", "race_num": 5,  "post_time": "16:50", "num_horses": 12},
    {"org": "NAR", "venue": "大井", "race_num": 6,  "post_time": "17:25", "num_horses": 14},
    {"org": "NAR", "venue": "大井", "race_num": 7,  "post_time": "18:00", "num_horses": 16},
    {"org": "NAR", "venue": "大井", "race_num": 8,  "post_time": "18:35", "num_horses": 14},
    {"org": "NAR", "venue": "大井", "race_num": 9,  "post_time": "19:10", "num_horses": 14},
    {"org": "NAR", "venue": "大井", "race_num": 10, "post_time": "19:45", "num_horses": 16},
    {"org": "NAR", "venue": "大井", "race_num": 11, "post_time": "20:15", "num_horses": 16},
    {"org": "NAR", "venue": "大井", "race_num": 12, "post_time": "20:50", "num_horses": 16},
]

JRA_VENUES = ("東京", "中山", "京都", "阪神", "中京", "新潟", "福島", "小倉", "札幌", "函館")
NAR_VENUES = ("大井", "園田", "笠松", "川崎", "船橋", "浦和", "名古屋", "高知", "佐賀", "門別", "水沢", "盛岡", "姫路")


class RealtimeOddsExtractor:
    """
    NAR（楽天競馬：平日/毎日・笠松含む）＆ JRA（中央競馬：土日）両対応スクレイパー (v11.9)
    - 楽天競馬トップページ経由でのオッズ・出馬表・スケジュール取得（404回避）
    - th/td 両対応のテーブル解析
    - 未取得時のデバッグ画面キャプチャ自動保存
    """

    def __init__(self, logger, headless: bool = True):
        self.logger = logger
        self.headless = headless

    # =========================================================================
    # 共通：結果照合・スケジュール取得のディスパッチャ
    # =========================================================================
    def fetch_race_result(self, page, org: str, venue: str, race_num: int, bet_type: str) -> Optional[Dict]:
        if org == "JRA":
            return self.fetch_jra_race_result(page, venue, race_num, bet_type)
        return self.fetch_nar_race_result(page, venue, race_num, bet_type)

    def fetch_race_snapshot(self, page, org: str, venue: str, race_num: int) -> Dict:
        if org == "JRA":
            return self.fetch_jra_race_snapshot(page, venue, race_num)
        return self.fetch_nar_race_snapshot(page, venue, race_num)

    # =========================================================================
    # NAR（楽天競馬：平日・毎日）
    # =========================================================================
    def fetch_nar_race_result(self, page, venue: str, race_num: int, bet_type: str) -> Optional[Dict]:
        page.goto("https://keiba.rakuten.co.jp/refund/", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1200)

        venue_link = page.locator(f'a:has-text("{venue}")').first
        if venue_link.count() > 0:
            venue_link.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

        body_txt = page.inner_text("body")
        m_race = re.search(rf"■\s*{race_num}R\b", body_txt)
        if not m_race:
            return None

        after = body_txt[m_race.start():]
        m_next = re.search(r"■\s*(?:\d{1,2}R|本日の発売情報)", after[4:])
        block = after[: m_next.start() + 4] if m_next else after

        target_label = "枠単" if bet_type == "枠単" else ("馬単" if bet_type == "馬単" else "枠複")
        m_pay = re.search(rf"{target_label}\s+(\d{{1,2}})\s*[-－→]\s*(\d{{1,2}})\s+([\d,]+)\s*円", block)
        if not m_pay:
            return None

        return {
            "w1": int(m_pay.group(1)),
            "w2": int(m_pay.group(2)),
            "dividend_yen": int(m_pay.group(3).replace(",", "")),
        }

    def fetch_nar_today_schedule(self, page) -> List[Dict]:
        schedule: List[Dict] = []
        try:
            page.goto("https://keiba.rakuten.co.jp/", timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

            # 1. トップページの「本日の開催情報（直近の発走状況）」から各会場の直近レースを抽出
            body_txt = page.inner_text("body")
            for v in NAR_VENUES:
                m_top = re.search(
                    rf"\b{v}\b[\s\S]{{0,120}}?(\d{{1,2}})\s*(?:レース|R)\s*(\d{{1,2}}:\d{{2}})\s*発走",
                    body_txt,
                )
                if m_top:
                    rnum = int(m_top.group(1))
                    ptime = m_top.group(2)
                    if not any(s["venue"] == v and s["race_num"] == rnum for s in schedule):
                        schedule.append({
                            "org": "NAR",
                            "venue": v,
                            "race_num": rnum,
                            "post_time": ptime,
                            "num_horses": 10,
                        })

            # 2. 「出馬表」ページから全1R〜12Rの発走時刻も取得できれば追加
            card_nav = page.locator('a:has-text("出馬表")').first
            if card_nav.count() > 0:
                card_nav.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(1200)

                venue_selector = ", ".join(f'a:has-text("{v}")' for v in NAR_VENUES)
                venue_links = page.locator(venue_selector)
                seen_venues = set()
                for i in range(venue_links.count()):
                    v_text = venue_links.nth(i).inner_text().strip()
                    m_v = re.search(r"(" + "|".join(NAR_VENUES) + r")", v_text)
                    if not m_v or m_v.group(1) in seen_venues:
                        continue
                    venue = m_v.group(1)
                    seen_venues.add(venue)
                    venue_links.nth(i).click()
                    page.wait_for_timeout(1000)

                    rows = page.locator("table tr")
                    for r_idx in range(rows.count()):
                        row_txt = rows.nth(r_idx).inner_text()
                        m_row = re.search(r"(\d{1,2})R\s+(\d{1,2}:\d{2})", row_txt)
                        if m_row:
                            rnum = int(m_row.group(1))
                            ptime = m_row.group(2)
                            m_h = re.search(r"(\d{1,2})頭", row_txt)
                            nh = int(m_h.group(1)) if m_h else 10
                            # 同一会場・同一レースは出馬表側で上書き更新
                            schedule = [
                                s for s in schedule
                                if not (s["venue"] == venue and s["race_num"] == rnum)
                            ]
                            schedule.append({
                                "org": "NAR",
                                "venue": venue,
                                "race_num": rnum,
                                "post_time": ptime,
                                "num_horses": nh,
                            })
        except Exception as e:
            self.logger.warning(f"⚠️ NAR当日スケジュール取得時に例外が発生しました: {e}")

        if schedule:
            return schedule

        # 動的取得が空の場合のみフォールバックを使用（笠松を含む）
        return FALLBACK_SCHEDULE

    def _extract_win_odds_from_page(self, page) -> List[Dict]:
        horses_win = []
        rows = page.locator("table tr")
        for i in range(rows.count()):
            cells = [c.strip() for c in rows.nth(i).locator("th, td").all_inner_texts()]
            if len(cells) < 4:
                continue
            if not (cells[0].isdigit() and cells[1].isdigit()):
                continue
            c0 = int(cells[0])
            umaban = int(cells[1])
            if not (1 <= umaban <= 18):
                continue

            win_odds = None
            for cell in cells[2:]:
                if "-" in cell or "－" in cell:
                    continue
                m_o = re.fullmatch(r"(\d+\.\d+)", cell)
                if m_o:
                    win_odds = float(m_o.group(1))
                    break

            if win_odds is None:
                continue

            frame_no = c0 if (1 <= c0 <= 8) else None
            if not any(h["horse_no"] == umaban for h in horses_win):
                horses_win.append({"frame_no": frame_no, "horse_no": umaban, "win_odds": win_odds})

        if len(horses_win) < 2:
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                page.screenshot(path=f"../logs/odds_debug_{ts}.png", full_page=True)
                self.logger.warning(f"📸 オッズ未取得デバッグ画像保存: logs/odds_debug_{ts}.png (URL: {page.url})")
            except Exception:
                pass

        return horses_win

    def _extract_pair_odds_from_page(self, page, ordered: bool, max_num: int) -> List[Dict]:
        combos = []
        rows = page.locator("table tr")
        for i in range(rows.count()):
            cells = [c.strip() for c in rows.nth(i).locator("th, td").all_inner_texts()]
            if len(cells) < 3 or not cells[0].isdigit():
                continue
            pop = int(cells[0])
            m_pair = re.search(r"(\d{1,2})\s*[-－→]\s*(\d{1,2})", cells[1])
            m_odds = re.fullmatch(r"(\d+(?:\.\d+)?)", cells[2])
            if m_pair and m_odds:
                a, b, odds = int(m_pair.group(1)), int(m_pair.group(2)), float(m_odds.group(1))
                if not (1 <= a <= max_num and 1 <= b <= max_num):
                    continue
                if not ordered:
                    a, b = min(a, b), max(a, b)
                elif max_num > 8 and a == b:
                    continue
                if 0.0 < odds < 9999 and not any(c["w1"] == a and c["w2"] == b for c in combos):
                    combos.append({"pop": pop, "w1": a, "w2": b, "odds": odds})

        if len(combos) >= 3:
            return combos
        return self._parse_pair_odds(page.inner_text("body"), ordered=ordered, max_num=max_num)

    def fetch_nar_race_snapshot(self, page, venue: str, race_num: int) -> Dict:
        page.goto("https://keiba.rakuten.co.jp/", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1000)

        odds_nav = page.locator('a:has-text("オッズ")').first
        if odds_nav.count() > 0:
            odds_nav.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

        venue_tab = page.locator(f'a:has-text("{venue}"), li:has-text("{venue}")').first
        if venue_tab.count() > 0:
            venue_tab.click()
            page.wait_for_timeout(1000)

        race_tab = page.locator(f'a:has-text("{race_num}R"), li:has-text("{race_num}R")').first
        if race_tab.count() > 0:
            race_tab.click()
            page.wait_for_timeout(1000)

        body_tan = page.inner_text("body")
        has_scratch = bool(
            re.search(r"(?:出走取消|競走除外|\b取消\b|\b除外\b)", body_tan.replace("取消・除外された場合", ""))
        )

        horses_win = self._extract_win_odds_from_page(page)
        valid_wins = [h for h in horses_win if h["win_odds"] > 0.0]
        is_odds_released = len(valid_wins) >= 2

        m_horses = re.search(r"(\d{1,2})\s*頭", body_tan)
        num_horses = len(horses_win) if len(horses_win) >= 2 else (int(m_horses.group(1)) if m_horses else 14)

        if is_odds_released:
            valid_wins.sort(key=lambda x: x["win_odds"])
            fav1 = valid_wins[0]
            fav1_win_odds = fav1["win_odds"]
            fav1_horse_no = fav1["horse_no"]
            fav1_frame_no = fav1["frame_no"] if fav1["frame_no"] else (fav1_horse_no if num_horses <= 8 else None)
        else:
            fav1_win_odds, fav1_frame_no, fav1_horse_no = 0.0, None, None

        wakuren_combos = []
        wf_tab = page.locator('a:has-text("枠複")').first
        has_wakuren_tab = wf_tab.count() > 0

        if is_odds_released and has_wakuren_tab and num_horses >= 12 and fav1_win_odds >= 2.0:
            wf_tab.click()
            page.wait_for_timeout(1000)
            pop_tab = page.locator('a:has-text("人気高配当順"), a:has-text("人気順"), button:has-text("人気順")').first
            if pop_tab.count() > 0:
                pop_tab.click()
                page.wait_for_timeout(800)
            wakuren_combos = self._extract_pair_odds_from_page(page, ordered=False, max_num=8)

        wakutan_combos, umatan_combos = [], []
        need_axis_mode = (not has_wakuren_tab) or (num_horses <= 11) or (fav1_win_odds < 2.0)

        if is_odds_released and need_axis_mode:
            wt_tab = page.locator('a:has-text("枠単")').first
            if wt_tab.count() > 0:
                wt_tab.click()
                page.wait_for_timeout(1000)
                pop_tab = page.locator('a:has-text("人気高配当順"), a:has-text("人気順"), button:has-text("人気順")').first
                if pop_tab.count() > 0:
                    pop_tab.click()
                    page.wait_for_timeout(800)
                wakutan_combos = self._extract_pair_odds_from_page(page, ordered=True, max_num=8)

            if len(wakutan_combos) < 3:
                ut_tab = page.locator('a:has-text("馬単")').first
                if ut_tab.count() > 0:
                    ut_tab.click()
                    page.wait_for_timeout(1000)
                    pop_tab = page.locator('a:has-text("人気高配当順"), a:has-text("人気順"), button:has-text("人気順")').first
                    if pop_tab.count() > 0:
                        pop_tab.click()
                        page.wait_for_timeout(800)
                    umatan_combos = self._extract_pair_odds_from_page(page, ordered=True, max_num=18)

        return {
            "org": "NAR",
            "mode": "nar",
            "venue": venue,
            "race_num": race_num,
            "is_obstacle": False,
            "num_horses": num_horses,
            "is_odds_released": is_odds_released,
            "fav1_win_odds": fav1_win_odds,
            "fav1_frame_no": fav1_frame_no,
            "fav1_horse_no": fav1_horse_no,
            "has_scratch": has_scratch,
            "wakuren_combos": wakuren_combos,
            "wakutan_combos": wakutan_combos,
            "umatan_combos": umatan_combos,
        }

    # =========================================================================
    # JRA（中央競馬：土日開催）
    # =========================================================================
    def fetch_jra_today_schedule(self, page) -> List[Dict]:
        today_ymd = datetime.now().strftime("%Y%m%d")
        schedule = []
        try:
            page.goto(f"https://race.netkeiba.com/top/race_list.html?kaisai_date={today_ymd}", timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)

            blocks = page.locator(".RaceList_DataList")
            for b_idx in range(blocks.count()):
                block_txt = blocks.nth(b_idx).inner_text()
                m_v = re.search(r"(" + "|".join(JRA_VENUES) + r")", block_txt)
                if not m_v:
                    continue
                venue = m_v.group(1)
                items = blocks.nth(b_idx).locator("li.RaceList_DataItem")
                for i in range(items.count()):
                    it_txt = items.nth(i).inner_text()
                    m_r = re.search(r"(\d{1,2})R", it_txt)
                    m_t = re.search(r"(\d{1,2}:\d{2})", it_txt)
                    m_h = re.search(r"(\d{1,2})頭", it_txt)
                    if m_r and m_t:
                        schedule.append({
                            "org": "JRA",
                            "venue": venue,
                            "race_num": int(m_r.group(1)),
                            "post_time": m_t.group(1),
                            "num_horses": int(m_h.group(1)) if m_h else 14,
                            "is_obstacle": bool(re.search(r"障\d+", it_txt)),
                        })
        except Exception as e:
            self.logger.warning(f"⚠️ JRA当日スケジュール取得時に例外が発生しました: {e}")
        return schedule

    def fetch_jra_race_snapshot(self, page, venue: str, race_num: int) -> Dict:
        page.goto("https://www.jra.go.jp/JRADB/accessO.html", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1200)

        v_link = page.locator(f'a:has-text("{venue}")').first
        if v_link.count() > 0:
            v_link.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1000)

        r_link = page.locator(f'a:has-text("{race_num}R")').first
        if r_link.count() > 0:
            r_link.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1000)

        body_txt = page.inner_text("body")
        is_obstacle = bool(re.search(r"障害|ジャンプ", body_txt))
        has_scratch = bool(re.search(r"(?:出走取消|競走除外|\b取消\b|\b除外\b)", body_txt.replace("取消・除外された場合", "")))

        horses_win = self._extract_win_odds_from_page(page)
        valid_wins = [h for h in horses_win if h["win_odds"] > 0.0]
        is_odds_released = len(valid_wins) >= 2
        num_horses = len(horses_win) if len(horses_win) >= 2 else 14

        if is_odds_released:
            valid_wins.sort(key=lambda x: x["win_odds"])
            fav1 = valid_wins[0]
            fav1_win_odds, fav1_horse_no, fav1_frame_no = fav1["win_odds"], fav1["horse_no"], fav1["frame_no"]
        else:
            fav1_win_odds, fav1_frame_no, fav1_horse_no = 0.0, None, None

        wakuren_combos, umatan_combos = [], []
        if is_odds_released:
            if num_horses >= 12 and fav1_win_odds >= 2.0:
                wr_tab = page.locator('a:has-text("枠連")').first
                if wr_tab.count() > 0:
                    wr_tab.click()
                    pa
