import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from playwright.sync_api import Page


@dataclass
class RaceMeta:
    race_id: str
    venue: str
    race_num: int
    race_name: str
    post_time: str
    has_scratch: bool = False


@dataclass
class BetOrder:
    meta: RaceMeta
    bet_type: str                     # "枠連" / "馬連" / "馬単"
    tickets: List[Tuple[str, int]]
    total_amount: int = field(init=False)

    def __post_init__(self):
        self.total_amount = sum(amt for _, amt in self.tickets)


def normalize_text(s: str) -> str:
    s = re.sub(r"[\s\u3000]+", "", s or "")
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    return s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def parse_hhmm_to_minutes(hhmm: str) -> Optional[int]:
    m = re.search(r"(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _wait_for_odds_loaded(page: Page, timeout_ms: int = 6000) -> None:
    """netkeibaの非同期オッズ（---.- から実数値への切替）を待機"""
    try:
        page.wait_for_function(
            """() => {
                const txt = document.body ? document.body.innerText : '';
                return /\\b\\d+\\.\\d\\b/.test(txt);
            }""",
            timeout=timeout_ms,
        )
    except Exception:
        pass
    page.wait_for_timeout(500)


# ==============================================================================
# 1. netkeiba からレース情報・オッズを取得
# ==============================================================================
def fetch_netkeiba_race_and_odds(
    page: Page,
    race_id: str,
    bet_type: str = "枠連",
) -> Tuple[RaceMeta, List[Dict]]:
    shutuba_url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    page.goto(shutuba_url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(500)

    body = page.inner_text("body")
    race_name = ""
    if page.locator(".RaceName").count() > 0:
        race_name = page.locator(".RaceName").first.inner_text().strip()

    m_time = re.search(r"(\d{1,2}:\d{2})\s*発走", body)
    post_time = m_time.group(1) if m_time else ""

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

    # b3: 枠連 / b4: 馬連 / b6: 馬単（housiki=c99 で人気順を直接開く）
    code_map = {"枠連": "b3", "馬連": "b4", "馬単": "b6"}
    odds_type_code = code_map.get(bet_type, "b3")
    ordered = bet_type == "馬単"
    max_num = 8 if bet_type == "枠連" else 18

    odds_url = f"https://race.netkeiba.com/odds/index.html?type={odds_type_code}&race_id={race_id}&housiki=c99"
    page.goto(odds_url, wait_until="domcontentloaded", timeout=25000)
    _wait_for_odds_loaded(page)

    # 人気順タブが未選択ならクリック
    pop_tab = page.locator('a:has-text("人気順")').first
    if pop_tab.count() > 0:
        try:
            pop_tab.click(timeout=2000)
            _wait_for_odds_loaded(page, timeout_ms=3000)
        except Exception:
            pass

    # チェックボックス列など列数の違いを吸収して行単位で抽出
    combos: List[Dict] = []
    seen_pairs = set()
    rows = page.locator("table tr")
    for i in range(rows.count()):
        cells = [re.sub(r"\s+", " ", c).strip() for c in rows.nth(i).locator("th, td").all_inner_texts()]
        if len(cells) < 3:
            continue

        m_pop = re.fullmatch(r"\d{1,3}", cells[0])
        if not m_pop:
            continue
        pop = int(m_pop.group(0))

        pair_found = None
        odds_found = None
        for cell in cells[1:]:
            if pair_found is None:
                m_p = re.search(r"\b(\d{1,2})\s*[-－→]\s*(\d{1,2})\b", cell)
                if m_p:
                    a, b = int(m_p.group(1)), int(m_p.group(2))
                    if 1 <= a <= max_num and 1 <= b <= max_num:
                        if not ordered:
                            a, b = min(a, b), max(a, b)
                        pair_found = (a, b)
                        continue
            else:
                m_o = re.search(r"\b(\d+(?:\.\d+)?)\b", cell)
                if m_o:
                    val = float(m_o.group(1))
                    if 1.0 <= val < 9999.0:
                        odds_found = val
                        break

        if pair_found and odds_found:
            pair_str = f"{pair_found[0]}-{pair_found[1]}"
            if pair_str not in seen_pairs:
                seen_pairs.add(pair_str)
                combos.append({
                    "pop": pop,
                    "pair": pair_str,
                    "w1": pair_found[0],
                    "w2": pair_found[1],
                    "odds": odds_found,
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
    if meta.venue not in ipat_venue_text:
        return False, f"場名不一致: netkeiba={meta.venue} vs 即PAT={ipat_venue_text}"

    m_r = re.search(r"(\d{1,2})\s*R", ipat_race_num_text)
    if not m_r or int(m_r.group(1)) != meta.race_num:
        return False, f"R番号不一致: netkeiba={meta.race_num}R vs 即PAT={ipat_race_num_text}"

    nk_name = normalize_text(meta.race_name)
    ip_name = normalize_text(ipat_race_name_text)
    if nk_name and ip_name and (nk_name not in ip_name) and (ip_name not in nk_name):
        return False, f"レース名不一致: netkeiba={meta.race_name} vs 即PAT={ipat_race_name_text}"

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
    page.goto("https://www.ipat.jra.go.jp/", wait_until="domcontentloaded", timeout=25000)
    page.fill('input[name="inetid"]', os.environ["JRA_INET_ID"])
    page.click('a:has-text("ログイン"), input[type="submit"]')
    page.wait_for_load_state("domcontentloaded")

    page.fill('input[name="i"]', os.environ["JRA_SUBSCRIBER_NO"])
    page.fill('input[name="p"]', os.environ["JRA_PIN"])
    page.fill('input[name="r"]', os.environ["JRA_PARS_NO"])
    page.click('a:has-text("ネット投票メニューへ"), input[type="submit"]')
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)


def execute_ipat_bet(page: Page, order: BetOrder, dry_run: bool = True) -> bool:
    if order.meta.has_scratch:
        print("🛑 [ABORT] 取消/除外馬ありのため購入中止", flush=True)
        return False

    page.locator('button:has-text("オッズ投票"), a:has-text("オッズ投票")').first.click()
    page.wait_for_timeout(800)

    page.locator(f'button:has-text("{order.meta.venue}")').first.click()
    page.wait_for_timeout(500)

    race_btn = page.locator(f'button:has-text("{order.meta.race_num}R")').first
    btn_text = race_btn.inner_text()
    ok, msg = verify_ipat_race_match(order.meta, order.meta.venue, btn_text, btn_text, btn_text)
    if not ok:
        print(f"🛑 [FAIL-SAFE ABORT / レース選択前] {msg}", flush=True)
        return False

    print(f"✅ [事前照合通過] {msg}", flush=True)
    race_btn.click()
    page.wait_for_timeout(800)

    page.locator("select").nth(2).select_option(label=order.bet_type)
    page.wait_for_timeout(500)
    page.locator('button:has-text("オッズ投票（人気順）"), a:has-text("オッズ投票（人気順）")').first.click()
    page.wait_for_timeout(600)

    header_txt = page.inner_text("body")[:800]
    ok2, msg2 = verify_ipat_race_match(order.meta, header_txt, header_txt, order.meta.race_name, header_txt)
    if not ok2:
        print(f"🛑 [FAIL-SAFE ABORT / 投票画面] {msg2}", flush=True)
        return False

    for pair_str, amount in order.tickets:
        if amount < 100 or amount % 100 != 0:
            print(f"🛑 [FAIL-SAFE ABORT] 不正な金額指定: {pair_str} -> {amount}円", flush=True)
            return False
        row_loc = page.locator(f'tr:has(td:text-is("{pair_str}"))').first
        if row_loc.count() == 0:
            print(f"🛑 [FAIL-SAFE ABORT] 即PAT画面に組番 {pair_str} が見つかりません", flush=True)
            return False
        row_loc.click()
        page.wait_for_timeout(200)
        page.locator('input[placeholder*="金額"], input[type="tel"]').last.fill(str(amount // 100))
        page.locator('button:has-text("セット")').last.click()
        page.wait_for_timeout(300)

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
        print(f"🧪 [DRY-RUN完了] 全フェイルセーフ通過（{order.meta.venue}{order.meta.race_num}R / {order.bet_type} / 合計{expected_total_str}）", flush=True)
        return True

    page.locator('input[name*="total"], input[placeholder*="合計"]').last.fill(str(order.total_amount))
    page.locator('button:has-text("購入する")').last.click()
    page.wait_for_timeout(500)
    page.locator('button:has-text("OK")').first.click()
    print(f"🎯 [実購入完了] {order.meta.venue}{order.meta.race_num}R {order.bet_type} 合計{expected_total_str}", flush=True)
    return True
