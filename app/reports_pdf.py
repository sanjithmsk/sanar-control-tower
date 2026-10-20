"""PDF export of the control tower report, built with fpdf2's core fonts (no font files needed)."""
from datetime import datetime

from fpdf import FPDF
from fpdf.enums import TableCellFillMode, XPos, YPos
from fpdf.fonts import FontFace

from .planner import buckets

INK, MUTED, GRID = (16, 40, 43), (88, 112, 117), (211, 222, 220)
SPEND, SAVED = (42, 120, 214), (27, 175, 122)  # categorical slots 1 and 3, validated for the light surface
_ASCII = str.maketrans({"–": "-", "—": "-", "−": "-", "→": "->", "’": "'", "‘": "'", "“": '"', "”": '"',
                        "…": "...", "≥": ">=", "≤": "<="})


def _t(s):
    """Core PDF fonts cover Latin-1 only."""
    return str(s).translate(_ASCII).encode("latin-1", "replace").decode("latin-1")


def compact(v):
    v = float(v)
    for div, unit in ((1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.1f}".rstrip("0").rstrip(".") + unit
    return f"{v:,.0f}"


def _columns(pdf, x, y, w, h, title, data, color, cur):
    """Single-series column chart: hairline grid, columns from one baseline, labelled max tick."""
    pdf.set_xy(x, y)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*INK)
    total = sum(v for _, v in data)
    pdf.cell(w, 5, _t(f"{title} - {cur} {compact(total)}"))
    top, base = y + 8, y + h - 5
    peak = max([v for _, v in data] + [1])
    pdf.set_draw_color(*GRID)
    pdf.set_line_width(0.2)
    for f in (0, 0.5, 1):
        gy = base - (base - top) * f
        pdf.line(x + 12, gy, x + w, gy)
        pdf.set_font("Helvetica", size=6)
        pdf.set_text_color(*MUTED)
        pdf.set_xy(x, gy - 1.5)
        pdf.cell(11, 3, compact(peak * f), align="R")
    slot = (w - 14) / max(len(data), 1)
    bar = min(slot * 0.6, 6)
    pdf.set_fill_color(*color)
    for i, (_, v) in enumerate(data):
        if v > 0:
            bh = (base - top) * v / peak
            pdf.rect(x + 14 + i * slot + (slot - bar) / 2, base - bh, bar, bh, style="F")
    pdf.set_font("Helvetica", size=6)
    pdf.set_text_color(*MUTED)
    if data:
        pdf.set_xy(x + 14, base + 1)
        pdf.cell((w - 14) / 2, 3, _t(data[0][0]))
        pdf.set_xy(x + 14 + (w - 14) / 2, base + 1)
        pdf.cell((w - 14) / 2, 3, _t(data[-1][0]), align="R")


def render(rep):
    """The report as PDF bytes."""
    cur, tot = rep["currency"], rep["totals"]
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=15)
    pdf.set_title(_t(f"{rep['network']} control tower report"))
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*INK)
    pdf.cell(0, 9, _t(f"{rep['network']} - control tower report"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(0, 4.5, _t(f"Period {rep['from']} to {rep['to']} ({rep['days']} days). Generated "
                               f"{datetime.now():%Y-%m-%d %H:%M}. Sample data: delivery history before the demo started "
                               "is illustrative; savings come from actions the tower executed."),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    tiles = [("Stockouts avoided", f"{tot['stockouts_avoided']:.0f}"), ("Units protected", compact(tot["units_protected"])),
             ("Waste prevented", f"{cur} {compact(tot['waste_saved'])}"),
             ("Saved by load sharing", f"{cur} {compact(tot['transport_saved'])}"),
             ("Procurement spend", f"{cur} {compact(tot['spend'])}"), ("Transport cost", f"{cur} {compact(tot['transport'])}"),
             ("Write-offs", f"{cur} {compact(tot['write_off'])}"),
             ("Auto-executed", f"{rep['auto_share_pct']:g}% of {rep['actions']}")]
    w, x0, y0 = (pdf.w - pdf.l_margin - pdf.r_margin) / 4, pdf.l_margin, pdf.get_y()
    for i, (label, value) in enumerate(tiles):
        x, y = x0 + (i % 4) * w, y0 + (i // 4) * 17
        pdf.set_draw_color(*GRID)
        pdf.rect(x + 0.5, y, w - 1, 15)
        pdf.set_xy(x + 3, y + 2)
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(*INK)
        pdf.cell(w - 6, 6, _t(value))
        pdf.set_xy(x + 3, y + 9)
        pdf.set_font("Helvetica", size=7.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(w - 6, 4, _t(label))
    y = y0 + 2 * 17 + 4
    full = pdf.w - pdf.l_margin - pdf.r_margin
    unit = "per day" if rep["days"] <= 31 else "per week"
    savings = [{"day": s["day"], "saved": s["waste_saved"] + s["transport_saved"]} for s in rep["series"]]
    _columns(pdf, x0, y, full / 2 - 4, 50, f"Procurement spend {unit}", buckets(rep["series"], "spend"), SPEND, cur)
    _columns(pdf, x0 + full / 2 + 4, y, full / 2 - 4, 50, f"Savings {unit}", buckets(savings, "saved"), SAVED, cur)
    pdf.set_xy(x0, y + 56)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*INK)
    pdf.cell(0, 7, "Supplier scorecards", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=8)
    pdf.set_draw_color(*GRID)
    pdf.set_fill_color(255, 255, 255)
    pdf.set_text_color(*INK)
    with pdf.table(col_widths=(41, 15, 14, 14, 14, 15, 32, 14, 21), text_align=("LEFT",) + ("RIGHT",) * 8,
                   line_height=5, borders_layout="HORIZONTAL_LINES", cell_fill_mode=TableCellFillMode.NONE,
                   headings_style=FontFace(emphasis="BOLD", color=INK, fill_color=(224, 236, 235))) as table:
        table.row([_t(h) for h in ("Supplier", "Deliveries", "On time", "In full", "OTIF", "Avg delay",
                                   "Compliance", "Status", f"Spend ({cur})")])
        pct = lambda v: "-" if v is None else f"{v:g}%"  # noqa: E731
        for s in rep["suppliers"]:
            table.row([_t(s["supplier"]), str(s["deliveries"]), pct(s["on_time_pct"]), pct(s["in_full_pct"]),
                       pct(s["otif_pct"]), f"{s['avg_delay_days']:g} d",
                       f"{s['compliance']:g}%" + (f" (contract {s['contract_compliance']}%)" if s["learned"] else ""),
                       "Blocked" if s["compliance"] < s["minimum"] else "OK", compact(s["spend"])])
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*INK)
    pdf.cell(0, 7, "Since the demo started", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(*MUTED)
    names = {"stockout_avoided": "Stockouts avoided", "waste_saved": "Waste prevented", "spend": "Procurement spend",
             "transport": "Transport cost", "transport_saved": "Saved by load sharing", "write_off": "Write-offs"}
    lines = [f"{names.get(k, k)}: {v['count']} record{'s' if v['count'] != 1 else ''}, {compact(v['units'] or 0)} units, "
             f"{cur} {compact(v['value'] or 0)}"
             for k, v in rep["since_start"].items()]
    pdf.multi_cell(0, 5, _t("\n".join(lines) or "No actions executed yet."), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())
