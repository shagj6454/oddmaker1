import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from env_loader import load_env_file

load_env_file()

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class RakutenKeibaVoter:
    """
    楽天競馬 スマホ投票画面 (https://bet.keiba.rakuten.co.jp/sp/menu)
    動画の画面遷移（会場選択 -> オッズ投票 -> チェック選択 -> 金額入力へ -> 馬券カゴ -> 投票内容を確認する -> 総投票金額 -> 投票する）
    に完全対応した自動投票クラス (v11.9)
    """

    def __init__(self, logger, headless: bool = True, dry_run: bool = False):
        self.logger = logger
        self.headless = headless
        self.dry_run = dry_run
        self.user_id = os.getenv("RAKUTEN_USER_ID", "").strip()
        self.password = os.getenv("RAKUTEN_PASSWORD", "").strip()
        self.vote_pin = os.getenv("RAKUTEN_VOTE_PIN", "").strip()

    def _save_screenshot(self, page, label: str):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = LOG_DIR / f"{label}_{ts}.png"
            page.screenshot(path=str(path), full_page=True)
            self.logger.info(f"📸 画面キャプチャ保存: {path}")
        except Exception:
            pass

    def ensure_logged_in(self, page) -> bool:
        """ログイン状態を確認し、未ログインなら楽天ID・パスワードでログイン実行"""
        page.goto("https://keiba.rakuten.co.jp/", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1200)
        body_txt = page.inner_text("body")

        if "ログアウト" in body_txt or ("精算" in body_txt and "ログイン" not in body_txt[:400]):
            self.logger.info("✅ 楽天競馬へすでにログイン済みです。")
            return True

        if not self.user_id or not self.password:
            self.logger.error("❌ RAKUTEN_USER_ID または RAKUTEN_PASSWORD がSecretsに設定されていません。")
            return False

        self.logger.info("🔐 楽天競馬へログインを実行します...")
        login_btn = page.locator('a:has-text("ログイン"), button:has-text("ログイン")').first
        if login_btn.count() > 0:
            login_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)

        user_box = page.locator('input[name="u"], input[id*="user"], input[type="email"], input[type="text"]').first
        user_box.fill(self.user_id)

        if page.locator('input[type="password"]').count() == 0:
            next_btn = page.locator('button:has-text("次へ"), #cta001, button[type="submit"]').first
            if next_btn.count() > 0:
                next_btn.click()
                page.wait_for_timeout(1500)

        pass_box = page.locator('input[name="p"], input[type="password"]').first
        pass_box.fill(self.password)
        submit_btn = page.locator('button:has-text("ログイン"), #cta011, input[type="submit"], button[type="submit"]').first
        submit_btn.click()
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

        if "ログアウト" in page.inner_text("body"):
            self.logger.info("✅ 楽天競馬ログイン成功！")
            return True
        else:
            self.logger.error("❌ 楽天競馬ログインに失敗しました（パスキー設定やパスワードを確認してください）。")
            self._save_screenshot(page, "login_failed")
            return False

    def get_real_balance(self, page) -> Optional[int]:
        """投票メニュー (sp/menu) 右上の『購入可能額』を円単位で取得"""
        try:
            txt = page.inner_text("body")
            m = re.search(r"(?:購入可能額|投票可能額|購入限度額|現在残高|残高)\s*[:：]?\s*([\d,]+)\s*円", txt)
            if m:
                return int(m.group(1).replace(",", ""))

            page.goto("https://bet.keiba.rakuten.co.jp/sp/menu", timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)
            bet_txt = page.inner_text("body")
            m2 = re.search(r"(?:購入可能額|投票可能額|購入限度額|現在残高|残高)\s*[:：]?\s*([\d,]+)\s*円", bet_txt)
            if m2:
                return int(m2.group(1).replace(",", ""))
        except Exception as e:
            self.logger.warning(f"⚠️ 実残高の取得中に例外が発生しました: {e}")
        return None

    def vote_on_open_page(self, page, race_data: Dict, ticket: Dict) -> bool:
        """
        スマホ版投票メニュー (https://bet.keiba.rakuten.co.jp/sp/menu) から
        オッズ投票 -> 買い目チェック -> 金額入力へ -> 馬券カゴで各口数入力 ->
        投票内容を確認する -> 総投票金額入力 -> 投票する を実行する
        """
        btype = ticket.get("bet_type", "枠複")
        if btype == "枠連/枠複":
            btype = "枠複"
        sep = "->" if ticket.get("is_order_bet") else "-"
        venue = race_data["venue"]
        race_num = int(race_data["race_num"])

        self.logger.info(
            f"[楽天競馬 {btype}投票実行] {venue}{race_num}R ({ticket['stage_name']}) "
            f"合計:{ticket['total_bet']}円 / 合成オッズ:{ticket['synthetic_odds']}倍 / 最低払戻:{ticket['min_payout']}円"
        )
        for item in ticket["items"]:
            self.logger.info(
                f"  -> 買い目: {btype} {item['w1']}{sep}{item['w2']} ({item['odds']}倍 / {item['pop']}人気) : {item['bet']}円"
            )

        if self.dry_run:
            self.logger.info("[DRY-RUN (テスト確認モード)] 実際の投票送信はスキップし、成功として記録します。")
            return True

        try:
            # 1. スマホ版投票メニューへ遷移
            page.goto("https://bet.keiba.rakuten.co.jp/sp/menu", timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)
            body_txt = page.inner_text("body")

            if "VOTI0003" in body_txt or "COMW0014" in body_txt or "サービス時間外" in body_txt:
                self.logger.warning("⚠️ 楽天競馬の投票受付時間外のため投票を中断します。")
                self._save_screenshot(page, "vote_closed")
                return False

            # 2. 競馬場（大井 / 園田 等）を選択
            page.locator(f'button:has-text("{venue}"), a:has-text("{venue}"), li:has-text("{venue}")').first.click()
            page.wait_for_timeout(1200)

            # 3. 対象レース番号（1R〜12R）の行にある「オッズ投票」ボタンをクリック
            odds_btns = page.locator('a:has-text("オッズ投票"), button:has-text("オッズ投票")')
            if odds_btns.count() >= race_num:
                odds_btns.nth(race_num - 1).click()
            else:
                # ブロック内に race_numR を含む要素から「オッズ投票」を探すフォールバック
                race_block = page.locator(f'xpath=//*[contains(text(), "{race_num}R")]/ancestor::*[.//a[contains(text(),"オッズ投票")] or .//button[contains(text(),"オッズ投票")]][1]')
                race_block.locator('a:has-text("オッズ投票"), button:has-text("オッズ投票")').first.click()

            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

            # 4. 式別（枠複 / 枠単 / 馬単）を選択
            page.locator(f'button:has-text("{btype}"), a:has-text("{btype}"), li:has-text("{btype}"), td:has-text("{btype}")').first.click()
            page.wait_for_timeout(1200)

            # 5. オッズ一覧テーブルから該当する組番（w1, w2）のチェックボックスをすべてONにする
            checked_count = 0
            rows = page.locator("table tr")
            row_count = rows.count()
            for item in ticket["items"]:
                n1 = str(item["w1"])
                n2 = str(item["w2"])
                for r_idx in range(row_count):
                    row = rows.nth(r_idx)
                    r_txt = row.inner_text().strip()
                    # 行内の数字トークンから組番を照合（例: "3 6" や "3-6"）
                    nums = re.findall(r"\b\d{1,2}\b", r_txt)
                    # オッズ一覧の行は先頭が組番の2数字（例: 3, 6）になっている
                    if len(nums) >= 2 and nums[0] == n1 and nums[1] == n2:
                        cb = row.locator('input[type="checkbox"]').first
                        if cb.count() > 0:
                            if not cb.is_checked():
                                cb.check(force=True)
                            checked_count += 1
                            page.wait_for_timeout(200)
                            break

            if checked_count == 0:
                self.logger.error("❌ オッズ一覧画面で対象買い目のチェックボックスを選択できませんでした。")
                self._save_screenshot(page, "odds_check_failed")
                return False

            self.logger.info(f"☑️ オッズ一覧で {checked_count}/{len(ticket['items'])} 点の買い目を選択しました。")

            # 6. 「金額入力へ」をクリック -> 馬券カゴ (sp/kaime) へ遷移
            page.locator('button:has-text("金額入力へ"), a:has-text("金額入力へ")').first.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

            # 7. 馬券カゴ (sp/kaime) で各買い目の100円単位口数を入力
            kaime_rows = page.locator("table tr, .kaime-row, li")
            filled_count = 0
            for item in ticket["items"]:
                n1 = str(item["w1"])
                n2 = str(item["w2"])
                units = str(item["bet"] // 100)
                pair_pattern = rf"{n1}\s*[-－→]\s*{n2}"

                matched_row = False
                for k_idx in range(kaime_rows.count()):
                    k_row = kaime_rows.nth(k_idx)
                    if re.search(pair_pattern, k_row.inner_text()):
                        inp = k_row.locator('input[type="tel"], input[type="number"], input[type="text"]').first
                        if inp.count() > 0:
                            inp.fill(units)
                            filled_count += 1
                            matched_row = True
                            page.wait_for_timeout(200)
                            break
                if not matched_row:
                    self.logger.warning(f"⚠️ 馬券カゴで買い目 {n1}-{n2} の入力欄が見つかりませんでした。")

            if filled_count == 0:
                self.logger.error("❌ 馬券カゴで金額を1件も入力できませんでした。")
                self._save_screenshot(page, "kaime_fill_failed")
                return False

            # 8. 「投票内容を確認する」をクリック -> 確認画面 (sp/confirm) へ遷移
            page.locator('button:has-text("投票内容を確認する"), a:has-text("投票内容を確認する")').first.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

            # 9. 確認画面 (sp/confirm) で「総投票金額」を入力（暗証番号欄がある場合のみPIN入力）
            total_box = page.locator('input[type="tel"], input[type="number"], input[name*="total"], input[type="text"]').first
            if total_box.count() > 0:
                total_box.fill(str(ticket["total_bet"]))
                page.wait_for_timeout(300)

            pin_box = page.locator('input[type="password"], input[name*="pin"]')
            if pin_box.count() > 0 and self.vote_pin:
                pin_box.first.fill(self.vote_pin)
                page.wait_for_timeout(300)

            # 10. 「投票する」ボタンをクリックして購入確定！
            page.locator('button:has-text("投票する"), input[value*="投票する"]').first.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)

            done_text = page.inner_text("body")
            if "投票を受け付けました" in done_text or "受付番号" in done_text or "投票完了" in done_text:
                self.logger.info(
                    f"🎉 [楽天競馬 投票確定完了] {venue}{race_num}R ({btype}) 合計{ticket['total_bet']}円の購入が完了しました！"
                )
                self._save_screenshot(page, "vote_success")
                return True
            else:
                self.logger.warning("⚠️ 投票完了画面の受付番号が確認できませんでした。")
                self._save_screenshot(page, "vote_unconfirmed")
                return False

        except Exception as e:
            self.logger.exception(f"❌ 楽天競馬 投票操作中にエラーが発生しました: {e}")
            self._save_screenshot(page, "vote_exception")
            return False
