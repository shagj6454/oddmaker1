import re
from typing import Dict

class RealtimeOddsExtractor:
    '''
    動画2 (sp.jra.jp) および 動画3 (keiba.rakuten.co.jp/odds/wakufuku, wakutan, umatan) の画面構造に対応した
    JRA・地方競馬（楽天競馬）ハイブリッドオッズ抽出クラス (v11.9: 枠連/枠複 ＋ 枠単優先・馬単フォールバック)
    '''
    def __init__(self, logger, headless: bool = True):
        self.logger = logger
        self.headless = headless

    def fetch_race_data(self, mode: str, race_id: str, mock: bool = False) -> Dict:
        if mock:
            self.logger.info(f"[MOCKモード] {mode.upper()} race_id={race_id} のテスト用オッズデータ(枠連＋枠単＋馬単)を生成します")
            return self._get_mock_data(mode, race_id)

        if mode == "jra":
            return self._fetch_jra_odds(race_id)
        elif mode == "nar":
            return self._fetch_nar_rakuten_odds(race_id)
        else:
            raise ValueError(f"未対応のモードです: {mode}")

    def _get_mock_data(self, mode: str, race_id: str) -> Dict:
        race_num = int(race_id[-2:]) if race_id[-2:].isdigit() else 1
        venue = "東京" if mode == "jra" else "大井"
        is_fav1x = (mode == "nar")
        return {
            "mode": mode,
            "race_id": race_id,
            "venue": venue,
            "race_num": race_num,
            "is_obstacle": False,
            "num_horses": 14,
            "fav1_win_odds": 1.4 if is_fav1x else 2.8,
            "fav1_frame_no": 5,
            "fav1_horse_no": 7,
            "has_scratch": False,
            "wakuren_combos": [
                {"w1": 5, "w2": 6, "odds": 2.7, "pop": 1},
                {"w1": 5, "w2": 8, "odds": 6.3, "pop": 2},
                {"w1": 6, "w2": 8, "odds": 6.8, "pop": 3},
                {"w1": 1, "w2": 5, "odds": 10.3, "pop": 4},
                {"w1": 4, "w2": 5, "odds": 14.5, "pop": 5},
                {"w1": 4, "w2": 8, "odds": 18.2, "pop": 6},
                {"w1": 1, "w2": 6, "odds": 22.1, "pop": 7},
                {"w1": 1, "w2": 8, "odds": 26.4, "pop": 8},
                {"w1": 6, "w2": 7, "odds": 30.4, "pop": 9},
                {"w1": 4, "w2": 6, "odds": 37.6, "pop": 10},
                {"w1": 5, "w2": 7, "odds": 38.1, "pop": 11},
                {"w1": 3, "w2": 6, "odds": 47.2, "pop": 12},
            ],
            "wakutan_combos": [
                {"w1": 5, "w2": 7, "odds": 4.8, "pop": 1},
                {"w1": 5, "w2": 8, "odds": 6.2, "pop": 2},
                {"w1": 5, "w2": 4, "odds": 8.9, "pop": 3},
                {"w1": 5, "w2": 1, "odds": 11.4, "pop": 4},
                {"w1": 5, "w2": 5, "odds": 14.0, "pop": 5},
            ] if mode == "nar" else [],
            "umatan_combos": [
                {"w1": 7, "w2": 9, "odds": 5.8, "pop": 1},
                {"w1": 7, "w2": 3, "odds": 7.4, "pop": 2},
                {"w1": 7, "w2": 11, "odds": 9.6, "pop": 3},
                {"w1": 7, "w2": 4, "odds": 12.8, "pop": 4},
                {"w1": 7, "w2": 14, "odds": 16.5, "pop": 5},
                {"w1": 7, "w2": 2, "odds": 24.0, "pop": 6},
            ],
        }

    def _fetch_jra_odds(self, race_id: str) -> Dict:
        from playwright.sync_api import sync_playwright
        self.logger.info(f"JRAオッズ取得開始(JRAは枠単なしのため馬単フォールバック対応): race_id={race_id}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            page.goto("https://sp.jra.jp/", timeout=30000)
            body_text = page.inner_text("body")
            browser.close()
        return self.parse_raw_odds_text(body_text, mode="jra", race_id=race_id)

    def _fetch_nar_rakuten_odds(self, race_id: str) -> Dict:
        from playwright.sync_api import sync_playwright
        url_wf = f"https://keiba.rakuten.co.jp/odds/wakufuku/RACEID/{race_id}"
        url_wt = f"https://keiba.rakuten.co.jp/odds/wakutan/RACEID/{race_id}"
        url_ut = f"https://keiba.rakuten.co.jp/odds/umatan/RACEID/{race_id}"
        self.logger.info(f"楽天競馬(地方) 枠複・枠単・馬単オッズ取得開始: {race_id}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            page.goto(url_wf, timeout=30000)
            page.wait_for_timeout(1200)
            wf_text = page.inner_text("body")

            page.goto(url_wt, timeout=30000)
            page.wait_for_timeout(1200)
            wt_text = page.inner_text("body")
            wt_combos = self.parse_ordered_odds_text(wt_text, max_num=8)

            ut_combos = []
            if len(wt_combos) < 3:
                page.goto(url_ut, timeout=30000)
                page.wait_for_timeout(1200)
                ut_text = page.inner_text("body")
                ut_combos = self.parse_ordered_odds_text(ut_text, max_num=18)

            browser.close()
        parsed = self.parse_raw_odds_text(wf_text, mode="nar", race_id=race_id)
        parsed["wakutan_combos"] = wt_combos
        parsed["umatan_combos"] = ut_combos
        return parsed

    def parse_ordered_odds_text(self, text: str, max_num: int):
        combos = []
        for m in re.finditer(r"(?:^|\s)(\d{1,3})\s*(?:位|番人気)?\s+(\d{1,2})\s*[-－→]\s*(\d{1,2})\s+(\d+(?:\.\d+)?)", text):
            pop, f_no, s_no, odds = int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))
            valid_pair = (f_no != s_no) if max_num > 8 else True
            if 1 <= f_no <= max_num and 1 <= s_no <= max_num and valid_pair and 0 < odds < 9999:
                if not any(c["w1"] == f_no and c["w2"] == s_no for c in combos):
                    combos.append({"pop": pop, "w1": f_no, "w2": s_no, "odds": odds})
        return combos

    def parse_raw_odds_text(self, text: str, mode: str, race_id: str) -> Dict:
        cleaned_nav = re.sub(r"帯広ば[\s\S]{0,120}?佐\s*賀", "", text).replace("競馬カナザワ", "")
        m_venue = re.search(
            r"(帯広ば|門別|盛岡|水沢|浦和|船橋|大井|川崎|金沢|笠松|名古屋|園田|姫路|高知|佐賀|東京|京都|阪神|中山|中京|新潟|福島|小倉|札幌|函館)",
            cleaned_nav,
        )
        venue = m_venue.group(1) if m_venue else ("東京" if mode == "jra" else "大井")

        race_num = int(race_id[-2:]) if len(race_id) >= 2 and race_id[-2:].isdigit() else 1
        is_obstacle = bool(re.search(r"障害|ジャンプ[SＳ]|J･G|ばんえい", text)) or venue == "帯広ば"

        m_horses = re.search(r"(\d{1,2})\s*頭", text)
        num_horses = int(m_horses.group(1)) if m_horses else 14

        cleaned_scratch = (
            text.replace("競走除外が発生した場合", "")
            .replace("出走取消・競走除外等", "")
            .replace("取消・除外された場合", "")
        )
        has_scratch = bool(re.search(r"(?:出走取消|競走除外|\b取消\b|\b除外\b|欠場)", cleaned_scratch))

        combos = []
        for m in re.finditer(r"(?:^|\s)(\d{1,2})\s*(?:位|番人気)?\s+([1-8])\s*[-－]\s*([1-8])\s+(\d+(?:\.\d+)?)", text):
            pop, a, b, odds = int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))
            lo, hi = min(a, b), max(a, b)
            if 0 < odds < 9999 and not any(c["w1"] == lo and c["w2"] == hi for c in combos):
                combos.append({"pop": pop, "w1": lo, "w2": hi, "odds": odds})

        return {
            "mode": mode,
            "race_id": race_id,
            "venue": venue,
            "race_num": race_num,
            "is_obstacle": is_obstacle,
            "num_horses": num_horses,
            "fav1_win_odds": 2.5,
            "fav1_frame_no": None,
            "fav1_horse_no": None,
            "has_scratch": has_scratch,
            "wakuren_combos": combos,
            "wakutan_combos": [],
            "umatan_combos": [],
        }
