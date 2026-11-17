#!/usr/bin/env python3
"""
Ekspor database pembukuan (SQLite, lihat schema.sql) ke Excel 3 sheet:
  1. Akun          - daftar akun + total + saldo + cek neraca saldo
  2. Jurnal        - daftar jurnal per transaksi (gaya Zahir: baris judul + baris debit/kredit)
  3. Buku Bulanan  - buku besar per akun per bulan

Semua angka di sheet Akun dan Buku Bulanan adalah RUMUS (SUMIFS) yang membaca sheet Jurnal.
Sumber kebenaran tetap database; Excel adalah laporan yang di-generate ulang.

Pakai:  python export_xlsx.py pembukuan.db pembukuan.xlsx [Nama Usaha]
"""
import sqlite3
import sys
from datetime import date, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

MAXROW = 5000                       # batas range rumus; naikkan kalau jurnal > 5000 baris
J_FIRST = 5                         # baris pertama data di sheet Jurnal
FONT = "Arial"
NUM = '#,##0;(#,##0);"-"'
DATE = "d/m/yyyy"
HDR_FILL = PatternFill("solid", start_color="D9D9D9")
HDR_FONT = Font(name=FONT, bold=True, size=10)
ENTRY_FILL = PatternFill("solid", start_color="EEF3F8")
BASE = Font(name=FONT, size=10)
BOLD = Font(name=FONT, size=10, bold=True)
GREY = Font(name=FONT, size=9, color="808080")
NOTE = Font(name=FONT, size=9, italic=True, color="595959")
TITLE = Font(name=FONT, size=14, bold=True)
SUB = Font(name=FONT, size=10, color="595959")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
LINE = Border(bottom=THIN)


def month_end(d):
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def month_periods(d_min, d_max):
    out, d = [], date(d_min.year, d_min.month, 1)
    while d <= d_max:
        out.append((f"{d.year}-{d.month:02d}", d, month_end(d)))
        d = month_end(d) + timedelta(days=1)
    return out


def header(ws, row, labels, widths, fill=HDR_FILL, font=HDR_FONT):
    for i, (lab, w) in enumerate(zip(labels, widths), start=1):
        c = ws.cell(row=row, column=i, value=lab)
        c.font, c.fill, c.border = font, fill, BOX
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[row].height = 28


def style_row(ws, row, ncols, num_cols=(), date_cols=()):
    for col in range(1, ncols + 1):
        c = ws.cell(row=row, column=col)
        c.font, c.border = BASE, BOX
        if col in num_cols:
            c.number_format = NUM
        if col in date_cols:
            c.number_format = DATE
            c.alignment = Alignment(horizontal="center")


def export(db_path, out_path, usaha="Pembukuan"):
    db = sqlite3.connect(db_path)
    accounts = db.execute(
        "SELECT code, name, type, normal_balance, is_temporary FROM accounts WHERE active=1 ORDER BY code"
    ).fetchall()
    entries = db.execute(
        """SELECT e.id, e.entry_date, e.kind, COALESCE(e.description,''),
                  COALESCE(r.receipt_no, r.merchant, '')
           FROM journal_entries e LEFT JOIN receipts r ON r.id = e.receipt_id
           WHERE e.status = 'posted' ORDER BY e.entry_date, e.id"""
    ).fetchall()
    if not entries:
        sys.exit("Belum ada jurnal berstatus 'posted'.")
    lines_by_entry = {}
    for eid, kode, deb, kre in db.execute(
        "SELECT entry_id, account_code, debit, credit FROM journal_lines ORDER BY id"
    ):
        lines_by_entry.setdefault(eid, []).append((kode, deb, kre))

    wb = Workbook()
    n_acc = len(accounts)
    acc_last = max(n_acc + 1, 500)

    def J(col):
        return f"Jurnal!${col}${J_FIRST}:${col}${MAXROW}"

    # Kolom sheet Jurnal:
    # A Ref | B Tanggal | C Kode Akun | D Keterangan/Nama Akun | E Debit | F Kredit |
    # G Ref. Struk | H Jenis | I Saldo Normal | J Efek ke Saldo | K Tahun
    # ================= Sheet 2: Jurnal =================
    wj = wb.active
    wj.title = "Jurnal"
    wj["A1"] = usaha; wj["A1"].font = TITLE
    wj["A2"] = "Daftar Jurnal - Semua Transaksi"; wj["A2"].font = SUB
    header(wj, J_FIRST - 1,
           ["Ref.", "Tanggal", "Kode Akun", "Keterangan", "Debit", "Kredit",
            "Ref. Struk", "Jenis", "Saldo Normal", "Efek ke Saldo", "Tahun"],
           [10, 12, 11, 44, 15, 15, 20, 11, 12, 15, 8])
    for col in range(7, 12):            # kolom bantu: abu-abu
        wj.cell(J_FIRST - 1, col).font = Font(name=FONT, bold=True, size=9, color="808080")

    r = J_FIRST
    flat = []                            # (tgl, kind, kode) untuk menentukan baris buku bulanan
    for eid, tgl, kind, desc, ref in entries:
        d = date.fromisoformat(tgl)
        # baris judul transaksi
        wj.cell(r, 1, f"J{eid}"); wj.cell(r, 2, d); wj.cell(r, 4, desc)
        wj.cell(r, 7, ref); wj.cell(r, 8, kind)
        for col in range(1, 12):
            c = wj.cell(r, col)
            c.fill, c.font = ENTRY_FILL, BOLD
        wj.cell(r, 2).number_format = DATE
        wj.cell(r, 2).alignment = Alignment(horizontal="center")
        r += 1
        # baris debit/kredit
        for kode, deb, kre in lines_by_entry.get(eid, []):
            wj.cell(r, 2, d)                       # tanggal diulang (abu-abu) agar rumus bisa membaca
            wj.cell(r, 3, kode)
            wj.cell(r, 4, f'=IFERROR(INDEX(Akun!$B$2:$B${acc_last},MATCH(C{r},Akun!$A$2:$A${acc_last},0)),"AKUN TIDAK DITEMUKAN")')
            wj.cell(r, 5, deb if deb else None)
            wj.cell(r, 6, kre if kre else None)
            wj.cell(r, 8, kind)
            wj.cell(r, 9, f'=IFERROR(INDEX(Akun!$D$2:$D${acc_last},MATCH(C{r},Akun!$A$2:$A${acc_last},0)),"")')
            wj.cell(r, 10, f'=IF(I{r}="debit",N(E{r})-N(F{r}),N(F{r})-N(E{r}))')
            wj.cell(r, 11, f"=YEAR(B{r})")
            for col in range(1, 12):
                c = wj.cell(r, col)
                c.font = GREY if col in (2, 8, 9, 10, 11) else BASE
                c.border = LINE
            wj.cell(r, 2).number_format = DATE
            wj.cell(r, 2).alignment = Alignment(horizontal="center")
            wj.cell(r, 4).alignment = Alignment(indent=2)
            for col in (5, 6, 10):
                wj.cell(r, col).number_format = NUM
            wj.cell(r, 11).number_format = "0"
            if kind != "closing":
                flat.append((d, kode))
            r += 1
        r += 0
    wj.freeze_panes = f"A{J_FIRST}"

    # ================= Sheet 1: Akun =================
    wa = wb.create_sheet("Akun", 0)
    header(wa, 1,
           ["Kode", "Nama Akun", "Tipe", "Saldo Normal", "Akun Sementara?",
            "Total Debit", "Total Kredit", "Saldo"],
           [9, 34, 15, 13, 14, 16, 16, 16])
    for i, (kode, nama, tipe, normal, temp) in enumerate(accounts, start=2):
        wa.cell(i, 1, kode); wa.cell(i, 2, nama); wa.cell(i, 3, tipe)
        wa.cell(i, 4, normal); wa.cell(i, 5, "Ya" if temp else "Tidak")
        thn = f',{J("K")},$L$6' if temp else ""   # akun sementara: hanya tahun berjalan
        wa.cell(i, 6, f'=SUMIFS({J("E")},{J("C")},A{i},{J("H")},"<>closing"{thn})')
        wa.cell(i, 7, f'=SUMIFS({J("F")},{J("C")},A{i},{J("H")},"<>closing"{thn})')
        wa.cell(i, 8, f'=IF(D{i}="debit",F{i}-G{i},G{i}-F{i})')
        style_row(wa, i, 8, num_cols=(6, 7, 8))
    wa.freeze_panes = "A2"
    wa.auto_filter.ref = f"A1:H{n_acc + 1}"

    wa.cell(1, 10, "CEK NERACA SALDO").font = BOLD
    checks = [("Total debit semua jurnal", f"=SUM({J('E')})"),
              ("Total kredit semua jurnal", f"=SUM({J('F')})"),
              ("Selisih (harus 0)", "=L2-L3"),
              ("Status", '=IF(L4=0,"SEIMBANG","TIDAK SEIMBANG")'),
              ("Tahun berjalan (data terakhir)", f"=MAX({J('K')})")]
    for k, (lab, f) in enumerate(checks, start=2):
        wa.cell(k, 10, lab).font = BASE
        c = wa.cell(k, 12, f)
        c.font = BOLD
        c.number_format = "0" if "MAX" in f else NUM
    wa.column_dimensions["J"].width = 30
    wa.column_dimensions["K"].width = 2
    wa.column_dimensions["L"].width = 18
    notes = [
        "Catatan:",
        "- Semua angka di file ini berupa rumus yang membaca sheet Jurnal.",
        "- Jurnal berjenis 'closing' (jurnal penutup) TIDAK dihitung di sheet Akun dan Buku Bulanan,",
        "  supaya saldo akun sementara sebelum penutupan tetap terlihat.",
        "- Akun sementara (pendapatan, beban, prive) direset tiap 1 Januari;",
        "  di sheet Akun hanya tahun berjalan (L6) yang dihitung.",
        "  Akun neraca (aset, kewajiban, ekuitas) saldonya berlanjut antar periode.",
        "- Neraca saldo seimbang tidak menjamin tidak ada salah akun atau struk ganda.",
    ]
    for k, t in enumerate(notes, start=8):
        wa.cell(k, 10, t).font = BOLD if k == 8 else NOTE

    # ================= Sheet 3: Buku Bulanan =================
    wm = wb.create_sheet("Buku Bulanan")
    header(wm, 1,
           ["Bulan", "Mulai", "Akhir", "Kode", "Nama Akun", "Saldo Normal",
            "Saldo Awal", "Debit", "Kredit", "Saldo Akhir"],
           [10, 12, 12, 8, 34, 12, 16, 16, 16, 16])
    d_min, d_max = flat and (min(x[0] for x in flat), max(x[0] for x in flat)) or (date.today(),) * 2
    activity = {}
    for d, kode in flat:
        activity.setdefault(kode, []).append(d)
    r = 2
    for label, start, end in month_periods(d_min, d_max):
        for kode, nama, tipe, normal, temp in accounts:
            rel = [x for x in activity.get(kode, []) if x <= end and (not temp or x.year == start.year)]
            if not rel:
                continue
            wm.cell(r, 1, label); wm.cell(r, 2, start); wm.cell(r, 3, end)
            wm.cell(r, 4, kode)
            wm.cell(r, 5, f'=IFERROR(INDEX(Akun!$B$2:$B${acc_last},MATCH(D{r},Akun!$A$2:$A${acc_last},0)),"")')
            wm.cell(r, 6, normal)
            thn = f',{J("K")},YEAR(B{r})' if temp else ""
            wm.cell(r, 7, f'=SUMIFS({J("J")},{J("C")},D{r},{J("B")},"<"&B{r},{J("H")},"<>closing"{thn})')
            wm.cell(r, 8, f'=SUMIFS({J("E")},{J("C")},D{r},{J("B")},">="&B{r},{J("B")},"<="&C{r},{J("H")},"<>closing")')
            wm.cell(r, 9, f'=SUMIFS({J("F")},{J("C")},D{r},{J("B")},">="&B{r},{J("B")},"<="&C{r},{J("H")},"<>closing")')
            wm.cell(r, 10, f'=G{r}+IF(F{r}="debit",H{r}-I{r},I{r}-H{r})')
            style_row(wm, r, 10, num_cols=(7, 8, 9, 10), date_cols=(2, 3))
            r += 1
    wm.freeze_panes = "A2"
    wm.auto_filter.ref = f"A1:J{max(r - 1, 2)}"

    wb.save(out_path)
    n_lines = sum(len(v) for v in lines_by_entry.values())
    print(f"OK: {out_path}  ({len(entries)} jurnal, {n_lines} baris, {n_acc} akun)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    export(sys.argv[1], sys.argv[2], " ".join(sys.argv[3:]) or "Pembukuan")
