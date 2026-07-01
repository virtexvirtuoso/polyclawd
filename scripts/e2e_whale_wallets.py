"""On-demand E2E suite for the live Predator Atlas page (hits prod — not part of pytest).
Run: python3 scripts/e2e_whale_wallets.py"""
from playwright.sync_api import sync_playwright
import re, sys

URL = "https://virtuosocrypto.com/polyclawd/whale-wallets.html"
results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    # ── 1. Desktop, reduced motion (deterministic interactions) ──
    ctx = browser.new_context(viewport={"width":1440,"height":950}, reduced_motion="reduce")
    page = ctx.new_page()
    console_errors, page_errors = [], []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.goto(URL); page.wait_for_load_state("networkidle"); page.wait_for_timeout(5000)

    check("loads with zero console errors", not console_errors, str(console_errors[:2]))
    check("zero uncaught page errors", not page_errors, str(page_errors[:2]))
    check("exactly one h1", page.locator("h1").count() == 1)
    check("has main landmark", page.locator("main").count() == 1)

    vitals = page.inner_text("header")
    m = re.search(r"smart\s+(\d+)", vitals)
    smart_n = int(m.group(1)) if m else 0
    check("vitals populated (smart>0, tracked>0)", smart_n > 0 and re.search(r"tracked\s+[1-9]", vitals), vitals[:80])

    # cross-links
    stream = page.locator('a[href*="whale.html"]').count()
    tape = page.locator('a[href*="whale-tape"]').count()
    check("cross-links to STREAM and TAPE", stream >= 1 and tape >= 1)

    # constellation nodes
    nodes = page.locator(".nodebtn")
    check("constellation nodes rendered", nodes.count() > 0, f"count={nodes.count()}")
    labels_ok = all(nodes.nth(i).get_attribute("aria-label") for i in range(min(5, nodes.count())))
    check("node buttons have aria-labels", labels_ok)

    # node click -> dossier matches the node's wallet (via svg pointer router)
    first = nodes.first
    name_in_aria = (first.get_attribute("aria-label") or "").split(",")[0]
    addr = first.get_attribute("data-addr")
    core = page.locator(f'g.w-node[data-addr="{addr}"] circle.core')
    bb = core.bounding_box()
    page.mouse.click(bb["x"] + bb["width"]/2, bb["y"] + bb["height"]/2)
    page.wait_for_timeout(2500)
    dossier = page.inner_text("body")
    check("node click opens matching dossier", name_in_aria and name_in_aria in dossier, f"expected {name_in_aria!r}")
    check("dossier shows record (closed positions)", re.search(r"\d+\s*closed", dossier, re.I))
    has_entries = "no attributed entries" in dossier.lower() or "attributed" in dossier.lower() \
        or re.search(r"(CRITICAL|HIGH|LOW)", dossier)
    check("dossier entries section present (or honest empty state)", has_entries)
    if not has_entries:
        seg = dossier[dossier.find("DOSSIER"):dossier.find("DOSSIER")+700] if "DOSSIER" in dossier else dossier[-700:]
        print("---- dossier content dump ----"); print(seg); print("---- end dump ----")

    page.keyboard.press("Escape"); page.wait_for_timeout(800)
    check("Escape clears dossier", "Select a predator" in page.inner_text("body"))

    # keyboard path: focus a node directly and Enter
    first.focus(); page.keyboard.press("Enter"); page.wait_for_timeout(1500)
    check("keyboard Enter opens dossier", name_in_aria in page.inner_text("body"))
    page.keyboard.press("Escape")

    # roster sorting
    def first_row():
        return page.locator(".rowbtn").first.inner_text()
    page.locator(".sortbtn", has_text="WIN RATE").click(); page.wait_for_timeout(600)
    wr_first = first_row()
    page.locator(".sortbtn", has_text="REALIZED PNL").click(); page.wait_for_timeout(600)
    pnl_first = first_row()
    check("sort buttons reorder roster", wr_first != pnl_first or smart_n <= 1, "same first row under both sorts")

    # dossier survives a polling cycle without churn (anti-churn requirement)
    bb = core.bounding_box()
    page.mouse.click(bb["x"] + bb["width"]/2, bb["y"] + bb["height"]/2)
    page.wait_for_timeout(1000)
    before = page.inner_text("body")
    page.wait_for_timeout(65000)   # cross a 60s poll boundary
    after = page.inner_text("body")
    check("locked dossier survives 60s poll", name_in_aria in after and not console_errors, "dossier lost or errors after poll")
    ctx.close()

    # ── 2. Mobile 390x844 ──
    ctx2 = browser.new_context(viewport={"width":390,"height":844}, reduced_motion="reduce",
                               user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")
    pg2 = ctx2.new_page()
    err2 = []
    pg2.on("console", lambda m: err2.append(m.text) if m.type == "error" else None)
    pg2.goto(URL); pg2.wait_for_load_state("networkidle"); pg2.wait_for_timeout(5000)
    sw = pg2.evaluate("document.documentElement.scrollWidth")
    check("mobile: no horizontal overflow", sw <= 390, f"scrollWidth={sw}")
    check("mobile: zero console errors", not err2, str(err2[:2]))
    rows = pg2.locator(".rowbtn")
    check("mobile: roster visible", rows.count() > 0)
    rows.first.click(); pg2.wait_for_timeout(2000)
    sheet_open = pg2.evaluate("!!document.querySelector('[class*=sheet],[class*=dossier]')") and "closed" in pg2.inner_text("body").lower()
    check("mobile: row opens dossier sheet", sheet_open)
    btns = pg2.locator("button:visible")
    small = 0
    for i in range(min(10, btns.count())):
        bb = btns.nth(i).bounding_box()
        if bb and (bb["height"] < 43 or bb["width"] < 43): small += 1
    check("mobile: sampled tap targets >=44px", small <= 2, f"{small}/10 below 44px")
    pg2.screenshot(path="/tmp/e2e_wallets_mobile.png")
    ctx2.close()

    # ── 3. Resilience: API down (client-side abort; prod untouched) ──
    ctx3 = browser.new_context(viewport={"width":1440,"height":950}, reduced_motion="reduce")
    pg3 = ctx3.new_page()
    pg3.route("**/polyclawd/api/whale/**", lambda r: r.abort())
    pg3.goto(URL); pg3.wait_for_timeout(6000)
    body3 = pg3.inner_text("body").upper()
    check("API-down shows uplink/error state", "UPLINK" in body3 or "STALE" in body3 or "UNAVAILABLE" in body3, body3[:120])
    ctx3.close()

    browser.close()

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n=== {passed}/{len(results)} PASSED ===")
sys.exit(0 if passed == len(results) else 1)
