"""
Modul Pembaca Struk (OCR / Vision Model) -> JSON Terstruktur
Mendukung:
1. File .env (GEMINI_API_KEY=xxx)
2. Environment variable OS (GEMINI_API_KEY)
3. SDK resmi google-genai / google.generativeai
4. Direct REST API via urllib (Zero Dependency: bisa jalan langsung tanpa pip install tambahan)
"""

import os
import json
import base64
import re
import urllib.request
import urllib.error
from typing import Dict, Any, Optional
from datetime import datetime


def load_env_file():
    """Membaca file .env jika ada di direktori kerja."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v


# Otomatis muat .env saat modul di-import
load_env_file()

# Prompt sistem baku untuk model Vision AI
VISION_RECEIPT_PROMPT = """
Anda adalah AI Auditor & Ekstraktor Dokumen Akuntansi yang sangat teliti.
Tugas Anda adalah membaca gambar struk / nota / faktur belanja dan mengekstrak informasinya ke dalam format JSON yang presisi.

ATURAN EKSTRAKSI:
1. is_business_transaction: boolean. True jika struk transaksi jual/beli/operasional bisnis. False jika bukan transaksi (misal: karcis tol pribadi, foto sembarangan, kertas coretan).
2. merchant: nama toko / vendor / penyedia jasa (string).
3. receipt_no: nomor nota / invoice / nomor struk (string atau null jika tidak ada).
4. receipt_date: tanggal transaksi dalam format ISO 'YYYY-MM-DD'. Jika tahun tidak tertera, asumsikan tahun berjalan.
5. subtotal: nilai sebelum pajak/ongkir dalam integer Rupiah (tanpa desimal/titik).
6. tax: total PPN / pajak (integer Rupiah, 0 jika tidak ada).
7. freight: ongkos kirim / delivery fee jika ada (integer Rupiah, 0 jika tidak ada).
8. total: total akhir yang dibayarkan konsumen dalam integer Rupiah.
9. payment_method: pilih salah satu: 'cash' (tunai), 'bank' (debit, transfer, QRIS, BCA, Mandiri), 'credit_card' (kartu kredit), 'on_account' (tempo / kasbon / belum lunas). Default 'cash' jika tidak jelas.
10. items: daftar item belanjaan (array of objects):
    - description: nama produk / barang / jasa
    - qty: jumlah kuantitas (angka, default 1)
    - amount: total harga untuk baris item tersebut (integer Rupiah)
    - suggested_category: tebakan awal kategori ('persediaan', 'atk', 'kemasan', 'konsumsi', 'transportasi', 'peralatan', 'lainnya')

OUTPUT HANYA JSON MURNI TANPA MARKDOWN BACKTICKS ATAU PENJELASAN LAIN.
"""


def clean_json_text(text: str) -> str:
    """Bersihkan output AI dari markdown fence seperti ```json ... ```."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def call_gemini_rest_api(image_path: str, api_key: str) -> Dict[str, Any]:
    """
    Panggilan langsung ke Gemini REST API tanpa membutuhkan library eksternal.
    Menggunakan urllib bawaan Python.
    """
    # Deteksi mime type berdasarkan ekstensi
    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/jpeg"
    if ext == ".png":
        mime = "image/png"
    elif ext == ".webp":
        mime = "image/webp"

    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": VISION_RECEIPT_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": mime,
                            "data": img_b64
                        }
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "response_mime_type": "application/json"
        }
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            candidates = res_data.get("candidates", [])
            if not candidates:
                raise ValueError("Tidak ada respon dari Gemini API.")
            content_text = candidates[0]["content"]["parts"][0]["text"]
            return json.loads(clean_json_text(content_text))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        raise RuntimeError(f"Gagal memanggil Gemini API ({e.code}): {err_msg}")


def parse_receipt_with_gemini(image_path: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Mengirim gambar struk ke Gemini API.
    Mendukung SDK google-genai / google.generativeai, dan auto-fallback ke REST API jika SDK belum dipasang.
    """
    load_env_file()
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError(
            "GEMINI_API_KEY tidak ditemukan!\n"
            "Solusi:\n"
            "1. Buat file .env di folder project dengan isi: GEMINI_API_KEY=AIzaSy...\n"
            "   ATAU\n"
            "2. Jalankan di PowerShell: $env:GEMINI_API_KEY=\"AIzaSy...\""
        )

    # 1. Coba SDK resmi terbaru (google-genai)
    try:
        from google import genai
        client = genai.Client(api_key=key)
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=[
                VISION_RECEIPT_PROMPT,
                genai.types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
            ]
        )
        return json.loads(clean_json_text(response.text))
    except ImportError:
        pass

    # 2. Coba SDK legacy (google.generativeai)
    try:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=key)
        model = genai_legacy.GenerativeModel("gemini-1.5-flash")
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        cookie_picture = {'mime_type': 'image/jpeg', 'data': image_bytes}
        response = model.generate_content([VISION_RECEIPT_PROMPT, cookie_picture])
        return json.loads(clean_json_text(response.text))
    except ImportError:
        pass

    # 3. Fallback murni REST API (Zero external dependency)
    return call_gemini_rest_api(image_path, key)


def mock_receipt_parser(scenario: str = "atk") -> Dict[str, Any]:
    """Mock parser untuk pengujian lokal tanpa internet / API key."""
    scenarios = {
        "atk": {
            "is_business_transaction": True,
            "merchant": "Gramedia Store",
            "receipt_no": "GRM-88219",
            "receipt_date": datetime.now().strftime("%Y-%m-%d"),
            "subtotal": 150000,
            "tax": 16500,
            "freight": 0,
            "total": 166500,
            "payment_method": "bank",
            "items": [
                {"description": "Binder Note B5", "qty": 1, "amount": 65000, "suggested_category": "atk"},
                {"description": "Isi Binder Joyko 100lbr", "qty": 2, "amount": 40000, "suggested_category": "atk"},
                {"description": "Pulpen Pilot G2 0.5", "qty": 3, "amount": 45000, "suggested_category": "atk"}
            ]
        },
        "dagang": {
            "is_business_transaction": True,
            "merchant": "Distributor Audio Jaya",
            "receipt_no": "INV-DA-491",
            "receipt_date": datetime.now().strftime("%Y-%m-%d"),
            "subtotal": 2400000,
            "tax": 0,
            "freight": 50000,
            "total": 2450000,
            "payment_method": "on_account",
            "items": [
                {"description": "Headphone Studio Monitor X10", "qty": 4, "amount": 2400000, "suggested_category": "persediaan"}
            ]
        },
        "peralatan": {
            "is_business_transaction": True,
            "merchant": "Sentra Komputer",
            "receipt_no": "SK-9012",
            "receipt_date": datetime.now().strftime("%Y-%m-%d"),
            "subtotal": 4500000,
            "tax": 495000,
            "freight": 0,
            "total": 4995000,
            "payment_method": "credit_card",
            "items": [
                {"description": "Printer Epson L3210 EcoTank", "qty": 1, "amount": 4500000, "suggested_category": "peralatan"}
            ]
        }
    }
    return scenarios.get(scenario, scenarios["atk"])
