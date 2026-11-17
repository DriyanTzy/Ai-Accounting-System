-- =====================================================================
-- Skema pembukuan dari struk belanja (usaha dagang, perpetual inventory)
-- Dialek: SQLite (mudah dipindah ke PostgreSQL; ganti trigger sesuai dialek)
-- Uang disimpan sebagai INTEGER (Rupiah, tanpa desimal). Jangan pakai FLOAT.
-- Acuan: Weygandt et al., Financial Accounting with IFRS 4e, Bab 2-5
-- =====================================================================
PRAGMA foreign_keys = ON;

-- ---------- Setelan usaha ----------
CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT INTO settings VALUES
  ('is_pkp', '0'),                       -- 1 = PPN Masukan dipisah (akun 135), 0 = masuk harga perolehan
  ('inventory_system', 'perpetual'),     -- Bab 5: perpetual
  ('review_threshold', '1000000'),       -- nominal di atas ini wajib review manual
  ('min_confidence', '0.85');            -- di bawah ini wajib review manual

-- ---------- Chart of accounts ----------
CREATE TABLE accounts (
  code           TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  type           TEXT NOT NULL CHECK (type IN
                   ('asset','liability','equity','revenue','cogs','expense','other_revenue','other_expense')),
  normal_balance TEXT NOT NULL CHECK (normal_balance IN ('debit','credit')),
  is_temporary   INTEGER NOT NULL DEFAULT 0,   -- 1 = ditutup saat closing tahunan (hal. 4-12)
  is_payment     INTEGER NOT NULL DEFAULT 0,   -- 1 = akun sumber pembayaran (kas/bank/utang)
  active         INTEGER NOT NULL DEFAULT 1
);

-- ---------- Periode ----------
CREATE TABLE periods (
  id     TEXT PRIMARY KEY,                     -- 'YYYY-MM'
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed'))
);

-- ---------- Struk ----------
CREATE TABLE receipts (
  id           INTEGER PRIMARY KEY,
  file_path    TEXT,
  merchant     TEXT,
  receipt_no   TEXT,
  receipt_date TEXT,                           -- ISO: YYYY-MM-DD
  subtotal     INTEGER,
  tax          INTEGER DEFAULT 0,
  freight      INTEGER DEFAULT 0,
  total        INTEGER,
  payment_method TEXT CHECK (payment_method IN ('cash','bank','credit_card','on_account')),
  fingerprint  TEXT UNIQUE,                    -- hash(merchant|tanggal|total|no.struk): cegah posting ganda (hal. 2-21)
  raw_extraction TEXT,                         -- JSON mentah dari AI/OCR
  status       TEXT NOT NULL DEFAULT 'extracted' CHECK (status IN
                 ('uploaded','extracted','classified','needs_review','ready','posted','not_a_transaction','voided')),
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE receipt_items (
  id              INTEGER PRIMARY KEY,
  receipt_id      INTEGER NOT NULL REFERENCES receipts(id),
  description     TEXT NOT NULL,
  qty             REAL DEFAULT 1,
  amount          INTEGER NOT NULL,            -- total baris item
  suggested_account TEXT REFERENCES accounts(code),  -- usulan AI
  confidence      REAL,
  final_account   TEXT REFERENCES accounts(code)     -- keputusan akhir (setelah review)
);

-- Koreksi user disimpan di sini supaya klasifikasi makin akurat
CREATE TABLE classification_rules (
  id           INTEGER PRIMARY KEY,
  merchant_pattern TEXT NOT NULL,
  item_pattern TEXT,
  account_code TEXT NOT NULL REFERENCES accounts(code),
  hits         INTEGER NOT NULL DEFAULT 1
);

-- ---------- Jurnal umum ----------
CREATE TABLE journal_entries (
  id          INTEGER PRIMARY KEY,
  entry_date  TEXT NOT NULL,
  receipt_id  INTEGER REFERENCES receipts(id),
  description TEXT,
  kind        TEXT NOT NULL DEFAULT 'normal' CHECK (kind IN
                ('normal','opening','adjusting','closing','correcting')),
  status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted')),
  corrects_entry_id INTEGER REFERENCES journal_entries(id),  -- jurnal koreksi menunjuk jurnal yang dikoreksi
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  posted_at   TEXT
);

CREATE TABLE journal_lines (
  id           INTEGER PRIMARY KEY,
  entry_id     INTEGER NOT NULL REFERENCES journal_entries(id),
  account_code TEXT NOT NULL REFERENCES accounts(code),
  debit        INTEGER NOT NULL DEFAULT 0 CHECK (debit  >= 0),
  credit       INTEGER NOT NULL DEFAULT 0 CHECK (credit >= 0),
  memo         TEXT,
  -- salah satu saja yang terisi
  CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))
);
CREATE INDEX idx_lines_account ON journal_lines(account_code);
CREATE INDEX idx_lines_entry   ON journal_lines(entry_id);

-- =====================================================================
-- Trigger: aturan akuntansi ditegakkan di level database
-- =====================================================================

-- 1) Jurnal tidak boleh diposting kalau tidak seimbang / kurang dari 2 baris (hal. 2-4)
CREATE TRIGGER trg_post_requires_balance
BEFORE UPDATE OF status ON journal_entries
WHEN NEW.status = 'posted' AND OLD.status = 'draft'
BEGIN
  SELECT RAISE(ABORT, 'jurnal minimal 2 baris')
   WHERE (SELECT COUNT(*) FROM journal_lines WHERE entry_id = NEW.id) < 2;
  SELECT RAISE(ABORT, 'debit != kredit')
   WHERE (SELECT SUM(debit) - SUM(credit) FROM journal_lines WHERE entry_id = NEW.id) <> 0;
  SELECT RAISE(ABORT, 'periode sudah ditutup')
   WHERE EXISTS (SELECT 1 FROM periods
                  WHERE id = substr(NEW.entry_date, 1, 7) AND status = 'closed');
END;

-- 2) Jurnal posted tidak boleh diubah atau dihapus: koreksi lewat jurnal koreksi (hal. 4-19)
CREATE TRIGGER trg_entry_immutable_update
BEFORE UPDATE ON journal_entries
WHEN OLD.status = 'posted'
BEGIN
  SELECT RAISE(ABORT, 'jurnal posted tidak boleh diubah; buat jurnal koreksi');
END;

CREATE TRIGGER trg_entry_immutable_delete
BEFORE DELETE ON journal_entries
WHEN OLD.status = 'posted'
BEGIN
  SELECT RAISE(ABORT, 'jurnal posted tidak boleh dihapus; buat jurnal koreksi');
END;

CREATE TRIGGER trg_lines_immutable_update
BEFORE UPDATE ON journal_lines
WHEN (SELECT status FROM journal_entries WHERE id = OLD.entry_id) = 'posted'
BEGIN
  SELECT RAISE(ABORT, 'baris jurnal posted tidak boleh diubah');
END;

CREATE TRIGGER trg_lines_immutable_delete
BEFORE DELETE ON journal_lines
WHEN (SELECT status FROM journal_entries WHERE id = OLD.entry_id) = 'posted'
BEGIN
  SELECT RAISE(ABORT, 'baris jurnal posted tidak boleh dihapus');
END;

CREATE TRIGGER trg_lines_no_insert_after_post
BEFORE INSERT ON journal_lines
WHEN (SELECT status FROM journal_entries WHERE id = NEW.entry_id) = 'posted'
BEGIN
  SELECT RAISE(ABORT, 'tidak bisa menambah baris ke jurnal posted');
END;

-- 3) Akun nonaktif tidak boleh dipakai
CREATE TRIGGER trg_lines_active_account
BEFORE INSERT ON journal_lines
WHEN (SELECT active FROM accounts WHERE code = NEW.account_code) = 0
BEGIN
  SELECT RAISE(ABORT, 'akun nonaktif');
END;

-- =====================================================================
-- View: buku besar dan neraca saldo (tidak ada tabel terpisah, selalu konsisten)
-- =====================================================================

-- Buku besar: Date, Explanation, Ref, Debit, Credit, Balance (format hal. 2-10)
CREATE VIEW v_ledger AS
SELECT
  l.account_code,
  a.name                         AS account_name,
  e.entry_date                   AS date,
  COALESCE(l.memo, e.description) AS explanation,
  'J' || e.id                    AS ref,
  l.debit,
  l.credit,
  SUM(CASE WHEN a.normal_balance = 'debit'
           THEN l.debit - l.credit
           ELSE l.credit - l.debit END)
    OVER (PARTITION BY l.account_code ORDER BY e.entry_date, e.id, l.id
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS balance
FROM journal_lines l
JOIN journal_entries e ON e.id = l.entry_id AND e.status = 'posted'
JOIN accounts a        ON a.code = l.account_code;

-- Neraca saldo: saldo tiap akun di kolom debit atau kredit sesuai posisinya (hal. 2-20)
CREATE VIEW v_trial_balance AS
SELECT
  a.code, a.name,
  CASE WHEN SUM(l.debit) - SUM(l.credit) > 0 THEN SUM(l.debit) - SUM(l.credit) ELSE 0 END AS debit,
  CASE WHEN SUM(l.credit) - SUM(l.debit) > 0 THEN SUM(l.credit) - SUM(l.debit) ELSE 0 END AS credit
FROM accounts a
JOIN journal_lines l   ON l.account_code = a.code
JOIN journal_entries e ON e.id = l.entry_id AND e.status = 'posted'
GROUP BY a.code, a.name
HAVING SUM(l.debit) <> SUM(l.credit);

-- Pemeriksaan: struk berstatus extracted/classified yang belum jadi jurnal (celah #1 hal. 2-21)
CREATE VIEW v_unrecorded_receipts AS
SELECT r.* FROM receipts r
WHERE r.status NOT IN ('posted','not_a_transaction','voided')
  AND NOT EXISTS (SELECT 1 FROM journal_entries e WHERE e.receipt_id = r.id AND e.status = 'posted');
