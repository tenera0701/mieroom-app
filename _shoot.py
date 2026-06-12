# -*- coding: utf-8 -*-
"""デモアカウントでログインして各画面のスクショを撮るスクリプト"""
import os, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000"
OUT = os.path.join("static", "manual")
os.makedirs(OUT, exist_ok=True)

# (ファイル名, パス, 撮影前の待機ms, full_page)
PAGES = [
    ("executive",        "/executive",                 1800, True),
    ("headquarters",     "/headquarters",              1800, True),
    ("sales",            "/sales",                     1800, True),
    ("customer-management","/customer-management",     1800, True),
    ("echo-management",  "/echo-management",           1800, True),
    ("customer-service", "/customer-service",          1800, True),
    ("reservations",     "/reservations",              1800, True),
    ("leads",            "/leads",                     1800, True),
    ("converter",        "/converter",                 1800, True),
    ("contract-customers","/contract-customers",       1800, True),
    ("past-customers",   "/past-customers",            1800, True),
    ("daily-report",     "/daily-report",              1800, True),
    ("floorplan",        "/floorplan",                 2200, False),
    ("image-editor",     "/image-editor",              2200, False),
    ("leave-management", "/leave-management",          1800, True),
    ("accounting",       "/accounting",                1800, True),
    ("chat",             "/chat",                      1800, False),
    ("mail-templates",   "/settings/mail-templates",   1800, True),
    ("mail-import",      "/settings/mail-import",       1800, True),
    ("mail-automation",  "/settings/mail-automation",  1800, True),
    ("doc-templates",    "/doc-templates",             1800, True),
    ("store-visits",     "/store-visits",              1800, True),
    ("reservation-settings","/settings/reservation",   1800, True),
    ("settings-staff",   "/settings/staff",            1800, True),
    ("settings-accounts","/settings/accounts",         1800, True),
    ("settings-company", "/settings/company",          1800, True),
    ("settings-profile", "/settings/profile",          1800, True),
]

def login(page):
    page.goto(BASE + "/app-login", wait_until="domcontentloaded")
    page.fill('input[name="username"]', "株式会社デモ")
    page.fill('input[name="password"]', "10180831")
    page.click('button[type="submit"]')
    page.wait_for_timeout(2500)
    print("after login url:", page.url)

def shoot(page, name, path, wait, full):
    try:
        page.goto(BASE + path, wait_until="domcontentloaded")
        page.wait_for_timeout(wait)
        fp = os.path.join(OUT, name + ".png")
        page.screenshot(path=fp, full_page=full)
        print("OK", name, "->", fp, "url=", page.url)
    except Exception as e:
        print("ERR", name, repr(e))

def main():
    with sync_playwright() as p:
        import glob as _g
        cands = (_g.glob(os.path.expanduser("~/AppData/Local/ms-playwright/chromium-1217/**/chrome.exe"), recursive=True)
                 or _g.glob("C:/Users/seiya/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Local/ms-playwright/chromium-1217/**/chrome.exe", recursive=True))
        exe = cands[0]
        browser = p.chromium.launch(headless=True, executable_path=exe)
        ctx = browser.new_context(viewport={"width":1440,"height":900}, device_scale_factor=2, locale="ja-JP")
        page = ctx.new_page()
        login(page)
        for name, path, wait, full in PAGES:
            shoot(page, name, path, wait, full)
        browser.close()

if __name__ == "__main__":
    main()
