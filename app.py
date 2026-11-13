import base64
import mimetypes
import sqlite3
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets

import requests
import streamlit as st
from pathlib import Path

# Model AI dan URL OpenRouter
MODEL_VISION = "nvidia/nemotron-3.5-super-vl:free"
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# FUNGSI UNTUK PEMERIKSAAN TEKS DAN TAMPILAN SKOR.

# Memeriksa kutipan AI di jawaban siswa, dengan mengabaikan spasi
# dan perbedaan huruf besar-kecil.
def normalkan(teks):
    return re.sub(r"\s+", " ", str(teks)).strip().casefold()


# Menentukan warna dan label lencana berdasarkan skor.
def lencana_skor(skor):
    if skor >= 75:
        return "#16a34a", "Baik"
    if skor >= 50:
        return "#d97706", "Cukup"
    return "#dc2626", "Perlu dicek"

# FUNGSI UNTUK MENYIAPKAN, MENYIMPAN, DAN MEMBACA DATABASE.
# Fitur akun: membuat akun, memeriksa kata sandi, dan memisahkan data tiap pengguna.

# Menempatkan database di folder yang sama dengan file aplikasi
DB_FILE = Path(__file__).with_name("koreksi.db")

# Membuat tabel database jika belum tersedia
def siapkan_database():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS akun (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                dibuat_pada TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS koreksi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                nama_guru TEXT NOT NULL,
                mapel TEXT NOT NULL,
                nama_tugas TEXT NOT NULL,
                nama_siswa TEXT NOT NULL,
                nilai_akhir REAL NOT NULL,
                dibuat_pada TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES akun(id) ON DELETE SET NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS detail_koreksi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                koreksi_id INTEGER NOT NULL,
                nomor_soal INTEGER NOT NULL,
                teks_soal TEXT NOT NULL,
                acuan TEXT NOT NULL,
                jawaban TEXT NOT NULL,
                skor_ai REAL NOT NULL,
                bukti_ai TEXT,
                alasan_ai TEXT,
                rincian_ai TEXT,
                skor_guru REAL NOT NULL,
                FOREIGN KEY (koreksi_id) REFERENCES koreksi(id)
            )
        """)

        kolom = {baris[1] for baris in conn.execute("PRAGMA table_info(koreksi)")}
        if "user_id" not in kolom:
            conn.execute("ALTER TABLE koreksi ADD COLUMN user_id INTEGER REFERENCES akun(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_koreksi_user_id ON koreksi(user_id)")
        kolom_detail = {baris[1] for baris in conn.execute("PRAGMA table_info(detail_koreksi)")}
        if "rincian_ai" not in kolom_detail:
            conn.execute("ALTER TABLE detail_koreksi ADD COLUMN rincian_ai TEXT")


# Hash password agar password asli tidak tersimpan di database.
# Keamanan akun: simpan password sebagai hash PBKDF2 dengan salt acak, bukan teks asli.
def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    hasil = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    return salt.hex(), hasil.hex()


# Membuat akun baru menggunakan alamat email sebagai nama login.
def buat_akun(email, password):
    email = email.strip().casefold()
    salt, password_hash = hash_password(password)
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.execute(
                "INSERT INTO akun (email, salt, password_hash) VALUES (?, ?, ?)",
                (email, salt, password_hash),
            )
            return {"id": cursor.lastrowid, "email": email}, None
    except sqlite3.IntegrityError:
        return None, "Email tersebut sudah terdaftar."


# Memeriksa email dan password tanpa mengungkap mana yang salah.
# Mengecek apakah email sudah terdaftar untuk menampilkan pesan login yang sesuai.
def akun_terdaftar(email):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT 1 FROM akun WHERE email = ? COLLATE NOCASE",
            (email.strip().casefold(),),
        ).fetchone()
    return row is not None


def autentikasi(email, password):
    email = email.strip().casefold()
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT id, email, salt, password_hash FROM akun WHERE email = ? COLLATE NOCASE",
            (email,),
        ).fetchone()

    if row is None:
        return None

    salt = bytes.fromhex(row[2])
    _, hasil = hash_password(password, salt)
    if not hmac.compare_digest(hasil, row[3]):
        return None
    return {"id": row[0], "email": row[1]}


# Menyimpan satu lembar koreksi di bawah akun yang sedang login.
def simpan_koreksi(user_id, metadata, hasil, skor_final, nilai_akhir):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.execute(
            """
            INSERT INTO koreksi (user_id, nama_guru, mapel, nama_tugas, nama_siswa, nilai_akhir)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                metadata["nama_guru"],
                metadata["mapel"],
                metadata["nama_tugas"],
                metadata["nama_siswa"],
                nilai_akhir,
            ),
        )
        id_koreksi = cursor.lastrowid

        for item, skor in zip(hasil, skor_final):
            conn.execute(
                """
                INSERT INTO detail_koreksi (
                    koreksi_id, nomor_soal, teks_soal, acuan, jawaban,
                    skor_ai, bukti_ai, alasan_ai, rincian_ai, skor_guru
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    id_koreksi, item["nomor"], item["soal"], item["acuan"],
                    item["jawaban"], item["skor_ai"], item["bukti"],
                    item["alasan"], json.dumps(item.get("rincian", []), ensure_ascii=False), skor,
                ),
            )
    return id_koreksi


# Menampilkan hanya 20 hasil koreksi terakhir milik akun yang sedang login.
def ambil_riwayat(user_id):
    if user_id is None:
        return []
    with sqlite3.connect(DB_FILE) as conn:
        baris = conn.execute(
            """
            SELECT id, nama_guru, mapel, nama_tugas, nama_siswa, nilai_akhir, dibuat_pada
            FROM koreksi
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (user_id,),
        ).fetchall()

    return [
        {
            "ID": row[0], "Guru": row[1], "Mapel": row[2],
            "Tugas": row[3], "Siswa": row[4], "Nilai akhir": row[5],
            "Waktu disimpan": row[6],
        }
        for row in baris
    ]

# Menghitung nilai tertimbang dari komponen rubrik yang dikembalikan AI.
def hitung_skor_rubrik(rincian_ai, rubrik):
    if not isinstance(rincian_ai, list):
        raise ValueError("AI tidak mengembalikan rincian rubrik.")
    aturan = {item["nama"].casefold(): item["bobot"] for item in rubrik}
    nilai_per_kriteria = {}
    for item in rincian_ai:
        nama = str(item.get("kriteria", "")).strip()
        kunci = nama.casefold()
        if kunci not in aturan or kunci in nilai_per_kriteria:
            raise ValueError("Nama komponen rubrik dari AI tidak sesuai.")
        skor = float(item["skor"])
        if not 0 <= skor <= 100:
            raise ValueError("Nilai komponen rubrik harus 0–100.")
        nilai_per_kriteria[kunci] = {
            "kriteria": nama,
            "bobot": aturan[kunci],
            "skor": skor,
            "alasan": str(item.get("alasan", "")),
        }
    if set(nilai_per_kriteria) != set(aturan):
        raise ValueError("Rincian rubrik AI belum mencakup semua komponen.")
    rincian = list(nilai_per_kriteria.values())
    skor_akhir = sum(item["skor"] * item["bobot"] / 100 for item in rincian)
    return skor_akhir, rincian


# Mengubah foto menjadi data URL agar bisa dikirim ke model vision.
def gambar_ke_data_url(gambar):
    tipe_gambar = gambar.type or ""

    # Jika browser tidak memberi tipe MIME, tebak dari ekstensi file.
    if tipe_gambar not in {"image/jpeg", "image/png", "image/webp"}:
        tipe_tebakan, _ = mimetypes.guess_type(gambar.name)
        tipe_gambar = tipe_tebakan or tipe_gambar

    if tipe_gambar not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("Gunakan foto berformat JPG, PNG, atau WebP.")

    if gambar.size > 5 * 1024 * 1024:
        raise ValueError("Ukuran foto maksimal 5 MB.")

    data_base64 = base64.b64encode(gambar.getvalue()).decode("utf-8")
    return f"data:{tipe_gambar};base64,{data_base64}"

# Mengirim permintaan ke OpenRouter dan mengambil teks jawaban model.
def minta_openrouter(api_key, model, konten):
    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": konten}],
            "temperature": 0.1,
            "max_tokens": 4096,
            "reasoning_effort": "none",
        },
        timeout=90,
    )

    if not response.ok:
        raise ValueError(
            f"OpenRouter memberi respons {response.status_code}: "
            f"{response.text[:500]}"
        )

    data = response.json()
    pilihan = data.get("choices") or []
    if not pilihan:
        raise ValueError("Model tidak mengembalikan jawaban.")

    isi = (pilihan[0].get("message") or {}).get("content")
    if not isinstance(isi, str) or not isi.strip():
        raise ValueError("Model mengembalikan jawaban kosong.")

    return re.sub(r"^```(?:json)?\s*|\s*```$", "", isi.strip())

st.set_page_config(
    page_title="Ruang Bantu Koreksi",
    page_icon="📝",
    layout="wide",
)

# Memastikan tabel database siap sebelum aplikasi digunakan
siapkan_database()

# Mengikuti tema Light/Dark yang dipilih dari menu bawaan Streamlit.
try:
    tema_aplikasi = st.context.theme.type
except (AttributeError, RuntimeError):
    tema_aplikasi = "dark"

if tema_aplikasi not in {"light", "dark"}:
    tema_aplikasi = "dark"

# Fitur tampilan: tema ungu, kartu, lencana skor, dan header responsif.
st.markdown(
    """
<style>
:root { --accent:#6256e8; --border: 3px solid rgba(230, 234, 241, 0.27); }
.block-container { max-width:1480px; padding:1.2rem 2rem 4rem; }
div[data-testid="stSegmentedControl"] {
    border-radius: 999px;
    padding: .2rem;
}
div[data-testid="stSegmentedControl"] button {
    border-radius: 999px !important;
    font-weight: 700;
}
div[data-testid="stSelectbox"] > div {
    border-radius: 12px;
}
.about-title { text-align:center; font-size:clamp(2.7rem,7vw,5rem); font-style:italic; font-weight:900; letter-spacing:.04em; margin:1rem 0 3rem; color:#f5eaff; }
.st-key-nav_beranda button, .st-key-nav_masuk button, .st-key-nav_daftar button { background:transparent !important; border:0 !important; box-shadow:none !important; font-weight:700; }
.st-key-nav_beranda button:hover, .st-key-nav_masuk button:hover { color:#d9b5ff !important; }
.st-key-nav_daftar button { border:1px solid rgba(255,255,255,.55) !important; border-radius:999px !important; }
div[data-testid="stPopover"] > button { background:transparent !important; border:0 !important; box-shadow:none !important; font-weight:700; }
div[data-testid="stPopoverBody"] { background:rgba(48,31,76,.92) !important; backdrop-filter:blur(16px); border:1px solid rgba(255,255,255,.2); border-radius:16px; }
.hero { position:relative; overflow:hidden; background:linear-gradient(130deg,#4c1d95 0%,#6d35c9 55%,#8b5cf6 100%); color:#fff; padding:2rem 2.2rem; border-radius:22px; margin-bottom:1.5rem; box-shadow:0 16px 38px rgba(91,33,182,.28); }
.hero:after { content:""; position:absolute; width:210px; height:210px; right:-55px; top:-95px; border: 3px solid rgba(230, 234, 241, 0.27); border-radius:50%; box-shadow:0 0 0 24px rgba(255,255,255,.05),0 0 0 48px rgba(255,255,255,.035); }
.hero h1 { color:#fff; margin:0; font-size:clamp(1.6rem,3vw,2.15rem); padding:0; letter-spacing:-.035em; position:relative; z-index:1; }
.hero p { color:#ede9fe; margin:.55rem 0 0; max-width:650px; font-size:1rem; line-height:1.6; position:relative; z-index:1; }
.hero-kicker { display:inline-block; margin-bottom:.7rem; padding:.28rem .65rem; border: 3px solid rgba(230, 234, 241, 0.27); border-radius:999px; color:#f5f3ff; font-size:.72rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }
.badge { display:inline-block; color:#fff; font-weight:700; padding:.3rem .8rem; border-radius:999px; font-size:.86rem; }
.skor-besar { font-size:2.2rem; font-weight:800; line-height:1; }
.chip-ok { background:rgba(34,197,94,.13); color:#15803d; padding:.65rem .85rem; border: 3px solid rgba(230, 234, 241, 0.27); border-radius:12px; font-size:.92rem; }
.chip-warn { background:rgba(245,158,11,.13); color:#b45309; padding:.65rem .85rem; border: 3px solid rgba(230, 234, 241, 0.27); border-radius:12px; font-size:.92rem; }
.alasan { background:rgba(127,127,127,.08); border: 3px solid rgba(230, 234, 241, 0.27); padding:.85rem 1rem; border-radius:12px; font-size:.94rem; line-height:1.6; }

div[data-testid="stVerticalBlockBorderWrapper"] {
    border: 3px solid rgba(230, 234, 241, 0.27) !important;
    border-radius: 16px !important;
}

[data-testid="stTextArea"] textarea, [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input { background:rgba(48,31,76,1) !important; color:#f4f7fb !important; border: 3px solid rgba(230, 234, 241, 0.27) !important; border-radius:11px; }
[data-testid="stTextArea"] textarea:focus, [data-testid="stTextInput"] input:focus, [data-testid="stNumberInput"] input:focus { border-color:#c9a9ff !important; box-shadow:0 0 0 2px rgba(192,156,255,.2) !important; }
[data-testid="stTextArea"] textarea::placeholder, [data-testid="stTextInput"] input::placeholder { color:rgba(238,225,255,.58) !important; }
[data-testid="stFileUploader"] section { background:rgba(0, 0, 0, 0.55); border: 3px solid rgba(230, 234, 241, 0.27); border-radius:13px; }
[data-testid="stFileUploader"] section, [data-testid="stFileUploader"] section * { color:#f3eaff; }
[data-testid="stCameraInput"] button { background:rgba(69,43,105,.86); color:#f4f7fb; border: 3px solid rgba(230, 234, 241, 0.27); border-radius:10px; }
.stButton>button, .stFormSubmitButton>button { min-height:2.8rem; border-radius:12px; font-weight:650; padding:.6rem 1.05rem; transition:transform .15s ease, box-shadow .15s ease; }
.stButton>button:hover, .stFormSubmitButton>button:hover { transform:translateY(-1px); box-shadow:0 6px 16px rgba(139,92,246,.22); }
[data-testid="stSidebar"] { border: 3px solid rgba(230, 234, 241, 0.27); }
[data-testid="stAlert"] { border-radius:12px; }

hr, [data-testid="stDivider"] hr, [data-testid="stDivider"] {
    background-color: rgba(255, 255, 255, 0.2) !important;
    border: none !important;
    height: 3px !important;
    border-radius: 2px !important;
}

@media(max-width:700px) { .block-container { padding:1.2rem .8rem 3rem; } .hero { padding:1.45rem 1.25rem; border-radius:17px; } .hero p {font-size:.92rem;} }
</style>
""",
    unsafe_allow_html=True,
)

# Pilih wallpaper sesuai tema dari folder "bahan_gambar".
FOLDER_GAMBAR_LATAR = Path(__file__).with_name("bahan_gambar")
NAMA_WALLPAPER = (
    "background_terang.jpg" if tema_aplikasi == "light" else "background.jpg"
)
GAMBAR_LATAR = FOLDER_GAMBAR_LATAR / NAMA_WALLPAPER
if not GAMBAR_LATAR.is_file():
    GAMBAR_LATAR = FOLDER_GAMBAR_LATAR / "background.jpg"
if not GAMBAR_LATAR.is_file():
    GAMBAR_LATAR = next(
        (file for file in sorted(FOLDER_GAMBAR_LATAR.glob("*"))
         if file.is_file() and file.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}),
        None,
    )
if GAMBAR_LATAR is not None:
    tipe_gambar = mimetypes.guess_type(GAMBAR_LATAR.name)[0] or "image/jpeg"
    gambar_base64 = base64.b64encode(GAMBAR_LATAR.read_bytes()).decode("ascii")
    FILE_PARTIKEL = FOLDER_GAMBAR_LATAR / "partikel.png"
    if FILE_PARTIKEL.is_file():
        tipe_partikel = mimetypes.guess_type(FILE_PARTIKEL.name)[0] or "image/png"
        partikel_base64 = base64.b64encode(FILE_PARTIKEL.read_bytes()).decode("ascii")
        css_partikel = f"""
        .stApp {{ position: relative; isolation: isolate; }}
        .stApp::before, .stApp::after {{
            content: "";
            position: fixed;
            inset: 0;
            z-index: 0;
            pointer-events: none;
            background-image: url("data:{tipe_partikel};base64,{partikel_base64}"),
                              url("data:{tipe_partikel};base64,{partikel_base64}"),
                              url("data:{tipe_partikel};base64,{partikel_base64}");
            background-repeat: no-repeat, no-repeat, no-repeat;
            will-change: opacity, background-position;
        }}
        .stApp::before {{
            opacity: .02;
            background-size: 126px 126px, 88px 88px, 104px 104px;
            background-position: 12% 22%, 27% 73%, 68% 18%;
            animation: partikel-melayang 3s ease-in-out 0s infinite,
                       partikel-bergerak 12s ease-in-out infinite,
                       partikel-gelombang 8s ease-in-out infinite;
        }}
        .stApp::after {{
            opacity: .02;
            background-size: 112px 112px, 92px 92px, 78px 78px;
            background-position: 88% 76%, 74% 36%, 42% 55%;
            animation: partikel-melayang-dua 3s ease-in-out -1.5s infinite,
                       partikel-bergerak-dua 15s ease-in-out infinite,
                       partikel-gelombang-dua 10s ease-in-out -4s infinite;
        }}
        .stApp > header, .stApp > div {{ position: relative; z-index: 1; }}
        @keyframes partikel-melayang {{
            0%, 100% {{ opacity: 0; }}
            50% {{ opacity: 1; }}
        }}
        @keyframes partikel-melayang-dua {{
            0%, 100% {{ opacity: 0; }}
            50% {{ opacity: 1; }}
        }}
        @keyframes partikel-bergerak {{
            0%, 100% {{ background-position: 12% 22%, 27% 73%, 68% 18%; }}
            25% {{ background-position: 15% 18%, 30% 69%, 71% 22%; }}
            50% {{ background-position: 17% 23%, 32% 77%, 72% 16%; }}
            75% {{ background-position: 14% 27%, 29% 75%, 69% 14%; }}
        }}
        @keyframes partikel-bergerak-dua {{
            0%, 100% {{ background-position: 88% 76%, 74% 36%, 42% 55%; }}
            25% {{ background-position: 85% 72%, 71% 32%, 39% 59%; }}
            50% {{ background-position: 82% 77%, 68% 39%, 36% 53%; }}
            75% {{ background-position: 86% 80%, 72% 42%, 40% 50%; }}
        }}
        @keyframes partikel-gelombang {{
            0%, 100% {{ transform: translate3d(0, 0, 0); }}
            25% {{ transform: translate3d(12px, -16px, 0); }}
            50% {{ transform: translate3d(22px, 3px, 0); }}
            75% {{ transform: translate3d(10px, 17px, 0); }}
        }}
        @keyframes partikel-gelombang-dua {{
            0%, 100% {{ transform: translate3d(0, 0, 0); }}
            25% {{ transform: translate3d(-14px, 13px, 0); }}
            50% {{ transform: translate3d(-24px, -4px, 0); }}
            75% {{ transform: translate3d(-11px, -17px, 0); }}
        }}
        """
    else:
        css_partikel = ""
    if tema_aplikasi == "light":
        lapisan_awal = "rgba(255, 255, 255, .20)"
        lapisan_akhir = "rgba(255, 255, 255, .28)"
    else:
        lapisan_awal = "rgba(34, 15, 57, .48)"
        lapisan_akhir = "rgba(34, 15, 57, .64)"
    st.markdown(
        f"""
        <style>
        .stApp {{
            background:
                linear-gradient({lapisan_awal}, {lapisan_akhir}),
                url("data:{tipe_gambar};base64,{gambar_base64}") center / cover fixed no-repeat;
        }}
        {css_partikel}
        .stApp,
        [data-testid="stAppViewContainer"] {{
            background-color: transparent;
            filter: none !important;
            backdrop-filter: none !important;
            -webkit-backdrop-filter: none !important;
        }}
        div[data-testid="stVerticalBlockBorderWrapper"] {{
            border: 3px solid rgba(230, 234, 241, 0.27) !important;
            border-radius: 16px !important;
        }}
        [data-testid="stTextArea"] textarea,
        [data-testid="stTextInput"] input,
        [data-testid="stNumberInput"] input {{
            background: rgba(48, 31, 76, 1) !important;
            backdrop-filter: none !important;
            -webkit-backdrop-filter: none !important;
        }}
        [data-testid="stSidebar"] > div:first-child {{
            background: rgba(31, 19, 49, .94);
        }}
        [data-testid="stHeader"] {{ background: rgba(0, 0, 0, 0); }}
        hr, [data-testid="stDivider"] hr, [data-testid="stDivider"] {{
            background-color: rgba(255, 255, 255, 0.2) !important;
            border: none !important;
            height: 3px !important;
            border-radius: 2px !important;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )

if tema_aplikasi == "light":
    st.markdown(
        """
        <style>
        .stApp, [data-testid="stAppViewContainer"] { color: #392447; }
        [data-testid="stSidebar"] {
            background: rgba(246, 228, 250, .97) !important;
        }
        [data-testid="stSidebar"] > div:first-child {
            background: rgba(246, 228, 250, .97) !important;
        }
        [data-testid="stSidebar"] * {
            color: #392447 !important;
        }
        .about-title { color: #5b2678 !important; }
        div[data-testid="stPopoverBody"] { background:rgba(248,231,255,.96) !important; border-color:rgba(112,68,139,.28) !important; }
        div[data-testid="stPopoverBody"] * { color:#392447 !important; }
        div[data-testid="stVerticalBlockBorderWrapper"] {
            background: rgba(247, 229, 252, .76) !important;
            border-color: rgba(130, 88, 151, .34) !important;
        }
        [data-testid="stTextArea"] textarea,
        [data-testid="stTextInput"] input,
        [data-testid="stNumberInput"] input {
            background: rgba(255, 240, 249, .98) !important;
            color: #392447 !important;
            border-color: #8b55b5 !important;
        }
        [data-testid="stNumberInput"] button {
            background: rgba(241, 222, 248, .98) !important;
            color: #392447 !important;
            border-color: rgba(141, 101, 160, .36) !important;
        }
        [data-testid="stTextArea"] textarea::placeholder,
        [data-testid="stTextInput"] input::placeholder {
            color: rgba(73, 49, 86, .58) !important;
        }
        [data-testid="stFileUploader"] section {
            background: rgba(247, 232, 253, .86) !important;
            border-color: rgba(141, 101, 160, .40) !important;
        }
        [data-testid="stFileUploader"] section,
        [data-testid="stFileUploader"] section * {
            color: #392447 !important;
        }
        [data-testid="stCameraInput"] button {
            background: rgba(247, 231, 252, .96) !important;
            color: #392447 !important;
            border-color: rgba(141, 101, 160, .40) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

# Fitur login/daftar: pengguna boleh mencoba koreksi, lalu masuk untuk menyimpan hasil.
st.session_state.setdefault("auth_mode", None)
st.session_state.setdefault("nav_primary", "Beranda")
st.session_state.setdefault("nav_asisten", "Asisten Koreksi Ujian")
akun_aktif = st.session_state.get("akun_aktif")

# Navigasi atas: logo, Beranda, dropdown Asisten Guru, dan tombol akun.
file_logo = Path(__file__).with_name("bahan_gambar") / "logo.png"
nav_primary = st.session_state.get("nav_primary", "Beranda")
halaman_asisten = st.session_state.get("nav_asisten", "Asisten Koreksi Ujian")
akun_aktif = st.session_state.get("akun_aktif")

kol_logo, kol_spasi_kiri, kol_beranda, kol_asisten, kol_spasi_kanan, kol_akun = st.columns([1.2, 2.2, 1.2, 1.8, 2.2, 2.2])
with kol_logo:
    if file_logo.is_file():
        st.image(str(file_logo), width=68)
    else:
        st.markdown("## 📝")

with kol_beranda:
    if st.button("Beranda", key="nav_beranda", width="stretch"):
        st.session_state["nav_primary"] = "Beranda"
        st.rerun()

with kol_asisten:
    with st.popover("Asisten Guru"):
        if st.button("Asisten Koreksi Ujian", key="nav_assistant_exam", width="stretch"):
            st.session_state["nav_primary"] = "Asisten Guru"
            st.session_state["nav_asisten"] = "Asisten Koreksi Ujian"
            st.rerun()
        if st.button("History / Riwayat", key="nav_assistant_history", width="stretch"):
            st.session_state["nav_primary"] = "Asisten Guru"
            st.session_state["nav_asisten"] = "History / Riwayat"
            st.rerun()

with kol_akun:
    if akun_aktif:
        if st.button("Keluar", key="nav_keluar", width="stretch"):
            st.session_state.pop("akun_aktif", None)
            st.session_state.pop("hasil_penilaian_batch", None)
            st.session_state.pop("siswa_tersimpan", None)
            st.session_state.pop("metadata_koreksi", None)
            st.session_state.pop("hasil_telah_disimpan", None)
            st.rerun()
    else:
        tombol_masuk, tombol_daftar = st.columns(2)
        with tombol_masuk:
            if st.button("Masuk", key="nav_masuk", width="stretch"):
                st.session_state["auth_mode"] = "masuk"
        with tombol_daftar:
            if st.button("Buat akun", key="nav_daftar", width="stretch"):
                st.session_state["auth_mode"] = "daftar"

st.divider()

if not akun_aktif and st.session_state.get("auth_mode"):
    with st.container(border=True):
        mode_akun = st.session_state["auth_mode"]
        st.subheader("Masuk ke akun" if mode_akun == "masuk" else "Buat akun baru")
        if mode_akun == "masuk":
            with st.form("form_masuk_akun"):
                email_masuk = st.text_input("Email", key="email_masuk")
                sandi_masuk = st.text_input("Kata sandi", type="password", key="sandi_masuk")
                kirim_masuk = st.form_submit_button("Masuk", type="primary")
            if kirim_masuk:
                akun = autentikasi(email_masuk, sandi_masuk)
                if akun:
                    st.session_state["akun_aktif"] = akun
                    st.session_state["auth_mode"] = None
                    st.success("Berhasil masuk.")
                    st.rerun()
                else:
                    if akun_terdaftar(email_masuk):
                        st.error("Kata sandi salah. Coba periksa lagi.")
                    else:
                        st.error("Akun tidak ditemukan, buat akun terlebih dahulu.")
        else:
            with st.form("form_daftar_akun"):
                email_baru = st.text_input("Email", key="email_baru")
                sandi_baru = st.text_input("Kata sandi (minimal 8 karakter)", type="password", key="sandi_baru")
                konfirmasi_sandi = st.text_input("Ulangi kata sandi", type="password", key="konfirmasi_sandi")
                kirim_daftar = st.form_submit_button("Buat akun", type="primary")
            if kirim_daftar:
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email_baru.strip()):
                    st.error("Masukkan alamat email yang valid.")
                elif len(sandi_baru) < 8:
                    st.error("Kata sandi harus minimal 8 karakter.")
                elif sandi_baru != konfirmasi_sandi:
                    st.error("Konfirmasi kata sandi belum sama.")
                else:
                    akun, pesan = buat_akun(email_baru, sandi_baru)
                    if akun:
                        st.session_state["akun_aktif"] = akun
                        st.session_state["auth_mode"] = None
                        st.success("Akun berhasil dibuat dan kamu sudah masuk.")
                        st.rerun()
                    else:
                        st.error(pesan)
        if st.button("Tutup", key="tutup_auth"):
            st.session_state["auth_mode"] = None
            st.rerun()

# Beranda: informasi aplikasi dengan logo dan tombol Coba.
if nav_primary == "Beranda":
    st.markdown('<h1 class="about-title">TENTANG</h1>', unsafe_allow_html=True)
    kol_about_sisi_kiri, kol_about_kartu, kol_about_sisi_kanan = st.columns([0.6, 8.8, 0.6])
    with kol_about_kartu:
        with st.container(border=True):
            kol_logo_about, kol_tentang = st.columns([1, 3])
            with kol_logo_about:
                if file_logo.is_file():
                    st.image(str(file_logo), width=220)
                else:
                    st.markdown("## 📝")
            with kol_tentang:
                st.markdown(
                    "**RUANG BANTU KOREKSI** adalah sebuah aplikasi berbasis **Python** "
                    "yang dikembangkan oleh **Tim Triboma** untuk berpartisipasi dalam "
                    "kompetisi **Logicodix UNESA**. Web ini dirancang khusus sebagai "
                    "solusi cerdas untuk mempermudah para guru dalam proses penilaian, "
                    "mulai dari menaruh nilai, mengelola rubrik berbobot, hingga "
                    "mengecek serta meninjau jawaban uraian siswa secara mudah, cepat, "
                    "dan terstruktur."
                )
    kol_kosong_kiri, kol_coba, kol_kosong_kanan = st.columns([1, 1, 1])
    with kol_coba:
        if st.button("Coba", type="primary", width="stretch", key="mulai_dari_beranda"):
            st.session_state["nav_primary"] = "Asisten Guru"
            st.session_state["nav_asisten"] = "Asisten Koreksi Ujian"
            st.rerun()
    st.stop()

if halaman_asisten == "History / Riwayat":
    st.title("History / Riwayat")
    if akun_aktif:
        riwayat = ambil_riwayat(akun_aktif["id"])
        if riwayat:
            st.dataframe(riwayat, width="stretch")
        else:
            st.caption("Belum ada hasil tersimpan di akun ini.")
    else:
        st.info("Masuk atau buat akun untuk melihat riwayat hasil yang tersimpan.")
    st.stop()

st.title("Ruang Bantu Koreksi — Koreksi Ujian")

# Panel samping: pengaturan dan petunjuk singkat.
with st.sidebar:
    st.header("Pengaturan")
    jumlah_soal = st.number_input(
        "Jumlah soal",
        min_value=1,
        max_value=10,
        value=2,
        step=1,
    )

    jumlah_siswa = st.number_input(
        "Jumlah siswa yang akan diproses",
        min_value=1,
        max_value=36,
        value=1,
        step=1,
    )

    st.divider()
    # Fitur rubrik kustom: guru mengatur nama komponen dan bobot penilaiannya.
    st.subheader("Rubrik berbobot")
    st.caption("Atur komponen dan bobotnya. Jumlah bobot aktif harus 100%.")
    nama_default = ["Pemahaman konsep", "Ketepatan isi", "Logika dan kelengkapan", "Tata bahasa dan kejelasan"]
    bobot_default = [40, 30, 20, 10]
    rubrik_aktif = []
    rubrik_nama_semua = []
    rubrik_invalid = False
    for idx in range(4):
        kol_nama, kol_bobot = st.columns([2, 1])
        with kol_nama:
            nama_kriteria = st.text_input(
                f"Komponen {idx + 1}", value=nama_default[idx], key=f"rubrik_nama_{idx + 1}"
            ).strip()
        with kol_bobot:
            bobot_kriteria = st.number_input(
                "Bobot %", min_value=0, max_value=100, value=bobot_default[idx],
                step=5, key=f"rubrik_bobot_{idx + 1}"
            )
        if bobot_kriteria > 0:
            if not nama_kriteria:
                rubrik_invalid = True
            rubrik_aktif.append({"nama": nama_kriteria, "bobot": int(bobot_kriteria)})
            rubrik_nama_semua.append(nama_kriteria.casefold())
    total_bobot_rubrik = sum(item["bobot"] for item in rubrik_aktif)
    rubrik_valid = (
        bool(rubrik_aktif)
        and total_bobot_rubrik == 100
        and not rubrik_invalid
        and len(rubrik_nama_semua) == len(set(rubrik_nama_semua))
    )
    if not rubrik_valid:
        st.warning(f"Bobot rubrik harus berjumlah 100% dan nama komponen tidak boleh kosong atau duplikat (sekarang {total_bobot_rubrik}%).")
    st.divider()
    st.markdown(
        "**Cara pakai**\n"
        "1. Silahkan isi soal yang anda ujikan, kunci jawaban/rubrik penilaian, dan jawaban siswa\n"
        "2. Klik *Minta saran penilaian*\n"
        "3. Anda bisa mengecek terlebih dahulu dengan minjau skor hasil koreksi jika kurang sesuai\n"
        "4. Jika sudah, klik setujui tiap soal untuk nilai akhir"
    )
    st.caption(f"Model: {MODEL}")

# Menyimpan soal dan acuan yang dibuat satu kali untuk seluruh siswa.
daftar_soal = []

st.subheader("1. Masukkan soal dan acuan")

# Informasi tugas yang berlaku untuk seluruh siswa.
nama_guru = st.text_input("Nama guru", key="nama_guru")
mapel = st.text_input("Mata pelajaran", key="mapel")
nama_tugas = st.text_input("Nama tugas atau ulangan", key="nama_tugas")

# Guru mengisi setiap soal dan acuannya satu kali.
for nomor in range(1, int(jumlah_soal) + 1):
    with st.container(border=True):
        st.markdown(f"**Soal {nomor}**")

        kol_soal, kol_acuan = st.columns(2)

        with kol_soal:
            soal = st.text_area(
                f"Teks soal {nomor}",
                key=f"soal_{nomor}",
                height=130,
            )

        with kol_acuan:
            acuan = st.text_area(
                f"Kunci jawaban atau acuan soal {nomor}",
                key=f"acuan_{nomor}",
                height=130,
                help="Isi jawaban acuan atau informasi yang membantu penilaian.",
            )

        daftar_soal.append(
            {
                "nomor": nomor,
                "soal": soal,
                "acuan": acuan,
            }
        )

st.divider()
st.subheader("2. Masukkan foto lembar jawaban siswa")
st.caption(
    "Setiap siswa dapat memiliki 1–3 foto. Urutkan foto sesuai urutan "
    "lembar jawaban."
)

# Menyimpan nama opsional dan 1–3 foto untuk setiap siswa.
daftar_siswa = []

for nomor_siswa in range(1, int(jumlah_siswa) + 1):
    with st.container(border=True):
        st.markdown(f"### Siswa {nomor_siswa}")

        nama_manual = st.text_input(
            "Nama atau kode siswa (opsional, untuk membantu verifikasi)",
            key=f"nama_manual_{nomor_siswa}",
        )

        foto_siswa = []

        # Tiga slot foto per siswa. Setiap slot dapat memakai upload atau kamera.
        for nomor_lembar in range(1, 4):
            with st.expander(
                f"Foto lembar {nomor_lembar}",
                expanded=(nomor_lembar == 1),
            ):
                cara_foto = st.segmented_control(
                    f"Cara memasukkan foto lembar {nomor_lembar}, siswa {nomor_siswa}",
                    options=["Upload", "Kamera"],
                    selection_mode="single",
                    default="Upload",
                    required=True,
                    key=f"cara_foto_{nomor_siswa}_{nomor_lembar}",
                    width="stretch",
                )

                gambar = None

                if cara_foto == "Upload":
                    gambar = st.file_uploader(
                        f"Pilih foto lembar {nomor_lembar}",
                        type=["jpg", "jpeg", "png", "webp"],
                        key=f"upload_siswa_{nomor_siswa}_{nomor_lembar}",
                    )

                elif cara_foto == "Kamera":
                    gambar = st.camera_input(
                        f"Ambil foto lembar {nomor_lembar}",
                        key=f"kamera_siswa_{nomor_siswa}_{nomor_lembar}",
                        resolution="720p",
                    )

                if gambar is not None:
                    st.image(
                        gambar,
                        caption=f"Siswa {nomor_siswa}, lembar {nomor_lembar}",
                        width=360,
                    )
                    foto_siswa.append(
                        {
                            "nomor_lembar": nomor_lembar,
                            "gambar": gambar,
                        }
                    )

        daftar_siswa.append(
            {
                "urutan": nomor_siswa,
                "nama_manual": nama_manual.strip(),
                "foto": foto_siswa,
            }
        )

kirim = st.button(
    "✨ Minta saran penilaian",
    type="primary",
    width="stretch",
    key="kirim_koreksi",
)


# Memeriksa kelengkapan data dan mengirimkannya ke AI setelah tombol diklik.
if kirim:
    st.session_state.pop("siswa_tersimpan", None)
    st.session_state.pop("hasil_penilaian_batch", None)

    # Menghapus status simpan lama saat koreksi baru diminta
    st.session_state.pop("metadata_koreksi", None)
    st.session_state.pop("hasil_telah_disimpan", None)

    # Memeriksa identitas tugas dan kelengkapan soal serta foto setiap siswa.
    ada_yang_kosong = (
        not nama_guru.strip()
        or not mapel.strip()
        or not nama_tugas.strip()
        or any(
            not soal["soal"].strip() or not soal["acuan"].strip()
            for soal in daftar_soal
        )
        or not daftar_siswa
        or any(
            len(siswa["foto"]) < 1
            or len(siswa["foto"]) > 3
            or any(
                foto["gambar"].size > 5 * 1024 * 1024
                for foto in siswa["foto"]
            )
            or sum(
                foto["gambar"].size
                for foto in siswa["foto"]
            ) > 10 * 1024 * 1024
            for siswa in daftar_siswa
        )
    )

    if not rubrik_valid:
        st.warning("Perbaiki rubrik di Pengaturan; total bobot harus 100%.")
    elif ada_yang_kosong:
        st.warning(
            "Lengkapi identitas tugas dan semua soal beserta acuannya. "
            "Setiap siswa harus memiliki 1–3 foto; maksimal 5 MB per foto "
            "dan 10 MB untuk seluruh foto milik satu siswa."
        )
    else:
        # Membaca API key dari Codespaces secret.
        api_key = os.getenv("OPENROUTER_API_KEY")

        if not api_key:
            st.error("API key belum terbaca. Periksa Codespaces secret.")
        else:
            # Membaca nama dan jawaban dari 1–3 foto milik setiap siswa.
            hasil_ocr_semua_siswa = []
            nomor_soal_valid = {
                soal["nomor"] for soal in daftar_soal
            }

            # PROMPT OCR (versi baru): transkripsi apa adanya + penanda keraguan.
            instruksi_ocr = (
                "Kamu adalah transkriber tulisan tangan siswa untuk pelajaran matematika. "
                "Tugasmu HANYA menyalin apa yang tertulis. Kamu bukan penilai dan bukan "
                "pemecah soal.\n\n"

                "KONTEKS\n"
                "Foto-foto berikut adalah lembar jawaban milik satu siswa, diberikan sesuai "
                "urutan lembar. Baca semuanya sebagai satu kumpulan.\n"
                f"Nomor soal yang valid hanya: {sorted(nomor_soal_valid)}. Jangan membuat nomor lain. "
                "Jangan mengeluarkan entri jawaban kosong atau placeholder.\n\n"

                "LANGKAH KERJA\n"
                "1. Ambil nama siswa jika tertulis di lembar (biasanya di bagian atas). "
                "Jika tidak ada, isi dengan teks kosong.\n"
                "2. Temukan nomor soal yang benar-benar tampak pada lembar, lalu kelompokkan "
                "tulisan jawaban berdasarkan nomor tersebut. Jika satu jawaban berlanjut "
                "di lembar berikutnya, gabungkan sesuai urutan.\n"
                "3. Lembar bisa terbagi menjadi beberapa kolom atau bagian (misalnya kolom "
                "kiri dan kanan, atau kotak bernomor seperti ① ②). Baca satu kolom sampai "
                "selesai sebelum pindah ke kolom lain. Perhatikan nomor yang dilingkari "
                "atau ditulis di pinggir sebagai penanda soal.\n"
                "4. Tulis ulang tiap baris persis seperti yang tertulis siswa, dengan "
                "urutan baris yang sama. Pisahkan baris dengan karakter newline.\n\n"

                "ATURAN TRANSKRIPSI (PENTING)\n"
                "- Salin apa adanya. JANGAN memperbaiki, menghitung ulang, menyesuaikan, "
                "atau menambahkan langkah. Jika siswa menulis angka yang salah atau "
                "perhitungan yang keliru, salin kesalahan itu apa adanya.\n"
                "- JANGAN mengubah angka agar cocok dengan angka lain di lembar, dengan "
                "perhitungan, atau dengan logika soal. Ketidakcocokan angka adalah "
                "informasi yang dibutuhkan guru.\n"
                "- Pecahan: tulis dalam bentuk linear, misalnya 616/28 atau (1/3) x 22/7. "
                "Tulisan atas-bawah dengan garis di tengah adalah pecahan. Kata 'per' "
                "atau garis miring bermakna pecahan atau pembagian.\n"
                "- Operasi: tulis x untuk perkalian, : atau / untuk pembagian, sesuai "
                "tulisan siswa. Pangkat tulis dengan ^, misalnya r^2 atau 10^2. Akar "
                "tulis sebagai akar(...). Tulis pi sebagai π bila terlihat.\n"
                "- Satuan (cm, m, m^2, dm, dst.) dan tanda sama dengan harus ikut disalin.\n"
                "- Angka ambigu: pada tulisan tangan, angka 7 sering memakai garis "
                "silang di tengah atau garis di atas, dan sering mirip angka 1. Angka 1 "
                "biasanya lebih pendek dan tanpa garis atas. Angka 4 dan 9, serta 0 dan 6 "
                "juga sering tertukar. Perhatikan bentuk setiap angka dengan teliti, "
                "terutama pada angka yang berkali-kali muncul di lembar yang sama. "
                "Bandingkan dengan cara siswa menulis angka itu di tempat lain pada "
                "foto yang sama.\n"
                "- Coretan: tulisan yang dicoret jangan dimasukkan, kecuali satu-satunya "
                "isi baris itu. Jika dimasukkan, beri tanda [dicoret].\n"
                "- Jika ada bagian yang tidak terbaca sama sekali, tulis [?] di posisinya. "
                "Jangan menebak untuk menutup kekosongan.\n\n"

                "PENANDA KERAGUAN\n"
                "Untuk setiap angka atau simbol yang kamu ragukan (bentuknya bisa dibaca "
                "lebih dari satu cara), catat di field 'ragu':\n"
                "- 'teks': potongan yang kamu tulis di transkripsi,\n"
                "- 'alternatif': daftar bacaan lain yang mungkin,\n"
                "- 'lokasi': deskripsi singkat letaknya, misalnya 'baris 3, kolom kanan'.\n"
                "Lebih baik menandai angka yang sebenarnya benar daripada melewatkan "
                "angka yang salah baca. Angka yang kamu yakini tidak perlu ditandai.\n\n"

                "BAGIAN YANG TIDAK DAPAT DIPASANGKAN\n"
                "Jika ada tulisan nyata yang tidak dapat dikenali atau tidak dapat "
                "dipasangkan dengan nomor soal, masukkan ke 'tidak_terpetakan' beserta "
                "salinannya. Jangan mengisinya dengan teks kosong.\n\n"

                "FORMAT KELUARAN\n"
                "Keluarkan JSON saja, tanpa penjelasan dan tanpa blok kode:\n"
                '{"nama_siswa":"nama yang terbaca atau teks kosong",'
                '"jawaban":[{"nomor":1,"transkripsi":"baris 1\\nbaris 2",'
                '"ragu":[{"teks":"616","alternatif":["676"],"lokasi":"baris 4"}]}],'
                '"tidak_terpetakan":[]}'
            )

            try:
                for siswa in daftar_siswa:
                    konten_ocr = [{"type": "text", "text": instruksi_ocr}]

                    # Melampirkan foto sesuai urutan lembar siswa.
                    for foto in siswa["foto"]:
                        konten_ocr.append(
                            {
                                "type": "text",
                                "text": (
                                    f"Berikut foto lembar "
                                    f"{foto['nomor_lembar']}."
                                ),
                            }
                        )
                        konten_ocr.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": gambar_ke_data_url(foto["gambar"])
                                },
                            }
                        )

                    teks_ocr = minta_openrouter(
                        api_key,
                        MODEL_VISION,
                        konten_ocr,
                    )

                    # Mengambil objek JSON pertama dari respons OCR.
                    awal_json = teks_ocr.find("{")
                    if awal_json == -1:
                        raise ValueError(
                            f"Respons OCR siswa {siswa['urutan']} "
                            "tidak berisi JSON."
                        )

                    data_ocr, _ = json.JSONDecoder().raw_decode(
                        teks_ocr[awal_json:]
                    )

                    jawaban_ocr = {}
                    ragu_ocr = {}  # BARU: penanda ragu per nomor soal
                    bagian_tidak_terpetakan = data_ocr.get(
                        "tidak_terpetakan", []
                    )
                    if not isinstance(bagian_tidak_terpetakan, list):
                        bagian_tidak_terpetakan = [bagian_tidak_terpetakan]

                    for jawaban in data_ocr.get("jawaban", []):
                        nomor = jawaban.get("nomor")
                        teks_jawaban = str(
                            jawaban.get("transkripsi", "")
                        ).strip()

                        try:
                            nomor = int(nomor)
                        except (TypeError, ValueError):
                            bagian_tidak_terpetakan.append(jawaban)
                            continue

                        if nomor not in nomor_soal_valid or not teks_jawaban:
                            bagian_tidak_terpetakan.append(jawaban)
                            continue

                        # Menggabungkan jawaban jika satu soal berlanjut
                        # pada lembar lain.
                        if nomor in jawaban_ocr:
                            jawaban_ocr[nomor] += "\n" + teks_jawaban
                        else:
                            jawaban_ocr[nomor] = teks_jawaban

                        # BARU: simpan penanda ragu dari OCR.
                        daftar_ragu = jawaban.get("ragu") or []
                        if isinstance(daftar_ragu, list):
                            ragu_ocr.setdefault(nomor, []).extend(
                                r for r in daftar_ragu if isinstance(r, dict)
                            )

                    nama_terbaca = str(
                        data_ocr.get("nama_siswa", "")
                    ).strip()

                    # Jika nama dari foto kosong, pakai nama/kode manual
                    # sebagai cadangan.
                    nama_siswa_akhir = (
                        nama_terbaca
                        or siswa["nama_manual"]
                        or f"Siswa {siswa['urutan']}"
                    )

                    hasil_ocr_semua_siswa.append(
                        {
                            "urutan": siswa["urutan"],
                            "nama_siswa": nama_siswa_akhir,
                            "nama_dari_ocr": nama_terbaca,
                            "jawaban": jawaban_ocr,
                            "ragu": ragu_ocr,  # BARU
                            "tidak_terpetakan": bagian_tidak_terpetakan,
                            "gambar_bytes": [
                                foto["gambar"].getvalue()
                                for foto in siswa["foto"]
                            ],
                        }
                    )

                st.session_state["hasil_ocr_batch"] = hasil_ocr_semua_siswa

            except requests.RequestException as error:
                st.error(f"Permintaan OCR gagal: {error}")
                st.stop()

            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                st.error(f"Hasil OCR tidak dapat diproses: {error}")
                st.stop()

            # Memberi instruksi penilaian dan format jawaban kepada AI.
            # Menilai jawaban OCR satu siswa demi satu siswa.
            hasil_batch = []

            with st.spinner("Menilai jawaban para siswa..."):
                try:
                    for siswa_ocr in hasil_ocr_semua_siswa:
                        # Memasangkan jawaban OCR dengan soal dan acuan guru.
                        data_soal_ai = []

                        for soal in daftar_soal:
                            data_soal_ai.append(
                                {
                                    "nomor": soal["nomor"],
                                    "soal": soal["soal"],
                                    "acuan": soal["acuan"],
                                    "jawaban": siswa_ocr["jawaban"].get(
                                        soal["nomor"], ""
                                    ),
                                    # BARU: bagian yang diragukan OCR
                                    "ragu_ocr": siswa_ocr["ragu"].get(
                                        soal["nomor"], []
                                    ),
                                }
                            )

                        # Prompt penilaian hanya menerima teks, bukan foto.
                        prompt = f"""
                        Nilai jawaban siswa berdasarkan kelogisan dan ketepatan matematisnya.

                        Gunakan komponen rubrik dan bobot berikut:
                        {json.dumps(rubrik_aktif, ensure_ascii=False)}

                        Beri skor 0 sampai 100 untuk setiap komponen. Skor total dihitung sebagai
                        jumlah skor komponen dikali bobotnya dibagi 100.

                        Terima metode lain yang setara dan benar. Siswa boleh melewati langkah
                        perantara jika penalarannya tetap jelas.

                        Aturan pemeriksaan:
                        - Nilai jawaban berdasarkan teks pada bagian Data.
                        - Jika sebuah bagian jawaban tercantum di "ragu_ocr", angka atau simbol itu
                        mungkin salah dibaca OCR. Jangan mengurangi nilai karena bagian tersebut;
                        sebutkan di alasan bahwa guru perlu mencocokkannya dengan foto.
                        - Jika ada ketidakkonsistenan angka yang TIDAK tercantum di "ragu_ocr",
                        nilai apa adanya sesuai teks.
                        - Periksa operasi hitung, satuan, dan jawaban akhir sendiri.
                        - Jangan mengarang kutipan atau isi jawaban.
                        - Jika jawaban untuk suatu nomor kosong, jangan mengarang transkripsinya.
                        Jelaskan bahwa jawaban soal tersebut perlu diperiksa guru.

                        Untuk setiap soal, berikan skor usulan, rincian skor tiap komponen rubrik,
                        kutipan persis dari jawaban siswa sebagai bukti, dan alasan penilaian.
                        Jika tidak menemukan kutipan yang mendukung alasan, isi "bukti" dengan
                        teks kosong. Skor AI adalah saran yang harus ditinjau guru.

                        Keluarkan JSON saja:
                        {{
                        "hasil": [
                            {{
                            "nomor": 1,
                            "skor": 0,
                            "rincian": [
                                {{
                                "kriteria": "Nama komponen rubrik",
                                "skor": 0,
                                "alasan": "Alasan komponen"
                                }}
                            ],
                            "bukti": "Kutipan persis dari jawaban siswa",
                            "alasan": "Alasan penilaian"
                            }}
                        ]
                        }}

                        Data soal, acuan, rubrik, dan jawaban siswa:
                        {json.dumps(data_soal_ai, ensure_ascii=False)}
                        """

                        # Permintaan kedua untuk siswa ini: penilaian teks.
                        teks_nilai = minta_openrouter(
                            api_key,
                            MODEL,
                            prompt,
                        )

                        awal_json = teks_nilai.find("{")
                        if awal_json == -1:
                            raise ValueError(
                                f"Respons penilaian {siswa_ocr['nama_siswa']} "
                                "tidak berisi JSON."
                            )

                        data_nilai, _ = json.JSONDecoder().raw_decode(
                            teks_nilai[awal_json:]
                        )
                        hasil_ai = data_nilai["hasil"]

                        if not isinstance(hasil_ai, list):
                            raise ValueError(
                                f"Hasil penilaian {siswa_ocr['nama_siswa']} "
                                "bukan berupa daftar."
                            )

                        if len(hasil_ai) != len(daftar_soal):
                            raise ValueError(
                                f"Jumlah hasil penilaian {siswa_ocr['nama_siswa']} "
                                "tidak sesuai dengan jumlah soal."
                            )

                        hasil_siswa = []
                        nomor_ditemukan = set()

                        for item_nilai in hasil_ai:
                            nomor = int(item_nilai["nomor"])

                            if nomor in nomor_ditemukan:
                                raise ValueError(
                                    f"AI mengulang nomor soal {nomor}."
                                )

                            skor, rincian_rubrik = hitung_skor_rubrik(
                                item_nilai.get("rincian", []),
                                rubrik_aktif,
                            )

                            jawaban_asli = siswa_ocr["jawaban"].get(
                                nomor, ""
                            )

                            hasil_siswa.append(
                                {
                                    "nomor": nomor,
                                    "skor_ai": skor,
                                    "rincian": rincian_rubrik,
                                    "bukti": str(item_nilai.get("bukti", "")),
                                    "alasan": str(item_nilai.get("alasan", "")),
                                    "jawaban": jawaban_asli,
                                    "jawaban_terbaca": jawaban_asli,
                                    "ragu": siswa_ocr["ragu"].get(nomor, []),  # BARU
                                    "sumber_foto": True,
                                    "gambar_bytes": siswa_ocr["gambar_bytes"],
                                    "soal": daftar_soal[nomor - 1]["soal"],
                                    "acuan": daftar_soal[nomor - 1]["acuan"],
                                }
                            )

                            nomor_ditemukan.add(nomor)

                        nomor_diharapkan = {
                            soal["nomor"] for soal in daftar_soal
                        }
                        if nomor_ditemukan != nomor_diharapkan:
                            raise ValueError(
                                f"Ada soal yang belum dinilai untuk "
                                f"{siswa_ocr['nama_siswa']}."
                            )

                        hasil_batch.append(
                            {
                                "urutan": siswa_ocr["urutan"],
                                "nama_siswa": siswa_ocr["nama_siswa"],
                                "metadata": {
                                    "nama_guru": nama_guru,
                                    "mapel": mapel,
                                    "nama_tugas": nama_tugas,
                                    "nama_siswa": siswa_ocr["nama_siswa"],
                                },
                                "hasil": sorted(
                                    hasil_siswa,
                                    key=lambda item: item["nomor"],
                                ),
                                "tidak_terpetakan": siswa_ocr[
                                    "tidak_terpetakan"
                                ],
                            }
                        )

                        # Menyimpan progres agar hasil siswa sebelumnya tidak hilang
                        # jika permintaan untuk siswa berikutnya gagal.
                        st.session_state["hasil_penilaian_batch"] = list(
                            hasil_batch
                        )

                    st.session_state["hasil_telah_disimpan"] = False

                except requests.RequestException as error:
                    st.error(f"Permintaan ke OpenRouter gagal: {error}")

                except (ValueError, KeyError, IndexError, TypeError) as error:
                    st.error(f"Hasil penilaian tidak dapat diproses: {error}")

# Menampilkan, meninjau, dan menyimpan hasil untuk setiap siswa.
hasil_batch = st.session_state.get("hasil_penilaian_batch", [])

if hasil_batch:
    st.divider()
    st.subheader("3. Tinjau hasil koreksi siswa")

    baris_csv = [
        ["Nama siswa", "Nomor soal", "Skor AI", "Nilai guru", "Alasan"]
    ]

    for data_siswa in hasil_batch:
        urutan = data_siswa["urutan"]
        nama = data_siswa["nama_siswa"]
        hasil = data_siswa["hasil"]

        st.markdown(f"## Siswa {urutan}: {nama}")

        if data_siswa.get("tidak_terpetakan"):
            st.warning(
                "Ada tulisan pada foto yang belum berhasil dipasangkan "
                "dengan nomor soal. Periksa bagian ini sebelum menyetujui nilai."
            )
            st.json(data_siswa["tidak_terpetakan"])

        with st.expander("Lihat foto lembar jawaban"):
            for indeks_foto, foto_bytes in enumerate(
                hasil[0]["gambar_bytes"], start=1
            ):
                st.image(
                    foto_bytes,
                    caption=f"Lembar {indeks_foto}",
                    width=420,
                )

        skor_final_siswa = []
        status_setuju_siswa = []

        for item in hasil:
            nomor = item["nomor"]
            skor_awal = int(round(item["skor_ai"]))
            warna, label = lencana_skor(item["skor_ai"])

            with st.container(border=True):
                kol_judul, kol_skor = st.columns([3, 1])

                with kol_judul:
                    st.markdown(f"### Soal {nomor}")
                    st.markdown(
                        f'<span class="badge" style="background:{warna}">'
                        f"{label}</span>",
                        unsafe_allow_html=True,
                    )

                with kol_skor:
                    st.markdown(
                        f'<div class="skor-besar" style="color:{warna};'
                        f'text-align:right">{item["skor_ai"]:g}'
                        f'<span style="font-size:1rem;opacity:.6">'
                        "/100</span></div>"
                        '<div style="text-align:right;font-size:.8rem;'
                        'opacity:.7">saran AI</div>',
                        unsafe_allow_html=True,
                    )

                st.markdown(
                    f'<div class="alasan"><b>Alasan AI:</b> '
                    f'{item["alasan"]}</div>',
                    unsafe_allow_html=True,
                )

                with st.expander("Rincian skor menurut rubrik"):
                    st.dataframe(
                        item.get("rincian", []),
                        hide_index=True,
                        width="stretch",
                    )

                jawaban_asli = item["jawaban"]
                bukti = item["bukti"]

                if not jawaban_asli.strip():
                    st.warning(
                        "Jawaban untuk nomor ini tidak ditemukan oleh OCR. "
                        "Periksa foto sebelum menetapkan nilai."
                    )
                elif bukti and normalkan(bukti) in normalkan(jawaban_asli):
                    st.success(f"Kutipan AI ditemukan pada jawaban: “{bukti}”")
                else:
                    st.warning(
                        "Kutipan dari AI tidak ditemukan persis pada "
                        "transkripsi. Periksa foto dan alasan penilaiannya."
                    )

                # BARU: peringatan bagian yang diragukan OCR.
                ragu_item = item.get("ragu", [])
                if ragu_item:
                    baris_ragu = []
                    for r in ragu_item:
                        alt = ", ".join(
                            str(a) for a in (r.get("alternatif") or [])
                        )
                        baris_ragu.append(
                            f"- **{r.get('teks', '?')}** "
                            f"(kemungkinan lain: {alt or '-'}) "
                            f"— {r.get('lokasi', '')}"
                        )
                    st.warning(
                        "OCR ragu pada bagian berikut. Cocokkan dengan foto:\n\n"
                        + "\n".join(baris_ragu)
                    )

                with st.expander("Lihat transkripsi jawaban"):
                    st.text(jawaban_asli)

                kol_nilai, kol_setuju = st.columns([1, 2])

                with kol_nilai:
                    skor_final = st.number_input(
                        "Revisi nilai guru (0–100)",
                        min_value=0,
                        max_value=100,
                        value=skor_awal,
                        step=1,
                        key=f"nilai_guru_{urutan}_{nomor}",
                    )

                with kol_setuju:
                    disetujui = st.checkbox(
                        "Saya sudah memeriksa transkripsi dan meninjau nilai",
                        key=f"setuju_{urutan}_{nomor}",
                    )

            skor_final_siswa.append(float(skor_final))
            status_setuju_siswa.append(disetujui)

            baris_csv.append(
                [
                    nama,
                    nomor,
                    f'{item["skor_ai"]:g}',
                    f"{skor_final:g}",
                    item["alasan"],
                ]
            )

        nilai_akhir_siswa = sum(skor_final_siswa) / len(skor_final_siswa)
        jumlah_setuju = sum(status_setuju_siswa)
        total_soal = len(status_setuju_siswa)
        semua_disetujui = jumlah_setuju == total_soal

        st.progress(
            jumlah_setuju / total_soal,
            text=f"{jumlah_setuju} dari {total_soal} soal siswa ini disetujui",
        )
        st.metric(
            f"Nilai akhir {nama}",
            f"{nilai_akhir_siswa:.2f}/100",
        )

        if semua_disetujui:
            if not akun_aktif:
                st.info(
                    "Masuk atau buat akun untuk menyimpan hasil siswa ini."
                )
                if st.button(
                    f"Masuk atau buat akun untuk menyimpan — siswa {urutan}",
                    key=f"masuk_simpan_{urutan}",
                    type="primary",
                ):
                    st.session_state["auth_mode"] = "masuk"
                    st.rerun()

            else:
                siswa_tersimpan = st.session_state.get(
                    "siswa_tersimpan", []
                )
                kunci_siswa = str(urutan)

                if kunci_siswa in siswa_tersimpan:
                    st.success("Hasil siswa ini sudah tersimpan.")
                elif st.button(
                    f"Simpan hasil siswa {urutan} ke database",
                    key=f"simpan_siswa_{urutan}",
                    type="primary",
                ):
                    try:
                        id_koreksi = simpan_koreksi(
                            akun_aktif["id"],
                            data_siswa["metadata"],
                            hasil,
                            skor_final_siswa,
                            nilai_akhir_siswa,
                        )

                        siswa_tersimpan.append(kunci_siswa)
                        st.session_state["siswa_tersimpan"] = siswa_tersimpan
                        st.success(
                            f"Hasil {nama} berhasil disimpan. "
                            f"Nomor data: {id_koreksi}"
                        )
                        st.rerun()

                    except sqlite3.Error as error:
                        st.error(f"Gagal menyimpan ke database: {error}")
        else:
            st.warning(
                "Setujui semua soal siswa ini sebelum menyimpan hasilnya."
            )

        st.divider()

    # Mengunduh rekap seluruh siswa yang sedang ditinjau.
    buffer = io.StringIO()
    penulis = csv.writer(buffer)
    penulis.writerows(baris_csv)

    st.download_button(
        "⬇️ Unduh rekap semua siswa (CSV)",
        data=buffer.getvalue(),python3 -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501
        file_name="rekap_nilai_siswa.csv",
        mime="text/csv",
        width="stretch",
    )

