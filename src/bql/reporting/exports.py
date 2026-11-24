"""CSV / Excel / PDF exports."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .. import PAPER_ONLY_NOTICE


def to_excel(sheets: dict[str, pd.DataFrame], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame({"notice": [PAPER_ONLY_NOTICE]}).to_excel(xw, sheet_name="README", index=False)
        for name, df in sheets.items():
            d = df.copy()
            for c in d.columns:
                if isinstance(d[c].dtype, pd.DatetimeTZDtype):
                    d[c] = d[c].dt.tz_localize(None)
                if d[c].dtype == object:
                    d[c] = d[c].map(lambda v: str(v) if isinstance(v, (dict, list)) else v)
            d.to_excel(xw, sheet_name=name[:31], index=False)
    return path


def _fmt(v) -> str:
    if isinstance(v, float):
        if abs(v) >= 1000:
            return f"{v:,.0f}"
        return f"{v:.4f}"
    return str(v)


def to_pdf(title: str, sections: list[tuple[str, object]], path: Path, watermark: str | None = None) -> Path:
    """sections: list of (heading, content) where content is str, dict or DataFrame."""
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=1.2 * cm, rightMargin=1.2 * cm,
                            topMargin=1.2 * cm, bottomMargin=1.2 * cm, title=title)
    story = [Paragraph(title, styles["Title"]), Paragraph(PAPER_ONLY_NOTICE, styles["Italic"])]
    if watermark:
        story.append(Paragraph(f"<font color='red'><b>{watermark}</b></font>", styles["Heading3"]))
    story.append(Spacer(1, 0.3 * cm))
    for heading, content in sections:
        if heading == "__pagebreak__":
            story.append(PageBreak())
            continue
        story.append(Paragraph(heading, styles["Heading2"]))
        if isinstance(content, pd.DataFrame):
            df = content.head(40)
            if df.empty:
                story.append(Paragraph("No records.", styles["Normal"]))
                continue
            df = df.iloc[:, :12]
            data = [list(map(str, df.columns))] + [[_fmt(v)[:28] for v in row] for row in df.itertuples(index=False)]
            t = Table(data, repeatRows=1)
            t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfe6ee")),
                                   ("GRID", (0, 0), (-1, -1), 0.25, colors.grey)]))
            story.append(t)
        elif isinstance(content, dict):
            data = [[str(k), _fmt(v)[:90]] for k, v in content.items()]
            if data:
                t = Table(data, colWidths=[7 * cm, 18 * cm])
                t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey)]))
                story.append(t)
        else:
            for para in str(content).split("\n\n"):
                story.append(Paragraph(para.replace("\n", "<br/>"), styles["Normal"]))
        story.append(Spacer(1, 0.3 * cm))
    doc.build(story)
    return path
