import re
from datetime import datetime
from typing import Dict, List, Optional

TODAY_SCHEDULE_20261120 = [
    {"venue": "園田", "race_num": 1,  "post_time": "10:40", "num_horses": 10},
    {"venue": "園田", "race_num": 2,  "post_time": "11:10", "num_horses": 8},
    {"venue": "園田", "race_num": 3,  "post_time": "11:40", "num_horses": 7},
    {"venue": "園田", "race_num": 4,  "post_time": "12:10", "num_horses": 10},
    {"venue": "園田", "race_num": 5,  "post_time": "12:40", "num_horses": 10},
    {"venue": "園田", "race_num": 6,  "post_time": "13:15", "num_horses": 10},
    {"venue": "園田", "race_num": 7,  "post_time": "13:50", "num_horses": 10},
    {"venue": "園田", "race_num": 8,  "post_time": "14:25", "num_horses": 9},
    {"venue": "大井", "race_num": 1,  "post_time": "14:30", "num_horses": 13},
    {"venue": "園田", "race_num": 9,  "post_time": "15:00", "num_horses": 9},
    {"venue": "大井", "race_num": 2,  "post_time": "15:05", "num_horses": 14},
    {"venue": "園田", "race_num": 10, "post_time": "15:35", "num_horses": 12},
    {"venue": "大井", "race_num": 3,  "post_time": "15:40", "num_horses": 14},
    {"venue": "園田", "race_num": 11, "post_time": "16:10", "num_horses": 12},
    {"venue": "大井", "race_num": 4,  "post_time": "16:15", "num_horses": 14},
    {"venue": "園田", "race_num": 12, "post_time": "16:45", "num_horses": 12},
    {"venue": "大井", "race_num": 5,  "post_time": "16:50", "num_horses": 12},
    {"venue": "大井", "race_num": 6,  "post_time": "17:25", "num_horses": 14},
    {"venue": "大井", "race_num": 7,  "post_time": "18:00", "num_horses": 16},
    {"venue": "大井", "race_num": 8,  "post_time": "18:35", "num_horses": 14},
    {"venue": "大井", "race_num": 9,  "post_time": "19:10", "num_horses": 14},
    {"venue": "大井", "race_num": 10, "post_time": "19:45", "num_horses": 16},
    {"venue": "大井", "race_num": 11, "post_time": "20:15", "num_horses": 16},
    {"venue": "大井", "race_num": 12, "post_time": "20:50", "num_horses": 16},
]


class RealtimeOddsExtractor:
    """
    楽天競馬 PC版レイアウト (keiba.rakuten.co.jp) 対応スクレイパー (v11.9)
    """

    def __init__(self, logger, headless: bool = True):
        self.logger = logger
        self.headless = headless

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

        target_label = "枠複"
        if bet_type == "枠単":
            target_label = "枠単"
        elif bet_type == "馬単":
            target_label = "馬単"

        m_pay = re.search(rf"{target_label}\s+(\d{{1,2}})\s*[-－→]\s*(\d{{1,2}})\s+([\d,]+)\s*円", block)
        if not m_pay:
            return None

        w1 = int(m_pay.group(1))
        w2 = int(m_pay.group(2))
        dividend_yen = int(m_pay.group(3).replace(",", ""))
        return {"w1": w1, "w2": w2, "dividend_yen": dividend_yen}

    def fetch_nar_today_schedule(self, page) -> List[Dict]:
        page.goto("https://keiba.rakuten.co.jp/race_card/list/R/", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)

        schedule = []
        venue_links = page.locator(
            'a:has-text("大井"), a:has-text("園田"), a:has-text("川崎"), a:has-text("船橋"), '
            'a:has-text("浦和"), a:has-text("名古屋"), a:has-text("高知"), a:has-text("佐賀"), '
            'a:has-text("門別"), a:has-text("水沢"), a:has-text("盛岡"), a:has-text("姫路")'
        )
        seen_venues = set()
        for i in range(venue_links.count()):
            v_text = venue_links.nth(i).inner_text().strip()
            m_v = re.search(r"(大井|園田|川崎|船橋|浦和|名古屋|高知|佐賀|門別|水沢|盛岡|姫路)", v_text)
            if not m_v or m_v.group(1) in seen_venues:
                continue
            venue = m_v.group(1)
            seen_venues.add(venue)
            venue_links.nth(i).click()
            page.wait_for_timeout(1200)

            rows = page.locator("table tr")
            for r_idx in range(rows.count()):
                row_txt = rows.nth(r_idx).inner_text()
                m_row = re.search(r"(\d{1,2})R\s+(\d{1,2}:\d{2})[\s\S]*?(\d{1,2})頭", row_txt)
                if m_row:
                    schedule.append({
                        "venue": venue,
                        "race_num": int(m_row.group(1)),
                        "post_time": m_row.group(2),
                        "num_horses": int(m_row.group(3)),
                    })

        return schedule if schedule else TODAY_SCHEDULE_20261120

    def fetch_nar_race_snapshot(self, page, venue: str, race_num: int) -> Dict:
        page.goto("https://keiba.rakuten.co.jp/odds/tanfuku/R/", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1000)

        venue_tab = page.locator(f'a:has-text("{venue}"), li:has-text("{venue}")').first
        if venue_tab.count() > 0:
            venue_tab.click()
            page.wait_for_timeout(1000)

        race_tab = page.locator(f'a:has-text("{race_num}R"), li:has-text("{race_num}R")').first
        if race_tab.count() > 0:
            race_tab.click()
            page.wait_for_timeout(1000)

        body_tan = page.inner_text("body")
        m_horses = re.search(r"(\d{1,2})\s*頭", body_tan)
        num_horses = int(m_horses.group(1)) if m_horses else 14
        has_scratch = bool(
            re.search(r"(?:出走取消|競走除外|\b取消\b|\b除外\b)", body_tan.replace("取消・除外された場合", ""))
        )

        horses_win = []
        rows = page.locator("table tr")
        for i in range(rows.count()):
            txt = rows.nth(i).inner_text().strip()
            m_h = re.match(r"^([1-8])\s+(\d{1,2})\s+[^\n\d]+.*?(\d+\.\d)", txt.replace("\n", " "))
            if m_h:
                waku = int(m_h.group(1))
                umaban = int(m_h.group(2))
                win_odds = float(m_h.group(3))
                if not any(h["horse_no"] == umaban for h in horses_win):
                    horses_win.append({"frame_no": waku, "horse_no": umaban, "win_odds": win_odds})

        valid_wins = [h for h in horses_win if h["win_odds"] > 0.0]
        is_odds_released = len(valid_wins) >= 2

        if is_odds_released:
            valid_wins.sort(key=lambda x: x["win_odds"])
            fav1 = valid_wins[0]
            fav1_win_odds = fav1["win_odds"]
            fav1_frame_no = fav1["frame_no"]
            fav1_horse_no = fav1["horse_no"]
        else:
            fav1_win_odds = 0.0
            fav1_frame_no = None
            fav1_horse_no = None

        wakuren_combos = []
        wf_tab = page.locator('a:has-text("枠複")').first
        if wf_tab.count() > 0:
            wf_tab.click()
            page.wait_for_timeout(1000)
            pop_tab = page.locator('a:has-text("人気順"), button:has-text("人気順")').first
            if pop_tab.count() > 0:
                pop_tab.click()
                page.wait_for_timeout(800)
            wf_text = page.inner_text("body")
            wakuren_combos = self._parse_pair_odds(wf_text, ordered=False, max_num=8)

        wakutan_combos = []
        umatan_combos = []
        if is_odds_released and (num_horses <= 11 or fav1_win_odds < 2.0):
            wt_tab = page.locator('a:has-text("枠単")').first
            if wt_tab.count() > 0:
                wt_tab.click()
                page.wait_for_timeout(1000)
                pop_tab = page.locator('a:has-text("人気順"), button:has-text("人気順")').first
                if pop_tab.count() > 0:
                    pop_tab.click()
                    page.wait_for_timeout(800)
                wt_text = page.inner_text("body")
                wakutan_combos = self._parse_pair_odds(wt_text, ordered=True, max_num=8)

            if len(wakutan_combos) < 3:
                ut_tab = page.locator('a:has-text("馬単")').first
                if ut_tab.count() > 0:
                    ut_tab.click()
                    page.wait_for_timeout(1000)
                    pop_tab = page.locator('a:has-text("人気順"), button:has-text("人気順")').first
                    if pop_tab.count() > 0:
                        pop_tab.click()
                        page.wait_for_timeout(800)
                    ut_text = page.inner_text("body")
                    umatan_combos = self._parse_pair_odds(ut_text, ordered=True, max_num=18)

        return {
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

    def _parse_pair_odds(self, text: str, ordered: bool, max_num: int) -> List[Dict]:
        combos = []
        for m in re.finditer(
            r"(?:^|\s)(\d{1,3})\s*(?:位|番人気)?\s+(\d{1,2})\s*[-－→]\s*(\d{1,2})\s+(\d+(?:\.\d+)?)",
            text,
        ):
            pop, a, b, odds = int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))
            if not (1 <= a <= max_num and 1 <= b <= max_num):
                continue
            if not ordered:
                a, b = min(a, b), max(a, b)
            elif max_num > 8 and a == b:
                continue
            if odds > 0.0 and odds < 9999 and not any(c["w1"] == a and c["w2"] == b for c in combos):
                combos.append({"pop": pop, "w1": a, "w2": b, "odds": odds})
        return combos
