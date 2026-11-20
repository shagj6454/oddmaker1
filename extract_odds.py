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
    {"org": "NAR", "venue": "笠松", "race_num": 8,  "post_time": "14:00", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 8,  "post_time": "14:10", "num_horses": 10},
    {"org": "NAR", "venue": "大井", "race_num": 1,  "post_time": "14:30", "num_horses": 11},
    {"org": "NAR", "venue": "笠松", "race_num": 9,  "post_time": "14:35", "num_horses": 9},
    {"org": "NAR", "venue": "園田", "race_num": 9,  "post_time": "14:45", "num_horses": 10},
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
NAR_VENUES = ("大井", "笠松", "園田", "川崎", "船橋", "浦和", "名古屋", "高知", "佐賀", "門別", "水沢", "盛岡", "姫路")


class RealtimeOddsExtractor:
    """
    NAR（楽天競馬：平日/毎日・笠松/園田/大井対応）＆ JRA（中央競馬：土日）スクレイパー (v11.9)
    - 動画の実際のオッズ画面（単勝・枠複・馬単 / 人気順）構造に準拠
    - race_card/list への誤遷移を回避し、オッズ画面へ直接遷移
    """

    def __init__(self, logger, headless: bool = True):
        self.logger = logger
        self.headless = headless

    def fetch_race_result(self, page, org: str, venue: str, race_num: int, bet_type: str) -> Optional[Dict]:
        if org == "JRA":
            return self.fetch_jra_race_result(page, venue, race_num, bet_type)
        return self.fetch_nar_race_result(page, venue, race_num, bet_type)

    def fetch_race_snapshot(self, page, org: str, venue: str, race_num: int) -> Dict:
        if org == "JRA":
            return self.fetch_jra_race_snapshot(page, venue, race_num)
        return self.fetch_nar_race_snapshot(page, venue, race_num)

    def fetch_nar_race_result(self, page, venue: str, race_num: int, bet_type: str) -> Optional[Dict]:
        page.goto("https://keiba.rakuten.co.jp/refund/", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1200)

        venue_link = page.locator(f'a:has-text("{venue}"):visible').first
        if venue_link.count() > 0:
            venue_link.click(timeout=5000)
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
            page.goto("https://keiba.rakuten.co.jp/odds/tanfuku/R/", timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

            rows = page.locator("table tr")
            for i in range(rows.count()):
                txt = re.sub(r"\s+", " ", rows.nth(i).inner_text()).strip()
                m_v = re.search(r"(" + "|".join(NAR_VENUES) + r")", txt)
                m_r = re.search(r"(\d{1,2})\s*(?:レース|R)", txt)
                m_t = re.search(r"(\d{1,2}:\d{2})", txt)
                if m_v and m_r and m_t:
                    venue = m_v.group(1)
                    rnum = int(m_r.group(1))
                    ptime = m_t.group(1)
                    if not any(s["venue"] == venue and s["race_num"] == rnum for s in schedule):
                        schedule.append({
                            "org": "NAR",
                            "venue": venue,
                            "race_num": rnum,
                            "post_time": ptime,
                            "num_horses": 10,
                        })

            if not schedule:
                lines = [re.sub(r"\s+", " ", ln).strip() for ln in page.inner_text("body").splitlines()]
                joined = " / ".join([ln for ln in lines if ln])
                for v in NAR_VENUES:
                    m = re.search(
                        rf"{v}\s*(?:/\s*)?(?:発売中\s*)?(\d{{1,2}})\s*(?:レース|R)\s*(\d{{1,2}}:\d{{2}})",
                        joined,
                    )
                    if m:
                        rnum = int(m.group(1))
                        ptime = m.group(2)
                        if not any(s["venue"] == v and s["race_num"] == rnum for s in schedule):
                            schedule.append({
                                "org": "NAR",
                                "venue": v,
                                "race_num": rnum,
                                "post_time": ptime,
                                "num_horses": 10,
                            })
        except Exception as e:
            self.logger.warning(f"⚠️ NAR当日スケジュール取得時に例外が発生しました: {e}")

        if schedule:
            self.logger.info(f"📅 取得した直近スケジュール: {schedule}")
            return schedule

        return FALLBACK_SCHEDULE

    def _extract_win_odds_from_page(self, page) -> List[Dict]:
        """
        単勝オッズ表（枠番・馬番・馬名・単勝・複勝）から出走馬の単勝オッズを抽出する
        """
        horses_win = []
        rows = page.locator("table tr")
        for i in range(rows.count()):
            cells = [re.sub(r"\s+", " ", c).strip() for c in rows.nth(i).locator("th, td").all_inner_texts()]
            if len(cells) < 4:
                continue

            # 馬番（1〜18）と単勝オッズ（例: 1.8 / 12.4 / 124.0）をセル単位で探索
            nums_in_row = []
            for idx, c in enumerate(cells[:4]):
                m_int = re.fullmatch(r"\d{1,2}", c)
                if m_int:
                    nums_in_row.append((idx, int(c)))

            if len(nums_in_row) < 2:
                continue

            # 枠番と馬番の判定（2つ数字があれば [枠, 馬番]、1つなら [馬番]）
            if len(nums_in_row) >= 2 and nums_in_row[0][1] <= 8 and nums_in_row[1][1] <= 18:
                frame_no = nums_in_row[0][1]
                umaban = nums_in_row[1][1]
            else:
                frame_no = None
                umaban = nums_in_row[0][1]

            if not (1 <= umaban <= 18):
                continue

            win_odds = None
            for cell in cells[2:]:
                # 複勝範囲（1.0-1.3など）はスキップ
                if re.search(r"\d+\.\d+\s*[-－～~]\s*\d+\.\d+", cell):
                    continue
                m_o = re.search(r"\b(\d+\.\d+)\b", cell)
                if m_o:
                    val = float(m_o.group(1))
                    if 1.0 <= val <= 999.9:
                        win_odds = val
                        break

            if win_odds is None:
                continue

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
        """
        枠複・枠単・馬単のオッズ表（人気 | 馬/組番 | オッズ）から組番とオッズを抽出する
        """
        combos = []
        rows = page.locator("table tr")
        for i in range(rows.count()):
            cells = [re.sub(r"\s+", " ", c).strip() for c in rows.nth(i).locator("th, td").all_inner_texts()]
            if len(cells) < 3:
                continue
            m_pop = re.fullmatch(r"\d{1,3}", cells[0])
            if not m_pop:
                continue
            pop = int(m_pop.group(0))
            m_pair = re.search(r"\b(\d{1,2})\s*[-－→]\s*(\d{1,2})\b", cells[1])
            m_odds = re.search(r"\b(\d+(?:\.\d+)?)\b", cells[2])
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

    def _click_first_visible(self, page, selectors: List[str], timeout_ms: int = 4000) -> bool:
        for sel in selectors:
            loc = page.locator(f"{sel}:visible")
            cnt = loc.count()
            for idx in range(cnt):
                item = loc.nth(idx)
                cls = (item.get_attribute("class") or "")
                if "disableItem" in cls or "disabled" in cls:
                    continue
                try:
                    item.click(timeout=timeout_ms)
                    return True
                except Exception:
                    continue
        return False

    def fetch_nar_race_snapshot(self, page, venue: str, race_num: int) -> Dict:
        # 1. 単勝オッズ一覧へ直接遷移（race_card/list への誤遷移を回避）
        page.goto("https://keiba.rakuten.co.jp/odds/tanfuku/R/", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1200)

        # 2. 表示中の競馬場（大井 / 笠松 / 園田 等）を選択
        if self._click_first_visible(
            page,
            [
                f'button:has-text("{venue}")',
                f'a:has-text("{venue}")',
                f'li:has-text("{venue}")',
            ],
            timeout_ms=4000,
        ):
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1000)

        # 3. 対象レース番号（1R〜12R）を選択
        if self._click_first_visible(
            page,
            [
                f'a:has-text("{race_num}R")',
                f'a:has-text("{race_num}レース")',
                f'button:has-text("{race_num}R")',
                f'li:has-text("{race_num}R")',
            ],
            timeout_ms=4000,
        ):
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1000)

        # オッズタブが開いていない場合は「オッズ」タブをクリック
        if "/odds/" not in page.url:
            if self._click_first_visible(page, ['a:has-text("オッズ")'], timeout_ms=4000):
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

        # 4. 枠複オッズの取得（枠複タブがあり、12頭立て以上かつ1番人気単勝2.0倍以上の場合）
        wakuren_combos = []
        wf_tab = page.locator('a:has-text("枠複"):visible').first
        has_wakuren_tab = wf_tab.count() > 0

        if is_odds_released and has_wakuren_tab and num_horses >= 12 and fav1_win_odds >= 2.0:
            if self._click_first_visible(page, ['a:has-text("枠複")', 'button:has-text("枠複")'], timeout_ms=4000):
                page.wait_for_timeout(1000)
                self._click_first_visible(
                    page,
                    ['a:has-text("人気順")', 'a:has-text("人気高配当順")', 'button:has-text("人気順")'],
                    timeout_ms=3000,
                )
                page.wait_for_timeout(800)
                wakuren_combos = self._extract_pair_odds_from_page(page, ordered=False, max_num=8)

        # 5. 枠複未発売・11頭立て以下・単勝1倍台の場合は 枠単 -> 馬単 の順で取得
        wakutan_combos, umatan_combos = [], []
        need_axis_mode = (not has_wakuren_tab) or (num_horses <= 11) or (fav1_win_odds < 2.0)

        if is_odds_released and need_axis_mode:
            wt_tab = page.locator('a:has-text("枠単"):visible').first
            if wt_tab.count() > 0:
                if self._click_first_visible(page, ['a:has-text("枠単")', 'button:has-text("枠単")'], timeout_ms=4000):
                    page.wait_for_timeout(1000)
                    self._click_first_visible(
                        page,
                        ['a:has-text("人気順")', 'a:has-text("人気高配当順")', 'button:has-text("人気順")'],
                        timeout_ms=3000,
                    )
                    page.wait_for_timeout(800)
                    wakutan_combos = self._extract_pair_odds_from_page(page, ordered=True, max_num=8)

            if len(wakutan_combos) < 3:
                if self._click_first_visible(page, ['a:has-text("馬単")', 'button:has-text("馬単")'], timeout_ms=4000):
                    page.wait_for_timeout(1000)
                    self._click_first_visible(
                        page,
                        ['a:has-text("人気順")', 'a:has-text("人気高配当順")', 'button:has-text("人気順")'],
                        timeout_ms=3000,
                    )
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

        self._click_first_visible(page, [f'a:has-text("{venue}")'], timeout_ms=4000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1000)

        self._click_first_visible(page, [f'a:has-text("{race_num}R")'], timeout_ms=4000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1000)

        body_txt = page.inner_text("body")
        is_obstacle = bool(re.search(r
