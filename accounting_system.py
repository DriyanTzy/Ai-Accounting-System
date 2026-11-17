#!/usr/bin/env python3
"""
Sistem Pembukuan Pintar Berbasis AI (Versi 2.0)
Pipeline Lengkap:
  1. Pengeluaran: Foto/JSON Struk -> AI Vision Parser -> Generator Jurnal Draft -> Review Manusia -> Posting DB
  2. Pemasukan: Penjualan / Invoice Klien -> Jurnal Pendapatan
  3. Modal & Bank: Setor Modal Awal, Transfer Kas/Bank, Bayar Utang/Piutang
  4. Pajak & Pelacak Omzet: Monitor batas Rp 500 juta & Rp 4,8 Miliar sesuai PP 20/2026
  5. Penyesuaian Akhir Bulan: Depresiasi Peralatan otomatis
  6. Ekspor Excel: 3 sheet formula SUMIFS gaya Zahir Accounting
"""

import os
import sys
import json
import sqlite3
import argparse
import calendar
from typing import Optional
from datetime import datetime

from journal_engine import AccountingEngine, calculate_fingerprint
from receipt_parser import parse_receipt_with_gemini, mock_receipt_parser
from export_xlsx import export as export_to_excel

DB_FILE = "pembukuan.db"


def get_db(db_path=DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_database(db_path=DB_FILE):
    """Inisialisasi database SQLite (Aman dijalankan berulang kali)."""
    conn = get_db(db_path)
    cur = conn.cursor()

    # Eksekusi schema.sql dengan proteksi jika tabel sudah ada
    with open("schema.sql", "r", encoding="utf-8") as f:
        schema_text = f.read()

    # Pisahkan perintah SQL untuk eksekusi yang aman
    for statement in schema_text.split(";"):
        stmt = statement.strip()
        if not stmt:
            continue
        try:
            cur.execute(stmt)
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            pass

    # Seed COA
    with open("seed_coa.sql", "r", encoding="utf-8") as f:
        seed_text = f.read()
    for stmt in seed_text.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                cur.execute(stmt)
            except sqlite3.IntegrityError:
                pass

    # Pastikan periode default terbuka (2025 s/d 2028)
    for year in range(2025, 2029):
        for m in range(1, 13):
            cur.execute("INSERT OR IGNORE INTO periods (id, status) VALUES (?, 'open')", (f"{year}-{m:02d}",))

    conn.commit()
    conn.close()
    print("[OK] Inisialisasi database & Chart of Accounts siap digunakan.")


def process_receipt_data(extracted_json: dict, file_path: str = "", db_path=DB_FILE) -> Optional[int]:
    """Memasukkan hasil parsing struk ke database dengan validasi duplikasi ketat."""
    conn = get_db(db_path)
    cur = conn.cursor()

    is_biz = extracted_json.get("is_business_transaction", True)
    if not is_biz:
        print("[!] Dokumen diidentifikasi BUKAN transaksi bisnis. Disimpan sebagai arsip non-transaksi (tidak dijurnal).")
        cur.execute(
            """INSERT INTO receipts (file_path, merchant, raw_extraction, status)
               VALUES (?, ?, ?, 'not_a_transaction')""",
            (file_path, extracted_json.get("merchant", "Non-Transaksi"), json.dumps(extracted_json))
        )
        conn.commit()
        conn.close()
        return None

    merchant = extracted_json.get("merchant", "Toko")
    r_date = extracted_json.get("receipt_date", datetime.now().strftime("%Y-%m-%d"))
    total = int(extracted_json.get("total", 0))
    receipt_no = extracted_json.get("receipt_no", "")
    subtotal = int(extracted_json.get("subtotal", total))
    tax = int(extracted_json.get("tax", 0))
    freight = int(extracted_json.get("freight", 0))
    payment_method = extracted_json.get("payment_method", "cash")

    fp = calculate_fingerprint(merchant, r_date, total, receipt_no)

    # Cek duplikat struk
    existing = cur.execute(
        "SELECT id, status FROM receipts WHERE fingerprint = ?", (fp,)
    ).fetchone()
    if existing:
        print(f"[!] PERINGATAN: Struk ganda terdeteksi! Sudah terdaftar di Struk ID #{existing[0]} (Status: '{existing[1]}').")
        print("    -> Sistem membatalkan proses agar saldo tidak tercatat dobel.")
        conn.close()
        return None

    cur.execute(
        """INSERT INTO receipts (file_path, merchant, receipt_no, receipt_date, subtotal, tax, freight, total, payment_method, fingerprint, raw_extraction, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'extracted')""",
        (file_path, merchant, receipt_no, r_date, subtotal, tax, freight, total, payment_method, fp, json.dumps(extracted_json))
    )
    receipt_id = cur.lastrowid

    # Masukkan items
    engine = AccountingEngine(conn)
    items = extracted_json.get("items", [])
    if not items and total > 0:
        items = [{"description": f"Belanja di {merchant}", "qty": 1, "amount": total}]

    for it in items:
        desc = it.get("description", "Item")
        qty = it.get("qty", 1)
        amt = int(it.get("amount", 0))
        cat = it.get("suggested_category")
        sugg_acc, conf = engine.suggest_item_account(merchant, desc, item_amount=amt, ai_category=cat)
        cur.execute(
            """INSERT INTO receipt_items (receipt_id, description, qty, amount, suggested_account, confidence)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (receipt_id, desc, qty, amt, sugg_acc, conf)
        )

    conn.commit()
    conn.close()
    print(f"[+] Struk ID #{receipt_id} berhasil disimpan: {merchant} ({r_date}) - Rp {total:,}")
    return receipt_id


def generate_draft(receipt_id: int, db_path=DB_FILE) -> Optional[int]:
    """Jalankan engine jurnal untuk membuat usulan jurnal berstatus draft."""
    if not receipt_id:
        return None
    conn = get_db(db_path)
    engine = AccountingEngine(conn)
    try:
        entry_id = engine.save_journal_draft(receipt_id)
        r_status = conn.execute("SELECT status FROM receipts WHERE id = ?", (receipt_id,)).fetchone()[0]
        
        print(f"[+] Jurnal Draft #J{entry_id} dibuat. Status Struk: '{r_status}'")
        print("-" * 68)
        print(f"Preview Jurnal Transaksi (Ref: J{entry_id}):")
        lines = conn.execute(
            """SELECT l.account_code, a.name, l.debit, l.credit, l.memo
               FROM journal_lines l JOIN accounts a ON a.code = l.account_code
               WHERE l.entry_id = ? ORDER BY l.id""", (entry_id,)
        ).fetchall()
        print(f"{'Kode':<6} | {'Nama Akun':<30} | {'Debit (Rp)':<12} | {'Kredit (Rp)':<12}")
        print("-" * 68)
        for code, name, deb, kre, memo in lines:
            deb_s = f"{deb:,}" if deb else "-"
            kre_s = f"{kre:,}" if kre else "-"
            print(f"{code:<6} | {name:<30} | {deb_s:<12} | {kre_s:<12}")
        print("-" * 68)
        return entry_id
    finally:
        conn.close()


def review_and_post(entry_id: int, approve_all: bool = False, db_path=DB_FILE):
    """Meninjau draft jurnal dan posting ke database."""
    if not entry_id:
        return
    conn = get_db(db_path)
    engine = AccountingEngine(conn)
    cur = conn.cursor()

    entry = cur.execute(
        "SELECT id, entry_date, receipt_id, description, status FROM journal_entries WHERE id = ?",
        (entry_id,)
    ).fetchone()
    if not entry:
        print(f"[-] Jurnal ID {entry_id} tidak ditemukan.")
        conn.close()
        return

    if entry[4] == 'posted':
        print(f"[i] Jurnal ID {entry_id} sudah pernah di-posting.")
        conn.close()
        return

    receipt_id = entry[2]
    receipt_status = None
    merchant = "Manual"
    if receipt_id:
        r_row = cur.execute("SELECT merchant, status FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        if r_row:
            merchant, receipt_status = r_row

    # Validasi --auto-post: jika butuh review manual, jangan izinkan auto-post lolos diam-diam
    if approve_all and receipt_status == 'needs_review':
        print(f"\n[!] PERHATIAN: Jurnal J{entry_id} ditandai 'needs_review' (nominal besar/confidence rendah/selisih).")
        print("    -> Auto-post dibatalkan. Silakan lakukan review manual: python accounting_system.py review --id", entry_id)
        conn.close()
        return

    lines = cur.execute(
        """SELECT l.id, l.account_code, a.name, l.debit, l.credit
           FROM journal_lines l JOIN accounts a ON a.code = l.account_code
           WHERE l.entry_id = ?""", (entry_id,)
    ).fetchall()

    print(f"\n[REVIEW] Jurnal Ref: J{entry_id} ({entry[3]})")
    for lid, code, name, deb, kre in lines:
        side = f"Debit  Rp {deb:,}" if deb else f"Kredit Rp {kre:,}"
        print(f"  Line #{lid}: [{code}] {name:<30} -> {side}")

    if not approve_all:
        ans = input("\nSetujui dan Posting jurnal ini? (Y/n/koreksi [line_id] [kode_baru]): ").strip()
        ans_lower = ans.lower()

        if ans_lower in ('n', 'no', 'batal', 'cancel'):
            print(f"[-] Posting dibatalkan oleh pengguna. Jurnal J{entry_id} tetap berstatus 'draft'.")
            conn.close()
            return

        if ans_lower.startswith("koreksi"):
            parts = ans.split()
            if len(parts) >= 3:
                line_id = int(parts[1])
                new_code = parts[2]
                acc = cur.execute("SELECT name FROM accounts WHERE code = ? AND active = 1", (new_code,)).fetchone()
                if not acc:
                    print(f"[-] Akun {new_code} tidak valid atau tidak aktif.")
                    conn.close()
                    return

                cur.execute("UPDATE journal_lines SET account_code = ? WHERE id = ?", (new_code, line_id))
                conn.commit()
                print(f"[+] Baris #{line_id} diperbarui ke [{new_code}] {acc[0]}")

                # Simpan koreksi ke classification_rules
                engine.record_correction_rule(merchant_pattern=merchant, item_pattern=None, correct_account_code=new_code)
                print(f"[AI Learning] Aturan disimpan: Toko '{merchant}' dihubungkan ke akun [{new_code}].")
            else:
                print("[-] Format koreksi salah. Gunakan: koreksi [line_id] [kode_baru]")
                conn.close()
                return

    # Eksekusi posting
    try:
        engine.post_journal_entry(entry_id)
        print(f"[OK] Sukses: Jurnal J{entry_id} berhasil di-posting ke buku besar!")
    except sqlite3.OperationalError as e:
        print(f"[GAGAL POSTING (TRIGGER ERROR)]: {e}")
    finally:
        conn.close()


def record_initial_capital(amount: int, to_account: str = "bank", entry_date: str = None, desc: str = "Setoran Modal Awal"):
    """Mencatat setoran modal awal (Kas/Bank pada Modal Pemilik)."""
    conn = get_db()
    cur = conn.cursor()
    d = entry_date or datetime.now().strftime("%Y-%m-%d")
    debit_code = "102" if to_account.lower() == "bank" else "101"
    debit_name = "Bank" if debit_code == "102" else "Kas"

    cur.execute(
        """INSERT INTO journal_entries (entry_date, description, kind, status)
           VALUES (?, ?, 'opening', 'draft')""",
        (d, desc)
    )
    eid = cur.lastrowid
    cur.execute("INSERT INTO journal_lines (entry_id, account_code, debit, credit, memo) VALUES (?, ?, ?, 0, ?)",
                (eid, debit_code, amount, f"Setoran modal ke {debit_name}"))
    cur.execute("INSERT INTO journal_lines (entry_id, account_code, debit, credit, memo) VALUES (?, '311', 0, ?, 'Modal Pemilik')",
                (eid, amount))
    conn.commit()

    engine = AccountingEngine(conn)
    engine.post_journal_entry(eid)
    conn.close()
    print(f"[OK] Berhasil mencatat setoran modal awal: Rp {amount:,} ke [{debit_code}] {debit_name} (Ref: J{eid})")


def record_sale(client: str, amount: int, payment_method: str = "bank", entry_date: str = None, desc: str = ""):
    """Mencatat pemasukan/penjualan/invoice jasa."""
    conn = get_db()
    cur = conn.cursor()
    d = entry_date or datetime.now().strftime("%Y-%m-%d")
    description = desc or f"Penjualan / Invoice: {client}"

    # Debit akun penerima
    if payment_method == "bank":
        debit_code = "102"
    elif payment_method == "cash":
        debit_code = "101"
    else:
        debit_code = "112"  # Piutang Usaha

    cur.execute(
        """INSERT INTO journal_entries (entry_date, description, kind, status)
           VALUES (?, ?, 'normal', 'draft')""",
        (d, description)
    )
    eid = cur.lastrowid
    cur.execute("INSERT INTO journal_lines (entry_id, account_code, debit, credit, memo) VALUES (?, ?, ?, 0, ?)",
                (eid, debit_code, amount, f"Penerimaan dari {client}"))
    cur.execute("INSERT INTO journal_lines (entry_id, account_code, debit, credit, memo) VALUES (?, '400', 0, ?, 'Pendapatan Penjualan')",
                (eid, amount))
    conn.commit()

    engine = AccountingEngine(conn)
    engine.post_journal_entry(eid)
    conn.close()
    print(f"[OK] Berhasil mencatat penjualan: Rp {amount:,} dari {client} (Ref: J{eid})")


def track_omzet_and_tax(year: str = None):
    """
    Pelacak omzet kumulatif tahunan & estimasi pajak UMKM (PP 20/2026):
    - Batas bebas pajak Orang Pribadi: Rp 500.000.000
    - Tarif PPh Final UMKM: 0,5%
    - Batas Wajib PKP: Rp 4.800.000.000
    """
    y = year or datetime.now().strftime("%Y")
    conn = get_db()
    cur = conn.cursor()

    row = cur.execute(
        """SELECT COALESCE(SUM(l.credit) - SUM(l.debit), 0)
           FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id
           WHERE l.account_code = '400' AND e.status = 'posted' AND substr(e.entry_date, 1, 4) = ?""",
        (y,)
    ).fetchone()
    omzet = row[0] if row else 0

    threshold_free = 500000000
    threshold_pkp = 4800000000

    taxable_omzet = max(0, omzet - threshold_free)
    estimated_tax = round(taxable_omzet * 0.005)

    print("\n" + "=" * 65)
    print(f"LAPORAN OMZET & ESTIMASI PAJAK UMKM TAHUN {y} (PP 20/2026)")
    print("=" * 65)
    print(f"Total Omzet Kumulatif      : Rp {omzet:>15,}")
    print(f"Batas Bebas Pajak OP       : Rp {threshold_free:>15,} (PPh Final 0%)")
    print(f"Omzet Kena Pajak           : Rp {taxable_omzet:>15,}")
    print(f"Estimasi PPh Final 0,5%    : Rp {estimated_tax:>15,}")
    print("-" * 65)
    if omzet < threshold_free:
        sisa = threshold_free - omzet
        print(f"Status: BEBAS PAJAK (Sisa kuota bebas pajak: Rp {sisa:,})")
    elif omzet <= threshold_pkp:
        print("Status: WAJIB SETOR PPh Final 0,5% setiap tanggal 15 bulan berikutnya.")
    else:
        print("Status: PERINGATAN - Omzet melebihi Rp 4,8 Miliar. WAJIB KUKUH PKP!")
    print("=" * 65 + "\n")
    conn.close()


def generate_month_end_adjustments(month: str, db_path=DB_FILE):
    """
    Kandidat Jurnal Penyesuaian Akhir Bulan:
    1. Depresiasi Peralatan (157 -> 711 & 158)
    2. Dilengkapi proteksi anti-duplikasi penyesuaian untuk bulan yang sama.
    """
    conn = get_db(db_path)
    cur = conn.cursor()

    # Cek apakah sudah pernah dibuat penyesuaian depresiasi di bulan ini
    existing = cur.execute(
        """SELECT id FROM journal_entries 
           WHERE kind = 'adjusting' AND substr(entry_date, 1, 7) = ? 
             AND description LIKE '%penyusutan peralatan%'""",
        (month,)
    ).fetchone()
    if existing:
        print(f"[!] Penyesuaian depresiasi untuk bulan {month} sudah pernah dibuat (Ref: J{existing[0]}).")
        conn.close()
        return

    peralatan_bal = cur.execute(
        """SELECT COALESCE(SUM(l.debit) - SUM(l.credit), 0)
           FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id
           WHERE l.account_code = '157' AND e.status = 'posted' AND substr(e.entry_date, 1, 7) <= ?""",
        (month,)
    ).fetchone()[0]

    if peralatan_bal <= 0:
        print(f"[*] Tidak ada saldo peralatan yang perlu disusutkan untuk bulan {month}.")
        conn.close()
        return

    # Depresiasi garis lurus: 4 tahun (48 bulan)
    depr_monthly = round(peralatan_bal / 48)

    y_str, m_str = month.split("-")
    last_day_num = calendar.monthrange(int(y_str), int(m_str))[1]
    last_date = f"{month}-{last_day_num:02d}"

    print(f"\n[*] Usulan Jurnal Penyesuaian Akhir Bulan ({month}):")
    print(f"  Tanggal          : {last_date}")
    print(f"  Saldo Peralatan  : Rp {peralatan_bal:,}")
    print(f"  Beban Penyusutan : Rp {depr_monthly:,} (Garis lurus 48 bulan)")
    print(f"  Debit  [711] Beban Penyusutan           : Rp {depr_monthly:,}")
    print(f"  Kredit [158] Akumulasi Penyusutan       : Rp {depr_monthly:,}")

    ans = input("\nBuat jurnal penyesuaian draft ini? (Y/n): ").strip()
    if ans.lower() in ('y', 'yes', ''):
        cur.execute(
            """INSERT INTO journal_entries (entry_date, description, kind, status)
               VALUES (?, ?, 'adjusting', 'draft')""",
            (last_date, f"Penyesuaian penyusutan peralatan bulan {month}")
        )
        eid = cur.lastrowid
        cur.execute("INSERT INTO journal_lines (entry_id, account_code, debit, credit, memo) VALUES (?, '711', ?, 0, 'Beban Penyusutan Peralatan')",
                    (eid, depr_monthly))
        cur.execute("INSERT INTO journal_lines (entry_id, account_code, debit, credit, memo) VALUES (?, '158', 0, ?, 'Akumulasi Penyusutan Peralatan')",
                    (eid, depr_monthly))
        conn.commit()
        print(f"[+] Dibuat Draft Jurnal Penyesuaian Ref: J{eid}")
        
        post_ans = input("Posting sekarang ke buku besar? (Y/n): ").strip()
        if post_ans.lower() in ('y', 'yes', ''):
            engine = AccountingEngine(conn)
            engine.post_journal_entry(eid)
            print(f"[OK] Sukses: Jurnal penyesuaian J{eid} berhasil di-posting!")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="AI Accounting System Pipeline (v2.0)")
    subparsers = parser.add_subparsers(dest="cmd")

    subparsers.add_parser("init", help="Inisialisasi database pembukuan.db")

    scan_p = subparsers.add_parser("scan", help="Scan struk baru (AI OCR / Mock)")
    scan_p.add_argument("--image", help="Path file gambar struk")
    scan_p.add_argument("--api-key", help="Gemini API Key (opsional jika sudah di .env)")
    scan_p.add_argument("--mock", choices=["atk", "dagang", "peralatan"], default=None, help="Gunakan mock parser")
    scan_p.add_argument("--auto-post", action="store_true", help="Posting otomatis hanya jika status ready")

    rev_p = subparsers.add_parser("review", help="Review dan posting jurnal draft")
    rev_p.add_argument("--id", type=int, required=True, help="Entry ID Jurnal")
    rev_p.add_argument("--yes", action="store_true", help="Setujui langsung")

    cap_p = subparsers.add_parser("capital", help="Catat setoran modal awal")
    cap_p.add_argument("--amount", type=int, required=True, help="Nominal modal (Rupiah)")
    cap_p.add_argument("--to", choices=["bank", "cash"], default="bank", help="Disetor ke mana (bank/cash)")
    cap_p.add_argument("--date", help="Tanggal ISO (YYYY-MM-DD)")

    sale_p = subparsers.add_parser("sale", help="Catat pendapatan / penjualan / invoice klien")
    sale_p.add_argument("--client", required=True, help="Nama klien atau pembeli")
    sale_p.add_argument("--amount", type=int, required=True, help="Nominal penjualan (Rupiah)")
    sale_p.add_argument("--payment", choices=["bank", "cash", "credit"], default="bank", help="Cara terima pembayaran")
    sale_p.add_argument("--date", help="Tanggal ISO (YYYY-MM-DD)")

    omzet_p = subparsers.add_parser("omzet", help="Pelacak omzet kumulatif & pajak UMKM PP 20/2026")
    omzet_p.add_argument("--year", help="Tahun buku (default: tahun berjalan)")

    adj_p = subparsers.add_parser("adjust", help="Kandidat penyesuaian akhir bulan")
    adj_p.add_argument("--month", required=True, help="Format YYYY-MM")

    subparsers.add_parser("export", help="Ekspor database ke laporan Excel 3 sheet")

    args = parser.parse_args()

    if args.cmd == "init":
        init_database()

    elif args.cmd == "scan":
        if args.image:
            print(f"[*] Memproses struk dari file: {args.image}...")
            data = parse_receipt_with_gemini(args.image, api_key=args.api_key)
        elif args.mock:
            print(f"[*] Memproses struk demo mock skenario: '{args.mock}'...")
            data = mock_receipt_parser(args.mock)
        else:
            print("[-] Harap berikan --image atau --mock [atk|dagang|peralatan]")
            return

        print("[AI OCR Output JSON]:")
        print(json.dumps(data, indent=2))

        rid = process_receipt_data(data, file_path=args.image or "mock_file")
        if rid:
            eid = generate_draft(rid)
            if eid and args.auto_post:
                review_and_post(eid, approve_all=True)

    elif args.cmd == "review":
        review_and_post(args.id, approve_all=args.yes)

    elif args.cmd == "capital":
        record_initial_capital(args.amount, to_account=args.to, entry_date=args.date)

    elif args.cmd == "sale":
        record_sale(args.client, args.amount, payment_method=args.payment, entry_date=args.date)

    elif args.cmd == "omzet":
        track_omzet_and_tax(year=args.year)

    elif args.cmd == "adjust":
        generate_month_end_adjustments(args.month)

    elif args.cmd == "export":
        export_to_excel("pembukuan.db", "pembukuan.xlsx", "Toko Usaha Anda")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
