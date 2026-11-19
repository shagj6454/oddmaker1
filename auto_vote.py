import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict
from env_loader import load_env_file

load_env_file()

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class IPATVoter:
    """
    JRA 即PAT (https://www.ipat.jra.go.jp/sp/) 自動ログイン＆投票クラス (v11.9)
    - 通常戦: 枠連
    - 1倍台 / 11頭立て以下: 馬単 (JRAは枠単なしのため自動フォールバック)
    """

    def __init__(self, logger, headless: bool = True, dry_run: bool = False):
        self.logger = logger
        self.headless = headless
        self.dry_run = dry_run
        self.inet_id = os.getenv("JRA_INET_ID", "").strip()
        self.subscriber_no = os.getenv("JRA_SUBSCRIBER_NO", "").strip()
        self.pars_no = os.getenv("JRA_PARS_NO", "").strip()
        self.pin = os.getenv("JRA_PIN", "").strip()

    def _save_debug_screenshot(self, page, prefix: str):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = LOG_DIR / f"error_{prefix}_{ts}.png"
            page.screenshot(path=str(path), full_page=True)
            self.logger.error(f"📸 エラー時の画面キャプチャを保存しました: {path}")
        except Exception:
            pass

    def vote_wakuren(self, race_data: Dict, ticket: Dict) -> bool:
        btype = "馬単" if ticket.get("is_order_bet") else "枠連"
        sep = "->" if ticket.get("is_order_bet") else "-"
        venue = race_data["venue"]
        race_num = int(race_data["race_num"])

        self.logger.info(
            f"[JRA IPAT認証読込確認] INET_ID={'設定済' if self.inet_id else '未設定'}, "
            f"加入者番号={'設定済' if self.subscriber_no else '未設定'}, "
            f"P-ARS={'設定済' if self.pars_no else '未設定'}, "
            f"PIN={'設定済' if self.pin else '未設定'}"
        )
        self.logger.info(
            f"[JRA IPAT {btype}投票準備] {venue}{race_num}R ({ticket['stage_name']}) "
            f"合計:{ticket['total_bet']}円 / 合成オッズ:{ticket['synthetic_odds']}倍 / 最低払戻:{ticket['min_payout']}円"
        )
        for item in ticket["items"]:
            self.logger.info(
                f"  -> 買い目: {btype} {item['w1']}{sep}{item['w2']} ({item['odds']}倍 / {item['pop']}人気) : {item['bet']}円 (見込払戻 {item['est_payout']}円)"
            )

        if self.dry_run:
            self.logger.info("[DRY-RUN (テスト確認モード)] 実際のIPAT通信をスキップし、投票成功として記録します。")
            return True

        if not all([self.inet_id, self.subscriber_no, self.pars_no, self.pin]):
            self.logger.error("❌ JRA即PATの認証情報（Secrets）が不足しているため投票を中止します。")
            return False

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            )
            page = context.new_page()
            try:
                # 1. トップページアクセス＆受付時間チェック
                page.goto("https://www.ipat.jra.go.jp/sp/", timeout=30000)
                page.wait_for_load_state("networkidle", timeout=15000)
                body_text = page.inner_text("body")
                if "受付時間外" in body_text or "発売しておりません" in body_text:
                    self.logger.warning("JRA IPATは現在『受付時間外』です。投票を中断します。")
                    browser.close()
                    return False

                # 2. INET-ID 入力＆送信
                inet_input = page.locator('input[name="inetid"], input[id*="inet"]').first
                inet_input.fill(self.inet_id)
                page.locator('button:has-text("ログイン"), input[type="submit"], a:has-text("ログイン")').first.click()
                page.wait_for_load_state("networkidle", timeout=15000)

                # 3. 加入者番号・暗証番号・P-ARS番号 入力＆ログイン
                sub_input = page.locator('input[name="i"], input[placeholder*="加入者"]').first
                pin_input = page.locator('input[name="p"], input[type="password"]').first
                pars_input = page.locator('input[name="r"], input[placeholder*="P-ARS"]').first

                sub_input.fill(self.subscriber_no)
                pin_input.fill(self.pin)
                pars_input.fill(self.pars_no)
                page.locator('button:has-text("ネット投票メニューへ"), a:has-text("ネット投票メニューへ"), input[type="submit"]').first.click()
                page.wait_for_load_state("networkidle", timeout=15000)

                # お知らせ画面が出た場合はスキップしてメニューへ
                if page.locator('button:has-text("OK"), a:has-text("メニューへ")').count() > 0:
                    page.locator('button:has-text("OK"), a:has-text("メニューへ")').first.click()
                    page.wait_for_load_state("networkidle", timeout=10000)

                # 4. 「通常投票」を選択
                page.locator('a:has-text("通常投票"), button:has-text("通常投票")').first.click()
                page.wait_for_load_state("networkidle", timeout=15000)

                # 5. 開催場（東京・京都など）とレース番号（1R〜12R）を選択
                page.locator(f'button:has-text("{venue}"), a:has-text("{venue}"), label:has-text("{venue}")').first.click()
                page.wait_for_timeout(800)
                page.locator(f'button:has-text("{race_num}R"), a:has-text("{race_num}R"), td:has-text("{race_num}R")').first.click()
                page.wait_for_load_state("networkidle", timeout=15000)

                # 6. 式別選択（枠連 または 馬単）
                page.locator(f'button:has-text("{btype}"), a:has-text("{btype}"), option:has-text("{btype}")').first.click()
                page.wait_for_timeout(800)

                # 7. 各買い目の組番選択・金額入力（100円単位）・セット追加
                for item in ticket["items"]:
                    n1 = str(item["w1"])
                    n2 = str(item["w2"])
                    units = str(item["bet"] // 100)

                    # 1着（または1頭目）選択
                    page.locator(f'[data-umaban="{n1}"], label:has-text("{n1}")').first.click()
                    page.wait_for_timeout(300)
                    # 2着（または2頭目）選択
                    page.locator(f'[data-umaban="{n2}"], label:has-text("{n2}")').nth(1 if btype == "馬単" else 0).click()
                    page.wait_for_timeout(300)

                    # 金額（100円単位の口数）入力
                    amt_input = page.locator('input[type="tel"], input[type="number"], input[name*="amount"]').first
                    amt_input.fill(units)
                    page.locator('button:has-text("セット"), button:has-text("追加")').first.click()
                    page.wait_for_timeout(400)

                # 8. 入力終了 -> 合計金額確認 -> 投票確定
                page.locator('button:has-text("入力終了"), a:has-text("入力終了"), button:has-text("投票内容確認")').first.click()
                page.wait_for_load_state("networkidle", timeout=15000)

                total_input = page.locator('input[type="tel"], input[type="number"]').first
                if total_input.count() > 0:
                    total_input.fill(str(ticket["total_bet"]))

                page.locator('button:has-text("投票"), input[value*="投票"]').last.click()
                page.wait_for_load_state("networkidle", timeout=15000)

                final_body = page.inner_text("body")
                if "投票を受け付けました" in final_body or "受付番号" in final_body:
                    self.logger.info(f"🎉 [JRA IPAT投票完了] {venue}{race_num}R ({btype}) 合計{ticket['total_bet']}円の購入が確定しました！")
                    browser.close()
                    return True
                else:
                    self.logger.warning("⚠️ JRA IPAT投票完了画面の受付番号が確認できませんでした。")
                    self._save_debug_screenshot(page, "jra_unconfirmed")
                    browser.close()
                    return False

            except Exception as e:
                self.logger.exception(f"❌ JRA IPAT自動投票中にエラーが発生しました: {e}")
                self._save_debug_screenshot(page, "jra_exception")
                browser.close()
                return False


class RakutenKeibaVoter:
    """
    地方競馬（楽天競馬 https://bet.keiba.rakuten.co.jp/sp/）自動ログイン＆投票クラス (v11.9)
    - 通常戦: 枠複
    - 1倍台 / 11頭立て以下: 枠単優先 (大井・川崎・船橋・浦和など) / 枠単なし時は馬単 (園田など)
    """

    def __init__(self, logger, headless: bool = True, dry_run: bool = False):
        self.logger = logger
        self.headless = headless
        self.dry_run = dry_run
        self.user_id = os.getenv("RAKUTEN_USER_ID", "").strip()
        self.password = os.getenv("RAKUTEN_PASSWORD", "").strip()
        self.vote_pin = os.getenv("RAKUTEN_VOTE_PIN", "").strip()

    def _save_debug_screenshot(self, page, prefix: str):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = LOG_DIR / f"error_{prefix}_{ts}.png"
            page.screenshot(path=str(path), full_page=True)
            self.logger.error(f"📸 エラー時の画面キャプチャを保存しました: {path}")
        except Exception:
            pass

    def vote_wakufuku(self, race_data: Dict, ticket: Dict) -> bool:
        btype = ticket.get("bet_type", "枠複")
        if btype == "枠連/枠複":
            btype = "枠複"
        sep = "->" if ticket.get("is_order_bet") else "-"
        venue = race_data["venue"]
        race_num = int(race_data["race_num"])

        self.logger.info(
            f"[楽天競馬 {btype}投票準備] {venue}{race_num}R ({ticket['stage_name']}) "
            f"合計:{ticket['total_bet']}円 / 合成オッズ:{ticket['synthetic_odds']}倍 / 最低払戻:{ticket['min_payout']}円"
        )
        for item in ticket["items"]:
            self.logger.info(
                f"  -> 買い目: {btype} {item['w1']}{sep}{item['w2']} ({item['odds']}倍 / {item['pop']}人気) : {item['bet']}円 (見込払戻 {item['est_payout']}円)"
            )

        if self.dry_run:
            self.logger.info("[DRY-RUN (テスト確認モード)] 実際の楽天競馬通信をスキップし、投票成功として記録します。")
            return True

        if not all([self.user_id, self.password, self.vote_pin]):
            self.logger.error("❌ 楽天競馬の認証情報（Secrets）が不足しているため投票を中止します。")
            return False

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            )
            page = context.new_page()
            try:
                # 1. 楽天競馬スマホ投票メニューへアクセス
                page.goto("https://bet.keiba.rakuten.co.jp/sp/menu", timeout=30000)
                page.wait_for_load_state("networkidle", timeout=15000)
                content = page.inner_text("body")

                if "VOTI0003" in content or "COMW0014" in content or "投票受付は終了" in content or "サービス時間外" in content:
                    self.logger.warning("楽天競馬は現在『投票受付時間外 (VOTI0003/COMW0014)』です。投票を中断します。")
                    browser.close()
                    return False

                # 2. 楽天IDログイン画面へ遷移した場合のログイン処理
                if "ログイン" in content or page.locator('input[name="u"], input[id*="user"]').count() > 0:
                    if page.locator('a:has-text("ログイン"), button:has-text("ログイン")').count() > 0 and page.locator('input[type="password"]').count() == 0:
                        page.locator('a:has-text("ログイン"), button:has-text("ログイン")').first.click()
                        page.wait_for_load_state("networkidle", timeout=15000)

                    user_input = page.locator('input[name="u"], input[id*="user"], input[type="email"], input[type="text"]').first
                    user_input.fill(self.user_id)
                    # 2段階ログイン（次へボタン）対応
                    if page.locator('input[type="password"]').count() == 0:
                        page.locator('button:has-text("次へ"), #cta001, button[type="submit"]').first.click()
                        page.wait_for_timeout(1500)

                    pass_input = page.locator('input[name="p"], input[type="password"]').first
                    pass_input.fill(self.password)
                    page.locator('button:has-text("ログイン"), #cta011, input[type="submit"], button[type="submit"]').first.click()
                    page.wait_for_load_state("networkidle", timeout=20000)

                # 3. 対象競馬場（大井・園田など）を選択
                page.locator(f'a:has-text("{venue}"), button:has-text("{venue}")').first.click()
                page.wait_for_load_state("networkidle", timeout=15000)

                # 4. 対象レース番号（1R〜12R）を選択
                page.locator(f'a:has-text("{race_num}R"), button:has-text("{race_num}R")').first.click()
                page.wait_for_load_state("networkidle", timeout=15000)

                # 5. 式別タブ（枠複 / 枠単 / 馬単）を選択
                page.locator(f'a:has-text("{btype}"), button:has-text("{btype}"), li:has-text("{btype}")').first.click()
                page.wait_for_timeout(1000)

                # 6. 買い目ごとの組番チェック＆金額（100円単位）入力
                for item in ticket["items"]:
                    n1 = str(item["w1"])
                    n2 = str(item["w2"])
                    units = str(item["bet"] // 100)

                    # 1着（1頭目）チェック
                    page.locator(f'input[name="first"][value="{n1}"], td[data-first="{n1}"], label:has-text("{n1}")').first.click()
                    page.wait_for_timeout(300)
                    # 2着（2頭目）チェック
                    page.locator(f'input[name="second"][value="{n2}"], td[data-second="{n2}"], label:has-text("{n2}")').last.click()
                    page.wait_for_timeout(300)

                    # 金額（100円単位）入力してカート（投票予定リスト）へ追加
                    amt_box = page.locator('input[name*="amount"], input[type="tel"], input[type="number"]').first
                    amt_box.fill(units)
                    page.locator('button:has-text("追加"), button:has-text("カート"), a:has-text("追加")').first.click()
                    page.wait_for_timeout(500)

                # 7. 投票確認画面へ遷移 -> 合計金額・暗証番号入力 -> 投票確定
                page.locator('a:has-text("投票内容確認"), button:has-text("投票内容確認"), a:has-text("投票へ")').first.click()
                page.wait_for_load_state("networkidle", timeout=15000)

                total_box = page.locator('input[name*="total"], input[placeholder*="合計"]').first
                if total_box.count() > 0:
                    total_box.fill(str(ticket["total_bet"]))

                pin_box = page.locator('input[type="password"], input[name*="pin"], input[placeholder*="暗証番号"]').first
                pin_box.fill(self.vote_pin)

                page.locator('button:has-text("投票する"), input[value*="投票する"], button:has-text("投票確定")').first.click()
                page.wait_for_load_state("networkidle", timeout=15000)

                done_text = page.inner_text("body")
                if "投票を受け付けました" in done_text or "受付番号" in done_text or "投票完了" in done_text:
                    self.logger.info(f"🎉 [楽天競馬 投票完了] {venue}{race_num}R ({btype}) 合計{ticket['total_bet']}円の購入が確定しました！")
                    browser.close()
                    return True
                else:
                    self.logger.warning("⚠️ 楽天競馬の投票完了メッセージが確認できませんでした。")
                    self._save_debug_screenshot(page, "rakuten_unconfirmed")
                    browser.close()
                    return False

            except Exception as e:
                self.logger.exception(f"❌ 楽天競馬 自動投票中にエラーが発生しました: {e}")
                self._save_debug_screenshot(page, "rakuten_exception")
                browser.close()
                return False
