#!/usr/bin/env python3
"""One-page weekly election intelligence PDF report."""

from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# Colors
DEM_BLUE = colors.HexColor("#2B6CB0")
REP_RED = colors.HexColor("#B83A2E")
TERRACOTTA = colors.HexColor("#C75B39")
GOLD = colors.HexColor("#D4A843")
OLIVE = colors.HexColor("#4A8B5C")
BG = colors.HexColor("#FAF8F5")
SURFACE = colors.HexColor("#FFFFFF")
BORDER = colors.HexColor("#E2DDD6")
TEXT = colors.HexColor("#2D2D2D")
TEXT2 = colors.HexColor("#7A7568")
TEXT3 = colors.HexColor("#A09889")

W, H = letter  # 612 x 792
LM = 36  # left margin
RM = W - 36  # right margin
MID = 310  # column divider


def _fmt_money(amount):
    if not amount or amount == 0:
        return "$0"
    a = abs(amount)
    if a >= 1e9:
        return f"${a / 1e9:.1f}B"
    if a >= 1e6:
        return f"${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"${a / 1e3:.0f}K"
    return f"${a:.0f}"


def _bar(c, x, y, w, h, d_pct, label_l="", label_r=""):
    """Draw a D/R split bar with optional labels below."""
    d_pct = max(0.02, min(0.98, d_pct))
    d_w = w * d_pct
    c.setFillColor(DEM_BLUE)
    c.roundRect(x, y, d_w, h, 2, fill=1, stroke=0)
    c.setFillColor(REP_RED)
    c.roundRect(x + d_w, y, w - d_w, h, 2, fill=1, stroke=0)
    if label_l or label_r:
        c.setFont("Helvetica", 6)
        if label_l:
            c.setFillColor(DEM_BLUE)
            c.drawString(x, y - 8, label_l)
        if label_r:
            c.setFillColor(REP_RED)
            c.drawRightString(x + w, y - 8, label_r)


def _section(c, x, y, text):
    """Draw a section header and return new y."""
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(x, y, text)
    return y - 3


def _card(c, x, y, w, h):
    """Draw a card background."""
    c.setFillColor(SURFACE)
    c.roundRect(x, y, w, h, 3, fill=1, stroke=0)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.3)
    c.roundRect(x, y, w, h, 3, fill=0, stroke=1)


def generate_election_pdf(report: dict, output_path: str = None) -> str:
    """Generate a one-page PDF election intelligence brief."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    week_label = now.strftime("Week of %B %d, %Y")

    if not output_path:
        out_dir = Path(__file__).parent.parent / "storage" / "election_reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(out_dir / f"{date_str}.pdf")

    summary = report.get("summary", {})
    insights = report.get("insights", {})
    midterm = insights.get("midterm", {})
    pc = summary.get("party_control", {})
    score = summary.get("composite_score", 50)
    sb = midterm.get("scoreboard", {})
    fl = midterm.get("flipping", {}).get("senate", {})

    c = canvas.Canvas(output_path, pagesize=letter)

    # ── Background ──
    c.setFillColor(BG)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    # ── Header ──
    y = H - 32
    c.setFillColor(TERRACOTTA)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(LM, y, "POLYCLAWD")
    c.setFillColor(TEXT2)
    c.setFont("Helvetica", 7.5)
    c.drawString(100, y, "Election Intelligence Brief")
    c.setFillColor(TEXT3)
    c.setFont("Helvetica", 6.5)
    c.drawRightString(RM, y, f"{week_label}  ·  {summary.get('total_markets', 0):,} markets")
    y -= 6
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.5)
    c.line(LM, y, RM, y)

    # ── Composite Score ──
    y -= 28
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 32)
    c.drawString(LM, y, f"{score:.1f}")

    lean = "Toss-Up"
    if score >= 70: lean = "Strong Republican"
    elif score >= 58: lean = "Lean Republican"
    elif score > 42: lean = "Toss-Up"
    elif score > 30: lean = "Lean Democrat"
    else: lean = "Strong Democrat"

    c.setFont("Helvetica-Bold", 10)
    c.drawString(96, y + 12, lean)
    c.setFillColor(TEXT2)
    c.setFont("Helvetica", 7)
    c.drawString(96, y + 1, f"Composite Score  ·  Polymarket + Kalshi + FEC")

    # Spectrum bar
    y -= 12
    _bar(c, LM, y, RM - LM, 5, 0.5)
    marker_x = LM + (RM - LM) * (score / 100)
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 7)
    c.drawCentredString(marker_x, y + 7, "▼")

    # ══════════════════════════════════════════════════════════════════
    # CHAMBER CONTROL — 4 mini cards across full width
    # ══════════════════════════════════════════════════════════════════
    y -= 20
    y = _section(c, LM, y, "CHAMBER CONTROL")
    card_h = 36
    gap = 6
    card_w = (RM - LM - gap * 3) / 4

    chamber_data = [
        ("PRESIDENCY", pc.get("presidency", {})),
        ("SENATE", pc.get("senate", {})),
        ("HOUSE", pc.get("house", {})),
        ("GOVERNORS", None),
    ]

    for i, (label, probs) in enumerate(chamber_data):
        cx = LM + i * (card_w + gap)
        cy = y - card_h
        _card(c, cx, cy, card_w, card_h)

        c.setFillColor(TEXT3)
        c.setFont("Helvetica-Bold", 5.5)
        c.drawCentredString(cx + card_w / 2, cy + card_h - 10, label)

        if probs:
            d_val = probs.get("democrat", 0)
            r_val = probs.get("republican", 0)
            _bar(c, cx + 5, cy + card_h - 22, card_w - 10, 5, d_val,
                 f"D {d_val * 100:.0f}%", f"{r_val * 100:.0f}% R")
        else:
            gov = sb.get("governor", {})
            d_t, r_t = gov.get("d_total", 0), gov.get("r_total", 0)
            tot = d_t + r_t or 1
            _bar(c, cx + 5, cy + card_h - 22, card_w - 10, 5, d_t / tot,
                 f"D {d_t}", f"{r_t} R")

    y = y - card_h - 6

    # ══════════════════════════════════════════════════════════════════
    # SENATE PROJECTION — full width card
    # ══════════════════════════════════════════════════════════════════
    y = _section(c, LM, y, "SENATE SEAT PROJECTION")
    proj = fl.get("projected", {})
    net_label = fl.get("net_shift_label", "—")
    proj_d = proj.get("D", "?")
    proj_r = proj.get("R", "?")

    card_h = 26
    cy = y - card_h
    _card(c, LM, cy, RM - LM, card_h)

    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(LM + 8, cy + 8, net_label)

    c.setFillColor(TEXT2)
    c.setFont("Helvetica", 7)
    maj = "D MAJORITY" if proj.get("D", 0) >= 51 else "R MAJORITY" if proj.get("R", 0) >= 51 else "Need 51"
    c.drawString(170, cy + 10, f"Projected:  D {proj_d} – R {proj_r}  ·  {maj}")

    # Seat bar
    d_s = proj.get("D", 47)
    r_s = proj.get("R", 53)
    _bar(c, LM + 8, cy + 3, RM - LM - 16, 4, d_s / (d_s + r_s or 100))

    y = cy - 4

    # ══════════════════════════════════════════════════════════════════
    # TWO-COLUMN LAYOUT: Left = Flips + Battleground, Right = Money + Correlation + Tipping
    # ══════════════════════════════════════════════════════════════════
    col_l = LM
    col_r = MID + 8
    col_l_w = MID - LM - 4
    col_r_w = RM - col_r
    ly = y  # left column y
    ry = y  # right column y

    # ── LEFT: Projected Flips ──
    flips = fl.get("projected_flips", [])
    if flips:
        ly = _section(c, col_l, ly, "PROJECTED FLIPS")
        for f in flips[:4]:
            ly -= 10
            from_c = DEM_BLUE if f["from"] == "D" else REP_RED
            to_c = DEM_BLUE if f["to"] == "D" else REP_RED
            c.setFillColor(TEXT)
            c.setFont("Helvetica-Bold", 7)
            c.drawString(col_l, ly, f["state"])
            c.setFillColor(from_c)
            c.setFont("Helvetica", 7)
            c.drawString(col_l + 22, ly, f["from"])
            c.setFillColor(TEXT3)
            c.drawString(col_l + 32, ly, "→")
            c.setFillColor(to_c)
            c.setFont("Helvetica-Bold", 7)
            c.drawString(col_l + 42, ly, f["to"])
            c.setFillColor(TEXT2)
            c.setFont("Helvetica", 6.5)
            c.drawString(col_l + 56, ly, f"{f['flip_prob'] * 100:.0f}% prob, {f['margin'] * 100:.1f}pp")
        ly -= 6

    # At-risk holds (inline with flips to save space)
    holds = fl.get("at_risk_holds", [])
    if holds:
        ly -= 2
        c.setFillColor(TEXT3)
        c.setFont("Helvetica-Bold", 5.5)
        c.drawString(col_l, ly, "AT RISK")
        ly -= 9
        for h in holds[:2]:
            inc_c = DEM_BLUE if h["incumbent"] == "D" else REP_RED
            c.setFillColor(TEXT)
            c.setFont("Helvetica-Bold", 6.5)
            c.drawString(col_l, ly, h["state"])
            c.setFillColor(inc_c)
            c.setFont("Helvetica", 6.5)
            c.drawString(col_l + 22, ly, f"{h['incumbent']} {h['hold_prob'] * 100:.0f}%")
            c.setFillColor(TEXT2)
            c.drawString(col_l + 62, ly, f"({h['margin'] * 100:.1f}pp)")
            ly -= 9
        ly -= 2

    # ── LEFT: Battleground Table ──
    bg = midterm.get("battleground", [])[:10]
    if bg:
        ly = _section(c, col_l, ly, f"BATTLEGROUND RACES ({midterm.get('battleground_total', 0)} competitive)")
        # Table header
        ly -= 9
        c.setFillColor(TEXT3)
        c.setFont("Helvetica-Bold", 5.5)
        hx = [col_l, col_l + 28, col_l + 72, col_l + 108, col_l + 148, col_l + 180, col_l + 212]
        for x, h in zip(hx, ["ST", "RACE", "LEADER", "MARGIN", "D%", "R%", "FLIP"]):
            c.drawString(x, ly, h)
        ly -= 2
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.3)
        c.line(col_l, ly, col_l + col_l_w, ly)

        for b in bg:
            ly -= 9
            lc = DEM_BLUE if b["leader"] == "D" else REP_RED
            mc = TERRACOTTA if b["margin_pp"] <= 5 else GOLD if b["margin_pp"] <= 10 else TEXT2

            c.setFillColor(TEXT)
            c.setFont("Helvetica-Bold", 6.5)
            c.drawString(hx[0], ly, b["state"])
            c.setFillColor(TEXT2)
            c.setFont("Helvetica", 6.5)
            c.drawString(hx[1], ly, b["chamber"][:6])
            c.setFillColor(lc)
            c.setFont("Helvetica-Bold", 6.5)
            c.drawString(hx[2], ly, b["leader"])
            c.setFillColor(mc)
            c.setFont("Helvetica-Bold", 6.5)
            c.drawString(hx[3], ly, f"{b['margin_pp']}pp")
            c.setFillColor(DEM_BLUE)
            c.setFont("Helvetica", 6.5)
            c.drawString(hx[4], ly, f"{b['d_price'] * 100:.0f}%")
            c.setFillColor(REP_RED)
            c.drawString(hx[5], ly, f"{b['r_price'] * 100:.0f}%")
            if b.get("flip"):
                c.setFillColor(TERRACOTTA)
                c.setFont("Helvetica-Bold", 5.5)
                c.drawString(hx[6], ly, "FLIP")

    # ── RIGHT: IE Money Flow ──
    mf = midterm.get("money_flow", {})
    ry = _section(c, col_r, ry, "IE MONEY FLOW (30D)")

    for ch_name in ["senate", "house", "presidential"]:
        f = mf.get(ch_name, {})
        pro_d = f.get("pro_d", 0) or 0
        pro_r = f.get("pro_r", 0) or 0
        total = pro_d + pro_r
        if total == 0:
            continue

        ry -= 12
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(col_r, ry, ch_name.upper())
        c.setFillColor(TEXT3)
        c.setFont("Helvetica", 5.5)
        c.drawRightString(RM, ry, _fmt_money(total))

        ry -= 8
        _bar(c, col_r, ry, col_r_w, 4, pro_d / total if total else 0.5,
             f"D: {_fmt_money(pro_d)}", f"{_fmt_money(pro_r)} :R")
        ry -= 10

    # Total
    tot = mf.get("total", {})
    pro_d_tot = tot.get("pro_d", 0) or 0
    pro_r_tot = tot.get("pro_r", 0) or 0
    total_all = pro_d_tot + pro_r_tot
    if total_all > 0:
        ry -= 4
        adv = "D" if pro_d_tot > pro_r_tot else "R"
        _card(c, col_r, ry - 2, col_r_w, 12)
        c.setFillColor(DEM_BLUE if adv == "D" else REP_RED)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(col_r + 4, ry, f"Total: {_fmt_money(total_all)} — {adv} advantage")
        ry -= 16

    # ── RIGHT: Cross-Chamber Correlation ──
    ry -= 4
    cor = midterm.get("correlation", {})
    ry = _section(c, col_r, ry, "CROSS-CHAMBER CORRELATION")
    ry -= 10
    c.setFillColor(TEXT2)
    c.setFont("Helvetica", 6.5)
    c.drawString(col_r, ry, f"{cor.get('total_common', 0)} states w/ Senate + Governor")
    ry -= 10
    c.setFillColor(OLIVE)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(col_r, ry, f"{cor.get('aligned', 0)} aligned")
    c.setFillColor(TERRACOTTA)
    c.drawString(col_r + 55, ry, f"{cor.get('split', 0)} split")

    for s in [x for x in cor.get("states", []) if x.get("split")][:4]:
        ry -= 9
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(col_r, ry, s["state"])
        c.setFillColor(TERRACOTTA)
        c.setFont("Helvetica-Bold", 5.5)
        c.drawString(col_r + 20, ry, "SPLIT")
        sen_c = DEM_BLUE if s["senate_lean"] == "D" else REP_RED
        gov_c = DEM_BLUE if s["governor_lean"] == "D" else REP_RED
        c.setFillColor(TEXT2)
        c.setFont("Helvetica", 5.5)
        c.drawString(col_r + 46, ry, "Sen:")
        c.setFillColor(sen_c)
        c.drawString(col_r + 63, ry, f"{s['senate_lean']}{s['senate_margin'] * 100:+.0f}")
        c.setFillColor(TEXT2)
        c.drawString(col_r + 100, ry, "Gov:")
        c.setFillColor(gov_c)
        c.drawString(col_r + 117, ry, f"{s['governor_lean']}{s['governor_margin'] * 100:+.0f}")

    # ── RIGHT: Tipping Point Races ──
    ry -= 12
    movers = insights.get("tipping_point_races", [])
    if movers:
        ry = _section(c, col_r, ry, "TIPPING POINT RACES")

        for m in movers[:6]:
            ry -= 9
            lc = DEM_BLUE if m["leader"] == "D" else REP_RED
            mc = TERRACOTTA if m["margin"] < 0.10 else GOLD if m["margin"] < 0.20 else TEXT2

            c.setFillColor(TEXT)
            c.setFont("Helvetica-Bold", 6.5)
            c.drawString(col_r, ry, m["state"])
            c.setFillColor(lc)
            c.drawString(col_r + 22, ry, m["leader"])
            c.setFillColor(mc)
            c.setFont("Helvetica", 6.5)
            c.drawString(col_r + 34, ry, f"+{m['margin'] * 100:.1f}pp")

            # Mini bar with inline D/R %
            bx = col_r + 72
            bw = col_r_w - 72
            _bar(c, bx, ry, bw, 3, m["d_price"])
            # Inline labels on same line
            c.setFillColor(DEM_BLUE)
            c.setFont("Helvetica", 5)
            c.drawRightString(bx - 2, ry - 1, f"D{m['d_price'] * 100:.0f}")
            c.setFillColor(REP_RED)
            c.drawString(bx + bw + 2, ry - 1, f"R{m['r_price'] * 100:.0f}")

    # ── Footer ──
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.3)
    c.line(LM, 28, RM, 28)
    c.setFillColor(TEXT3)
    c.setFont("Helvetica", 5.5)
    c.drawString(LM, 19, f"Polyclawd Election Intelligence  ·  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    c.drawRightString(RM, 19, "Polymarket · Kalshi · PredictIt · Manifold · FEC")

    c.save()
    return output_path
