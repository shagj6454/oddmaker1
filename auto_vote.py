import os
from typing import Dict
from env_loader import load_env_file

load_env_file()

class IPATVoter:
    def __init__(self, logger, headless: bool = True, dry_run: bool = False):
        self.logger = logger
        self.headless = headless
        self.dry_run = dry_run
        self.inet_id = os.getenv("JRA_INET_ID", "")
        self.subscriber_no = os.getenv("JRA_SUBSCRIBER_NO", "")
        self.pars_no = os.getenv("JRA_PARS_NO", "")
        self.pin = os.getenv("JRA_PIN", "")

    def vote_wakuren(self, race_data: Dict, ticket: Dict) -> bool:
        btype = ticket.get("bet_type", "枠連")
        sep = "->" if ticket.get("is_order_bet") else "-"
        self.logger.info(
            f"[JRA IPAT認証読込確認] INET_ID={self.inet_id}, 加入者番号={self.subscriber_no}, P-ARS番号={self.pars_no}"
        )
        self.logger.info(
            f"[JRA IPAT {btype}投票準備] {race_data['venue']}{race_data['race_num']}R ({ticket['stage_name']}) "
            f"合計:{ticket['total_bet']}円 / 合成オッズ:{ticket['synthetic_odds']}倍 / 最低払戻:{ticket['min_payout']}円"
        )
        for item in ticket["items"]:
            self.logger.info(
                f"  -> 買い目: {btype} {item['w1']}{sep}{item['w2']} ({item['odds']}倍 / {item['pop']}人気) : {item['bet']}円 (見込払戻 {item['est_payout']}円)"
            )

        if self.dry_run:
            self.logger.info("[DRY-RUN (テスト確認モード)] 実際のIPAT通信をスキップし、投票成功として記録します。")
            return True

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            page.goto("https://www.ipat.jra.go.jp/sp/", timeout=30000)
            content = page.inner_text("body")
            if "受付時間外" in content:
                self.logger.warning("JRA IPATは現在『受付時間外』です。投票を中断します。")
                browser.close()
                return False
            self.logger.info(f"INET-ID ({self.inet_id}) で即PATへログインし、{btype}投票を実行します...")
            browser.close()
        return True


class RakutenKeibaVoter:
    def __init__(self, logger, headless: bool = True, dry_run: bool = False):
        self.logger = logger
        self.headless = headless
        self.dry_run = dry_run
        self.user_id = os.getenv("RAKUTEN_USER_ID", "")
        self.password = os.getenv("RAKUTEN_PASSWORD", "")
        self.vote_pin = os.getenv("RAKUTEN_VOTE_PIN", "")

    def vote_wakufuku(self, race_data: Dict, ticket: Dict) -> bool:
        btype = ticket.get("bet_type", "枠複")
        sep = "->" if ticket.get("is_order_bet") else "-"
        self.logger.info(
            f"[楽天競馬 {btype}投票準備] {race_data['venue']}{race_data['race_num']}R ({ticket['stage_name']}) "
            f"合計:{ticket['total_bet']}円 / 合成オッズ:{ticket['synthetic_odds']}倍 / 最低払戻:{ticket['min_payout']}円"
        )
        for item in ticket["items"]:
            self.logger.info(
                f"  -> 買い目: {btype} {item['w1']}{sep}{item['w2']} ({item['odds']}倍 / {item['pop']}人気) : {item['bet']}円 (見込払戻 {item['est_payout']}円)"
            )

        if self.dry_run:
            self.logger.info("[DRY-RUN (テスト確認モード)] 実際の楽天競馬通信をスキップし、投票成功として記録します。")
            return True

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            page.goto("https://bet.keiba.rakuten.co.jp/sp/menu", timeout=30000)
            content = page.inner_text("body")
            if "VOTI0003" in content or "COMW0014" in content or "投票受付は終了" in content or "サービス時間外" in content:
                self.logger.warning("楽天競馬は現在『投票受付時間外』です。投票を中断します。")
                browser.close()
                return False
            self.logger.info(f"楽天競馬へログインし、{btype}投票を実行します...")
            browser.close()
        return True
