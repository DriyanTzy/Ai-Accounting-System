"""
Modul Generator Jurnal & Aturan Akuntansi (Versi 2.0 - Stabil & IFRS Compliant)
Perbaikan Bug:
1. Penanganan PPN inklusif vs eksklusif (tidak dobel catat)
2. Akun 799 dibatasi maksimal Rp 1.000 (selisih > 1.000 wajib review, tidak disembunyikan)
3. Regex word-boundary (tidak salah klasifikasi fotokopi -> kopi atau tolak angin -> tol)
4. Memanfaatkan suggested_category dari AI Vision
5. Batas kapitalisasi aset tetap (default Rp 1.000.000); di bawah itu masuk Beban Perlengkapan
6. Ongkir non-dagang masuk ke beban/aset terkait (bukan 741 Freight-out)
7. Rule feedback spesifik per merchant + item (tidak meracuni merchant secara global)
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple


def calculate_fingerprint(merchant: str, receipt_date: str, total: int, receipt_no: Optional[str] = "") -> str:
    """Hash unik struk untuk mencegah duplikasi pencatatan (hal. 2-21)."""
    raw = f"{merchant.strip().lower()}|{receipt_date.strip()}|{total}|{(receipt_no or '').strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AccountingEngine:
    def __init__(self, db_conn: sqlite3.Connection):
        self.db = db_conn

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def find_rule(self, merchant: str, item_desc: str) -> Optional[str]:
        """Cari aturan spesifik di tabel classification_rules berdasarkan riwayat koreksi."""
        merchant_lower = merchant.strip().lower()
        item_lower = item_desc.strip().lower()

        # 1. Prioritas utama: Rule spesifik merchant + item pattern
        row = self.db.execute(
            """SELECT account_code, id FROM classification_rules
               WHERE ? LIKE '%' || LOWER(merchant_pattern) || '%'
                 AND item_pattern IS NOT NULL AND item_pattern <> ''
                 AND ? LIKE '%' || LOWER(item_pattern) || '%'
               ORDER BY hits DESC LIMIT 1""",
            (merchant_lower, item_lower)
        ).fetchone()
        if row:
            self.db.execute("UPDATE classification_rules SET hits = hits + 1 WHERE id = ?", (row[1],))
            return row[0]

        # 2. Rule merchant umum HANYA jika item_pattern kosong dan toko spesifik (bukan toserba umum)
        general_stores = ["indomaret", "alfamart", "gramedia", "tokopedia", "shopee"]
        if not any(g in merchant_lower for g in general_stores):
            row2 = self.db.execute(
                """SELECT account_code, id FROM classification_rules
                   WHERE ? LIKE '%' || LOWER(merchant_pattern) || '%'
                     AND (item_pattern IS NULL OR item_pattern = '')
                   ORDER BY hits DESC LIMIT 1""",
                (merchant_lower,)
            ).fetchone()
            if row2:
                self.db.execute("UPDATE classification_rules SET hits = hits + 1 WHERE id = ?", (row2[1],))
                return row2[0]

        return None

    def suggest_item_account(self, merchant: str, item_desc: str, item_amount: int = 0,
                            ai_category: Optional[str] = None) -> Tuple[str, float]:
        """
        Klasifikasi akun dengan hierarki:
        1. classification_rules (hasil review manusia sebelumnya)
        2. AI Suggested Category dari Vision OCR
        3. Regex word-boundary yang aman dari false positive
        4. Ambang batas kapitalisasi aset tetap
        """
        # 1. Cek riwayat aturan koreksi manusia
        rule_acc = self.find_rule(merchant, item_desc)
        if rule_acc:
            return rule_acc, 0.95

        text = f"{item_desc} {merchant}".lower()

        # Helper untuk pencocokan kata utuh (bukan substring)
        def match_words(patterns: List[str], target: str) -> bool:
            for p in patterns:
                if re.search(r'\b' + re.escape(p) + r'\b', target, re.IGNORECASE):
                    return True
            return False

        # 2. Evaluasi AI Suggested Category
        if ai_category:
            cat = ai_category.strip().lower()
            if cat in ("persediaan", "inventory", "barang_dagang"):
                return "120", 0.90
            elif cat in ("atk", "stationery"):
                return "632", 0.90
            elif cat in ("kemasan", "packaging"):
                return "633", 0.90
            elif cat in ("konsumsi", "makanan", "minuman"):
                # Jangan salah menganggap barang dagangan di toko sebagai konsumsi
                if not any(w in merchant.lower() for w in ["distributor", "supplier", "grosir"]):
                    return "738", 0.88
            elif cat in ("transportasi", "bensin", "travel"):
                return "735", 0.88
            elif cat in ("peralatan", "equipment", "aset_tetap"):
                # Cek ambang batas kapitalisasi (default Rp 1.000.000)
                if item_amount >= 1000000:
                    return "157", 0.90
                else:
                    return "631", 0.85  # Beban Perlengkapan

        # 3. Heuristik Regex Word-Boundary (Mencegah fotokopi -> kopi, tolak angin -> tol)
        # ATK
        if match_words(["kertas", "pulpen", "atk", "spidol", "buku tulis", "stapler", "tinta", "binder", "map", "pensil"], text):
            return "632", 0.90

        # Kemasan
        if match_words(["kardus", "bubble wrap", "lakban", "kemasan", "box", "packaging", "plastik packing"], text):
            return "633", 0.90

        # Transportasi
        if match_words(["bensin", "pertamax", "pertalite", "spbu", "shell", "tarif tol", "parkir", "grab", "gojek", "taksi", "tiket pesawat", "kereta"], text):
            return "735", 0.88

        # Konsumsi (Gunakan kata spesifik, hindari potongan kata)
        if match_words(["kopi", "makan siang", "snack", "teh", "konsumsi", "restoran", "kafe", "catering", "warung", "bakso", "mie ayam"], text):
            return "738", 0.85

        # Listrik, Air, Internet & Software SaaS
        if match_words(["pln", "listrik", "token listrik", "indihome", "biznet", "telkomsel", "pulsa", "paket data", "wifi", "pdam", "aws", "gcp", "hosting", "domain"], text):
            return "732", 0.90

        # Peralatan Aset Tetap (> 1 juta) vs Perlengkapan (< 1 juta)
        if match_words(["laptop", "printer", "meja kerja", "kursi kantor", "lemari", "ac", "komputer", "macbook", "scanner"], text):
            if item_amount >= 1000000:
                return "157", 0.88
            else:
                return "631", 0.85

        # Barang Dagang
        if match_words(["persediaan", "barang dagang", "supplier", "kulakan", "grosir"], text):
            return "120", 0.88

        # Fallback default butuh review manual
        return "790", 0.50

    def determine_credit_account(self, payment_method: str) -> str:
        """Sisi kredit tergantung cara bayar."""
        mapping = {
            "cash": "101",          # Kas
            "bank": "102",          # Bank (transfer / QRIS / debit)
            "credit_card": "202",   # Utang Kartu Kredit
            "on_account": "201"     # Utang Usaha (tempo / belum bayar)
        }
        return mapping.get(payment_method, "101")

    def generate_journal_lines_from_receipt(self, receipt_id: int) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Menghasilkan baris-baris jurnal (Debit & Kredit) dari struk di database.
        Mengembalikan (lines, needs_review_flag)
        """
        r = self.db.execute(
            """SELECT id, merchant, receipt_date, subtotal, tax, freight, total, payment_method
               FROM receipts WHERE id = ?""", (receipt_id,)
        ).fetchone()
        if not r:
            raise ValueError(f"Struk ID {receipt_id} tidak ditemukan.")

        _, merchant, r_date, subtotal, tax, freight, total, payment_method = r
        tax = tax or 0
        freight = freight or 0
        total = total or 0

        is_pkp = self.get_setting("is_pkp", "0") == "1"
        review_threshold = int(self.get_setting("review_threshold", "1000000"))
        min_confidence = float(self.get_setting("min_confidence", "0.85"))

        items = self.db.execute(
            "SELECT id, description, qty, amount, suggested_account, confidence, final_account FROM receipt_items WHERE receipt_id = ?",
            (receipt_id,)
        ).fetchall()

        if not items and total > 0:
            # Fallback jika struk tanpa rincian item
            items = [(None, f"Belanja di {merchant}", 1, total - freight, None, 0.5, None)]

        needs_review = False
        if total >= review_threshold:
            needs_review = True

        raw_sum_items = sum(it[3] for it in items)

        # Deteksi Pajak: Tax-Inclusive vs Tax-Exclusive
        # Struk ritel umumnya inklusif: sum(items) == total (atau sum(items) + freight == total)
        # Struk B2B umumnya eksklusif: sum(items) + tax + freight == total
        is_tax_inclusive = False
        if tax > 0:
            if abs(raw_sum_items + freight - total) <= 100:
                is_tax_inclusive = True
            elif abs(raw_sum_items + tax + freight - total) <= 100:
                is_tax_inclusive = False
            else:
                # Pola tidak jelas, flag review
                needs_review = True

        aggregated_debits: Dict[str, int] = {}

        # 1. Proses item per baris
        for it in items:
            item_id = it[0]
            desc = it[1]
            amt = it[3]
            sugg_acc = it[4]
            conf = it[5]
            final_acc = it[6]

            account = final_acc or sugg_acc
            item_conf = conf if conf is not None else 0.5

            if not account:
                account, item_conf = self.suggest_item_account(merchant, desc, item_amount=amt)
                if item_id:
                    self.db.execute(
                        "UPDATE receipt_items SET suggested_account = ?, confidence = ? WHERE id = ?",
                        (account, item_conf, item_id)
                    )

            if item_conf < min_confidence or account == "790":
                needs_review = True

            # Jika PPN inklusif dan kita PKP, kurangi porsi tax dari item agar tidak tercatat dobel
            actual_item_amount = amt
            if tax > 0 and is_tax_inclusive and is_pkp and raw_sum_items > 0:
                tax_portion = round(tax * (amt / raw_sum_items))
                actual_item_amount = amt - tax_portion

            aggregated_debits[account] = aggregated_debits.get(account, 0) + actual_item_amount

        # 2. PPN Masukan (Tax)
        if tax > 0:
            if is_pkp:
                aggregated_debits["135"] = aggregated_debits.get("135", 0) + tax
            else:
                # Non-PKP: Jika tax eksklusif, bebankan ke item perolehan
                if not is_tax_inclusive:
                    first_acc = next(iter(aggregated_debits.keys()), "790")
                    aggregated_debits[first_acc] += tax

        # 3. Ongkos Kirim Pembelian (Freight-in)
        if freight > 0:
            if "120" in aggregated_debits:
                aggregated_debits["120"] += freight
            elif "157" in aggregated_debits:
                aggregated_debits["157"] += freight
            else:
                # Jika bukan persediaan atau peralatan, kapitalisasi ke akun beban utama pada struk
                first_acc = next(iter(aggregated_debits.keys()), "631")
                aggregated_debits[first_acc] += freight

        debit_lines: List[Dict[str, Any]] = []
        for acc_code, amt in aggregated_debits.items():
            if amt > 0:
                debit_lines.append({
                    "account_code": acc_code,
                    "debit": amt,
                    "credit": 0,
                    "memo": f"{merchant} - {r_date}"
                })

        # 4. Sisi Kredit & Pemeriksaan Selisih Pembulatan (Akun 799)
        credit_account = self.determine_credit_account(payment_method or "cash")
        total_debit = sum(line["debit"] for line in debit_lines)
        diff = total - total_debit

        if diff != 0:
            # Selisih pembulatan wajar hanya toleransi s.d. Rp 1.000 (pecahan koin/kembalian tunai)
            if abs(diff) <= 1000:
                if diff > 0:
                    debit_lines.append({
                        "account_code": "799",
                        "debit": diff,
                        "credit": 0,
                        "memo": "Pembulatan struk belanja"
                    })
                else:
                    debit_lines.append({
                        "account_code": "799",
                        "debit": 0,
                        "credit": abs(diff),
                        "memo": "Pembulatan struk belanja"
                    })
            else:
                # Selisih > 1.000 BUKAN pembulatan, tapi kesalahan baca item/pajak AI!
                # WAJIB tandai needs_review, jangan disembunyikan diam-diam
                needs_review = True
                if diff > 0:
                    debit_lines.append({
                        "account_code": "790",
                        "debit": diff,
                        "credit": 0,
                        "memo": f"Selisih belum teralokasi: Rp {diff:,} (perlu review)"
                    })
                else:
                    credit_account_diff = {
                        "account_code": "790",
                        "debit": 0,
                        "credit": abs(diff),
                        "memo": f"Koreksi selisih debet lebih: Rp {abs(diff):,} (perlu review)"
                    }
                    debit_lines.append(credit_account_diff)

        credit_lines = [{
            "account_code": credit_account,
            "debit": 0,
            "credit": total,
            "memo": f"Pembayaran {payment_method} - {merchant}"
        }]

        all_lines = debit_lines + credit_lines
        return all_lines, needs_review

    def save_journal_draft(self, receipt_id: int) -> int:
        """Membuat journal entry berstatus 'draft' di database."""
        r = self.db.execute(
            "SELECT merchant, receipt_date, total, status FROM receipts WHERE id = ?", (receipt_id,)
        ).fetchone()
        if not r:
            raise ValueError(f"Struk ID {receipt_id} tidak valid.")

        merchant, r_date, total, r_status = r
        lines, needs_review = self.generate_journal_lines_from_receipt(receipt_id)

        # Cek jika draft untuk struk ini sudah ada
        existing_draft = self.db.execute(
            "SELECT id FROM journal_entries WHERE receipt_id = ? AND status = 'draft'", (receipt_id,)
        ).fetchone()
        if existing_draft:
            entry_id = existing_draft[0]
            self.db.execute("DELETE FROM journal_lines WHERE entry_id = ?", (entry_id,))
            cur = self.db.cursor()
        else:
            cur = self.db.cursor()
            cur.execute(
                """INSERT INTO journal_entries (entry_date, receipt_id, description, kind, status)
                   VALUES (?, ?, ?, 'normal', 'draft')""",
                (r_date, receipt_id, f"{merchant} - Struk #{receipt_id}")
            )
            entry_id = cur.lastrowid

        for l in lines:
            cur.execute(
                """INSERT INTO journal_lines (entry_id, account_code, debit, credit, memo)
                   VALUES (?, ?, ?, ?, ?)""",
                (entry_id, l["account_code"], l["debit"], l["credit"], l["memo"])
            )

        new_status = 'needs_review' if needs_review else 'ready'
        cur.execute("UPDATE receipts SET status = ? WHERE id = ?", (new_status, receipt_id))
        self.db.commit()
        return entry_id

    def post_journal_entry(self, entry_id: int) -> None:
        """Posting journal entry. Ditolak trigger database jika debit != kredit."""
        now = datetime.now().isoformat()
        self.db.execute(
            "UPDATE journal_entries SET status = 'posted', posted_at = ? WHERE id = ?",
            (now, entry_id)
        )
        row = self.db.execute("SELECT receipt_id FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
        if row and row[0]:
            self.db.execute("UPDATE receipts SET status = 'posted' WHERE id = ?", (row[0],))
        self.db.commit()

    def record_correction_rule(self, merchant_pattern: str, item_pattern: Optional[str], correct_account_code: str):
        """Menyimpan hasil review koreksi user ke classification_rules."""
        cur = self.db.cursor()
        merchant_clean = merchant_pattern.strip()
        item_clean = item_pattern.strip() if item_pattern else None

        existing = cur.execute(
            """SELECT id, hits FROM classification_rules 
               WHERE LOWER(merchant_pattern) = LOWER(?) AND 
                     ((item_pattern IS NULL AND ? IS NULL) OR LOWER(item_pattern) = LOWER(?))""",
            (merchant_clean, item_clean, item_clean)
        ).fetchone()
        if existing:
            cur.execute(
                "UPDATE classification_rules SET account_code = ?, hits = hits + 1 WHERE id = ?",
                (correct_account_code, existing[0])
            )
        else:
            cur.execute(
                """INSERT INTO classification_rules (merchant_pattern, item_pattern, account_code, hits)
                   VALUES (?, ?, ?, 1)""",
                (merchant_clean, item_clean, correct_account_code)
            )
        self.db.commit()
