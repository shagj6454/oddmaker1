import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from playwright.sync_api import Page, sync_playwright


@dataclass
class RaceMeta:
    race_id: str          # netkeiba 12桁ID (例: 202603030508)
    venue: str            # 場名 (例: 福島)
    race_num: int         # レース番号 (例: 8)
    race_name: str        # レース名 (例: 3歳以上1勝クラス, 高湯温泉特別)
    post_time: str        # 発走時刻 HH:MM (例: 13:55)
    has_scratch: bool = False


@dataclass
class BetOrder:
    meta: RaceMeta
    bet_type: str                     # "枠連" or "馬連"
    tickets: List[Tuple[str, int]]    # [("2-4", 400), ("2-3", 400), ...]
    total_amount: int = field(init=False)

    def __post_init__(self):
        self.total_amount = sum(amt for _, amt in self.tickets)


def normalize_text(s: str) -> str:
    """レース名・場名の比較用正規化（空白・記号・括弧内グレードを除去）"""
    s = re.sub(r"[\s\u3000]+", "", s or "")
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    return s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def parse_hhmm_to_minutes(hhmm: str) -> Optional[int]:
    m = re.search(r"(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


# ==============================================================================
# 1. netkeiba からレース情報・オッズを取得（JRAへのスクレイピングは一切しない）
# ==============================================================================
def fetch_netkeiba_race_and_odds(
    page: Page,
    race_id: str,
    bet_type: str = "枠連",
) -> Tuple[RaceMeta, List[Dict]]:
    """
    netkeiba の出馬表・オッズページから以下を取得する:
      - RaceMeta (場名・R番号・レース名・発走時刻・取消有無)
      - 枠連(または馬連)の人気順オッズリスト [{"pop": 1, "pair": "2-6", "odds": 5.4}, ...]
    """
    shutuba_url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    page.goto(shutuba_url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(500)

    body = page.inner_text("body")

    # レース名
    race_name = ""
    if page.locator(".RaceName").count() > 0:
        race_name = page.locator(".RaceName").first.inner_text().strip()

    # 発走時刻 (例: "13:55発走")
    m_time = re.search(r"(\d{1,2}:\d{2})\s*発走", body)
    post_time = m_time.group(1) if m_time else ""

    # 場名・レース番号
    venue_map = {
        "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
        "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
    }
    venue = venue_map.get(race_id[4:6], "")
    race_num = int(race_id[10:12])
    has_scratch = ("取消" in body) or ("除外" in body)

    meta = RaceMeta(
        race_id=race_id,
        venue=venue,
        race_num=race_num,
        race_name=race_name,
        post_time=post_time,
        has_scratch=has_scratch,
    )

    # オッズページ (type=b3: 枠連, b4: 馬連 / rf=shutuba_submenu)
    odds_type_code = "b3" if bet_type == "枠連" else "b4"
    odds_url = f"https://race.netkeiba.com/odds/index.html?type={odds_type_code}&race_id={race_id}&housiki=c0"
    page.goto(odds_url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(800)

    # 人気順タブがあればクリックして確実に人気順テーブルを表示
    pop_tab = page.locator('a:has-text("人気順")').first
    if pop_tab.count() > 0:
        try:
            pop_tab.click(timeout=3000)
            page.wait_for_timeout(600)
        except Exception:
            pass

    combos: List[Dict] = []
    rows = page.locator("table tr")
    for i in range(rows.count()):
        cells = [re.sub(r"\s+", " ", c).strip() for c in rows.nth(i).locator("th, td").all_inner_texts()]
        if len(cells) < 3:
            continue
        m_pop = re.fullmatch(r"\d{1,3}", cells[0])
        m_pair = re.search(r"\b(\d{1,2})\s*[-－]\s*(\d{1,2})\b", cells[1])
        m_odds = re.search(r"\b(\d+(?:\.\d+)?)\b", cells[2])
        if m_pop and m_pair and m_odds:
            a, b = sorted((int(m_pair.group(1)), int(m_pair.group(2))))
            combos.append({
                "pop": int(m_pop.group(0)),
                "pair": f"{a}-{b}",
                "w1": a,
                "w2": b,
                "odds": float(m_odds.group(1)),
            })

    combos.sort(key=lambda x: x["pop"])
    return meta, combos


# ==============================================================================
# 2. フェイルセーフ照合ロジック
# ==============================================================================
def verify_ipat_race_match(
    meta: RaceMeta,
    ipat_venue_text: str,
    ipat_race_num_text: str,
    ipat_race_name_text: str,
    ipat_close_time_text: str,
) -> Tuple[bool, str]:
    """
    netkeiba のレース情報と JRA 即PAT 画面上の表示を4項目で厳格照合する。
    """
    # ① 場名チェック
    if meta.venue not in ipat_venue_text:
        return False, f"場名不一致: netkeiba={meta.venue} vs 即PAT={ipat_venue_text}"

    # ② レース番号チェック
    m_r = re.search(r"(\d{1,2})\s*R", ipat_race_num_text)
    if not m_r or int(m_r.group(1)) != meta.race_num:
        return False, f"R番号不一致: netkeiba={meta.race_num}R vs 即PAT={ipat_race_num_text}"

    # ③ レース名チェック（正規化して部分一致または完全一致を確認）
    nk_name = normalize_text(meta.race_name)
    ip_name = normalize_text(ipat_race_name_text)
    if nk_name and ip_name and (nk_name not in ip_name) and (ip_name not in nk_name):
        return False, f"レース名不一致: netkeiba={meta.race_name} vs 即PAT={ipat_race_name_text}"

    # ④ 時刻チェック（即PAT締切時刻は通常、発走時刻の1〜2分前 -> 0〜5分差を許容）
    nk_min = parse_hhmm_to_minutes(meta.post_time)
    ip_min = parse_hhmm_to_minutes(ipat_close_time_text)
    if nk_min is None or ip_min is None:
        return False, f"時刻取得失敗: netkeiba={meta.post_time} vs 即PAT={ipat_close_time_text}"

    diff = nk_min - ip_min
    if not (0 <= diff <= 5):
        return False, f"時刻ズレ検知: 発走={meta.post_time} / 即PAT締切={ipat_close_time_text} (差{diff}分)"

    return True, f"照合OK ({meta.venue}{meta.race_num}R {meta.race_name} / 発走{meta.post_time}・締切{ipat_close_time_text})"


# ==============================================================================
# 3. JRA 即PAT ログイン〜フェイルセーフ照合〜投票実行
# ==============================================================================
def login_ipat(page: Page) -> None:
    inet_id = os.environ["JRA_INET_ID"]
    sub_no = os.environ["JRA_SUBSCRIBER_NO"]
    pin = os.environ["JRA_PIN"]
    pars_no = os.environ["JRA_PARS_NO"]

    page.goto("https://www.ipat.jra.go.jp/", wait_until="domcontentloaded", timeout=25000)
    page.fill('input[name="inetid"]', inet_id)
    page.click('a:has-text("ログイン"), input[type="submit"]')
    page.wait_for_load_state("domcontentloaded")

    page.fill('input[name="i"]', sub_no)
    page.fill('input[name="p"]', pin)
    page.fill('input[name="r"]', pars_no)
    page.click('a:has-text("ネット投票メニューへ"), input[type="submit"]')
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)


def execute_ipat_bet(page: Page, order: BetOrder, dry_run: bool = True) -> bool:
    """
    即PAT「オッズ投票（人気順）」画面から、netkeibaで決定した組番を選択し、
    フェイルセーフ照合を通過した場合のみ購入する。
    """
    if order.meta.has_scratch:
        print("🛑 [ABORT] 取消/除外馬ありのため購入中止", flush=True)
        return False

    # 「オッズ投票」を開く
    page.locator('button:has-text("オッズ投票"), a:has-text("オッズ投票")').first.click()
    page.wait_for_timeout(800)

    # 競馬場を選択 (例: "福島（土）")
    page.locator(f'button:has-text("{order.meta.venue}")').first.click()
    page.wait_for_timeout(500)

    # レース選択ボタンから「レース名・締切時刻」を取得して事前フェイルセーフ
    race_btn = page.locator(f'button:has-text("{order.meta.race_num}R")').first
    btn_text = race_btn.inner_text()
    ok, msg = verify_ipat_race_match(
        meta=order.meta,
        ipat_venue_text=order.meta.venue,
        ipat_race_num_text=btn_text,
        ipat_race_name_text=btn_text,
        ipat_close_time_text=btn_text,
    )
    if not ok:
        print(f"🛑 [FAIL-SAFE ABORT / レース選択前] {msg}", flush=True)
        return False

    print(f"✅ [事前照合通過] {msg}", flush=True)
    race_btn.click()
    page.wait_for_timeout(800)

    # 式別を「枠連」（または「馬連」）に切替し、「オッズ投票（人気順）」タブを開く
    page.locator('select').nth(2).select_option(label=order.bet_type)
    page.wait_for_timeout(500)
    page.locator('button:has-text("オッズ投票（人気順）"), a:has-text("オッズ投票（人気順）")').first.click()
    page.wait_for_timeout(600)

    # 投票画面ヘッダーでの再照合（場名・R番号・締切時刻）
    header_txt = page.inner_text("body")[:800]
    ok2, msg2 = verify_ipat_race_match(
        meta=order.meta,
        ipat_venue_text=header_txt,
        ipat_race_num_text=header_txt,
        ipat_race_name_text=order.meta.race_name,
        ipat_close_time_text=header_txt,
    )
    if not ok2:
        print(f"🛑 [FAIL-SAFE ABORT / 投票画面] {msg2}", flush=True)
        return False

    # netkeibaで決定した各組番を行検索して選択＆セット
    for pair_str, amount in order.tickets:
        if amount < 100 or amount % 100 != 0:
            print(f"🛑 [FAIL-SAFE ABORT] 不正な金額指定: {pair_str} -> {amount}円", flush=True)
            return False

        # テーブルから対象組番（例: "2-4"）の行をクリック
        row_loc = page.locator(f'tr:has(td:text-is("{pair_str}"))').first
        if row_loc.count() == 0:
            print(f"🛑 [FAIL-SAFE ABORT] 即PAT画面に組番 {pair_str} が見つかりません", flush=True)
            return False
        row_loc.click()
        page.wait_for_timeout(200)

        # 金額（100円単位）を入力して「セット」
        amt_input = page.locator('input[placeholder*="金額"], input[type="tel"]').last
        amt_input.fill(str(amount // 100))
        page.locator('button:has-text("セット")').last.click()
        page.wait_for_timeout(300)

    # 「入力終了」または「購入予定リスト」を開いて最終照合
    page.locator('button:has-text("入力終了"), button:has-text("購入予定リスト")').first.click()
    page.wait_for_timeout(800)

    cart_txt = page.inner_text("body")
    expected_total_str = f"{order.total_amount:,}円"
    if expected_total_str not in cart_txt:
        print(f"🛑 [FAIL-SAFE ABORT / カート金額不一致] 期待={expected_total_str}", flush=True)
        return False

    for pair_str, _ in order.tickets:
        if pair_str not in cart_txt:
            print(f"🛑 [FAIL-SAFE ABORT / カート組番欠落] {pair_str} が購入予定リストにありません", flush=True)
            return False

    if dry_run:
        print(f"🧪 [DRY-RUN完了] 全フェイルセーフ通過（{order.meta.venue}{order.meta.race_num}R / {order.bet_type} / 合計{expected_total_str}）※実購入はスキップ", flush=True)
        return True

    # 合計金額を入力して「購入する」を実行
    page.locator('input[name*="total"], input[placeholder*="合計"]').last.fill(str(order.total_amount))
    page.locator('button:has-text("購入する")').last.click()
    page.wait_for_timeout(500)
    page.locator('button:has-text("OK")').first.click()
    print(f"🎯 [実購入完了] {order.meta.venue}{order.meta.race_num}R {order.bet_type} 合計{expected_total_str}", flush=True)
    return True
