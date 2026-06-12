# -*- coding: utf-8 -*-
"""モーダル/フォームを開いてからスクショ"""
import os, glob
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000"
OUT = os.path.join("static", "manual")

# (ファイル名, パス, 開くJS, 待機ms, full_page)
SHOTS = [
    ("modal-cm-input",       "/customer-management",      "openAppModal()",      1200, False),
    ("modal-converter-new",  "/converter",                "openNew()",           1200, False),
    ("modal-mail-template",  "/settings/mail-templates",  "openTplEditor(null)", 1200, False),
]

def main():
    cands = (glob.glob(os.path.expanduser("~/AppData/Local/ms-playwright/chromium-1217/**/chrome.exe"), recursive=True)
             or glob.glob("C:/Users/seiya/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Local/ms-playwright/chromium-1217/**/chrome.exe", recursive=True))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=cands[0])
        ctx = browser.new_context(viewport={"width":1440,"height":900}, device_scale_factor=2, locale="ja-JP")
        page = ctx.new_page()
        page.goto(BASE + "/app-login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "株式会社デモ")
        page.fill('input[name="password"]', "10180831")
        page.click('button[type="submit"]')
        page.wait_for_timeout(2200)
        for name, path, js, wait, full in SHOTS:
            try:
                page.goto(BASE + path, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                page.evaluate(js)
                page.wait_for_timeout(wait)
                page.screenshot(path=os.path.join(OUT, name + ".png"), full_page=full)
                print("OK", name)
            except Exception as e:
                print("ERR", name, repr(e)[:140])
        browser.close()

if __name__ == "__main__":
    main()
