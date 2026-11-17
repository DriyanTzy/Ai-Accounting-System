-- Chart of accounts usaha dagang
-- Penomoran mengikuti buku (Illustration 2.19, hal. 2-13): 3 digit, dengan jeda untuk akun baru
--   101-199 aset | 200-299 kewajiban | 301-350 ekuitas | 400-499 pendapatan
--   500-599 HPP  | 601-799 beban     | 800-899 pendapatan lain | 900-999 beban lain
-- Kolom: code, name, type, normal_balance, is_temporary, is_payment

INSERT INTO accounts (code, name, type, normal_balance, is_temporary, is_payment) VALUES
-- Aset
('101','Kas',                              'asset','debit',0,1),
('102','Bank',                             'asset','debit',0,1),
('112','Piutang Usaha',                    'asset','debit',0,0),
('120','Persediaan Barang Dagang',         'asset','debit',0,0),   -- Inventory (Bab 5, hal. 5-7)
('126','Perlengkapan (Supplies)',          'asset','debit',0,0),
('130','Beban Dibayar di Muka',            'asset','debit',0,0),   -- Prepaid (jenis penyesuaian 1)
('135','PPN Masukan',                      'asset','debit',0,0),   -- dipakai hanya jika is_pkp = 1
('157','Peralatan',                        'asset','debit',0,0),
('158','Akumulasi Penyusutan - Peralatan', 'asset','credit',0,0),  -- akun kontra aset
-- Kewajiban
('201','Utang Usaha',                      'liability','credit',0,1),
('202','Utang Kartu Kredit',               'liability','credit',0,1),
('209','Pendapatan Diterima di Muka',      'liability','credit',0,0),
('212','Utang Gaji',                       'liability','credit',0,0),
('230','Utang Bunga',                      'liability','credit',0,0),
-- Ekuitas
('311','Modal Pemilik',                    'equity','credit',0,0),
('320','Laba Ditahan',                     'equity','credit',0,0),
('332','Prive / Dividen',                  'equity','debit', 1,0),
('350','Ikhtisar Laba Rugi',               'equity','credit',1,0), -- Income Summary, hanya untuk closing
-- Pendapatan (Bab 5)
('400','Penjualan',                        'revenue','credit',1,0),
('401','Retur dan Potongan Penjualan',     'revenue','debit', 1,0),
('402','Diskon Penjualan',                 'revenue','debit', 1,0),
-- HPP (perpetual: dihitung tiap penjualan)
('505','Harga Pokok Penjualan',            'cogs','debit',1,0),
-- Beban
('631','Beban Perlengkapan',               'expense','debit',1,0),
('632','Beban ATK',                        'expense','debit',1,0),
('633','Beban Kemasan',                    'expense','debit',1,0),
('711','Beban Penyusutan',                 'expense','debit',1,0),
('722','Beban Asuransi',                   'expense','debit',1,0),
('726','Beban Gaji',                       'expense','debit',1,0),
('729','Beban Sewa',                       'expense','debit',1,0),
('732','Beban Listrik, Air, Internet',     'expense','debit',1,0),
('735','Beban Transportasi',               'expense','debit',1,0),
('738','Beban Konsumsi',                   'expense','debit',1,0),
('741','Beban Ongkos Kirim Penjualan',     'expense','debit',1,0), -- Freight-out
('760','Beban Perbaikan dan Pemeliharaan', 'expense','debit',1,0),
('790','Beban Lain-lain',                  'expense','debit',1,0), -- penampung sementara, wajib direview
('799','Selisih Pembulatan',               'expense','debit',1,0),
-- Lain-lain
('905','Beban Bunga',                      'other_expense','debit',1,0);
