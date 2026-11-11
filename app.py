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

st.set_page_config(
    page_title="Asisten Koreksi Uraian",
    page_icon="📝",
    layout="centered",
)

# Memastikan tabel database siap sebelum aplikasi digunakan
siapkan_database()

# Fitur tampilan: tema ungu, kartu, lencana skor, dan header responsif.
st.markdown(
    """
<style>
:root { --accent:#6256e8; --border:rgba(128,128,128,.22); }
.block-container { max-width:980px; padding:2.2rem 1.4rem 4rem; }
.hero { position:relative; overflow:hidden; background:linear-gradient(130deg,#4c1d95 0%,#6d35c9 55%,#8b5cf6 100%); color:#fff; padding:2rem 2.2rem; border-radius:22px; margin-bottom:1.5rem; box-shadow:0 16px 38px rgba(91,33,182,.28); }
.hero:after { content:""; position:absolute; width:210px; height:210px; right:-55px; top:-95px; border:1px solid rgba(255,255,255,.18); border-radius:50%; box-shadow:0 0 0 24px rgba(255,255,255,.05),0 0 0 48px rgba(255,255,255,.035); }
.hero h1 { color:#fff; margin:0; font-size:clamp(1.6rem,3vw,2.15rem); padding:0; letter-spacing:-.035em; position:relative; z-index:1; }
.hero p { color:#ede9fe; margin:.55rem 0 0; max-width:650px; font-size:1rem; line-height:1.6; position:relative; z-index:1; }
.hero-kicker { display:inline-block; margin-bottom:.7rem; padding:.28rem .65rem; border:1px solid rgba(255,255,255,.3); border-radius:999px; color:#f5f3ff; font-size:.72rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }
.badge { display:inline-block; color:#fff; font-weight:700; padding:.3rem .8rem; border-radius:999px; font-size:.86rem; }
.skor-besar { font-size:2.2rem; font-weight:800; line-height:1; }
.chip-ok { background:rgba(34,197,94,.13); color:#15803d; padding:.65rem .85rem; border:1px solid rgba(34,197,94,.25); border-radius:12px; font-size:.92rem; }
.chip-warn { background:rgba(245,158,11,.13); color:#b45309; padding:.65rem .85rem; border:1px solid rgba(245,158,11,.25); border-radius:12px; font-size:.92rem; }
.alasan { background:rgba(127,127,127,.08); border:1px solid var(--border); padding:.85rem 1rem; border-radius:12px; font-size:.94rem; line-height:1.6; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:16px; border-color:rgba(202,173,245,.26); }
[data-testid="stTextArea"] textarea, [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input { background:rgba(48,31,76,.84) !important; color:#f4f7fb !important; border:1px solid rgba(202,173,245,.36) !important; border-radius:11px; }
[data-testid="stTextArea"] textarea:focus, [data-testid="stTextInput"] input:focus, [data-testid="stNumberInput"] input:focus { border-color:#c9a9ff !important; box-shadow:0 0 0 2px rgba(192,156,255,.2) !important; }
[data-testid="stTextArea"] textarea::placeholder, [data-testid="stTextInput"] input::placeholder { color:rgba(238,225,255,.58) !important; }
[data-testid="stFileUploader"] section { background:rgba(48,31,76,.62); border:1px dashed rgba(202,173,245,.4); border-radius:13px; }
[data-testid="stFileUploader"] section, [data-testid="stFileUploader"] section * { color:#f3eaff; }
[data-testid="stCameraInput"] button { background:rgba(69,43,105,.86); color:#f4f7fb; border:1px solid rgba(202,173,245,.34); border-radius:10px; }
.stButton>button, .stFormSubmitButton>button { min-height:2.8rem; border-radius:12px; font-weight:650; padding:.6rem 1.05rem; transition:transform .15s ease, box-shadow .15s ease; }
.stButton>button:hover, .stFormSubmitButton>button:hover { transform:translateY(-1px); box-shadow:0 6px 16px rgba(139,92,246,.22); }
[data-testid="stSidebar"] { border-right:1px solid rgba(202,173,245,.22); }
[data-testid="stAlert"] { border-radius:12px; }
@media(max-width:700px) { .block-container { padding:1.2rem .8rem 3rem; } .hero { padding:1.45rem 1.25rem; border-radius:17px; } .hero p {font-size:.92rem;} }
</style>
<div class="hero">
  <span class="hero-kicker">Asisten guru · penilaian uraian</span>
  <h1>📝 Asisten Koreksi Uraian</h1>
  <p>Asisten AI untuk membantu guru memberikan saran nilai pada ujian uraian.
  Guru tetap perlu meninjau dan menetapkan nilai akhir.</p>
</div>
""",
    unsafe_allow_html=True,
)

# Fitur wallpaper: membaca gambar JPG, PNG, atau WebP dari folder "bahan gambar".
FOLDER_GAMBAR_LATAR = Path(__file__).with_name("bahan gambar")
GAMBAR_LATAR = next(
    (file for file in sorted(FOLDER_GAMBAR_LATAR.glob("*"))
     if file.is_file() and file.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}),
    None,
)
if GAMBAR_LATAR is not None:
    tipe_gambar = mimetypes.guess_type(GAMBAR_LATAR.name)[0] or "image/jpeg"
    gambar_base64 = base64.b64encode(GAMBAR_LATAR.read_bytes()).decode("ascii")
    st.markdown(
        f"""
        <style>
        .stApp {{
            background:
                linear-gradient(rgba(34, 15, 57, .48), rgba(34, 15, 57, .64)),
                url("data:{tipe_gambar};base64,{gambar_base64}") center / cover fixed no-repeat;
        }}
        .stApp,
        [data-testid="stAppViewContainer"] {{
            background-color: transparent;
            filter: none !important;
            backdrop-filter: none !important;
            -webkit-backdrop-filter: none !important;
        }}
        div[data-testid="stVerticalBlockBorderWrapper"],
        [data-testid="stForm"] {{
            position: relative;
            isolation: isolate;
            overflow: hidden;
            background: transparent !important;
            border: 1px solid rgba(255, 255, 255, .42) !important;
            border-radius: 16px;
            box-shadow: 0 12px 36px rgba(12, 5, 25, .18);
        }}
        div[data-testid="stVerticalBlockBorderWrapper"]::before,
        [data-testid="stForm"]::before {{
            content: "";
            position: absolute;
            inset: 1px;
            z-index: -1;
            border-radius: inherit;
            background: rgba(52, 34, 78, .78);
            backdrop-filter: none !important;
            -webkit-backdrop-filter: none !important;
            pointer-events: none;
        }}
        [data-testid="stTextArea"] textarea,
        [data-testid="stTextInput"] input,
        [data-testid="stNumberInput"] input {{
            backdrop-filter: none !important;
            -webkit-backdrop-filter: none !important;
        }}
        [data-testid="stSidebar"] > div:first-child {{
            background: rgba(31, 19, 49, .94);
        }}
        [data-testid="stHeader"] {{ background: rgba(0, 0, 0, 0); }}
        </style>
        """,
        unsafe_allow_html=True,
    )

# Fitur login/daftar: pengguna boleh mencoba koreksi, lalu masuk untuk menyimpan hasil.
st.session_state.setdefault("auth_mode", None)
akun_aktif = st.session_state.get("akun_aktif")

kol_status, kol_masuk, kol_daftar = st.columns([6, 1, 1])
with kol_status:
    if akun_aktif:
        st.caption("Akun aktif: " + akun_aktif["email"])
    else:
        st.caption("Mode uji coba: koreksi bisa dicoba, login diperlukan untuk menyimpan riwayat.")
with kol_masuk:
    if akun_aktif:
        if st.button("Keluar", key="tombol_keluar", width="stretch"):
            st.session_state.pop("akun_aktif", None)
            st.session_state.pop("hasil_penilaian", None)
            st.session_state.pop("metadata_koreksi", None)
            st.session_state.pop("hasil_telah_disimpan", None)
            st.rerun()
    elif st.button("Masuk", key="tombol_masuk", width="stretch"):
        st.session_state["auth_mode"] = "masuk"
with kol_daftar:
    if not akun_aktif and st.button("Buat akun", key="tombol_daftar", width="stretch"):
        st.session_state["auth_mode"] = "daftar"

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

daftar_jawaban = []

# Membuat kartu input untuk setiap soal, acuan, dan jawaban siswa.
with st.form("form_koreksi"):
    st.subheader("1. Masukkan soal dan jawaban")

    # Informasi yang akan disimpan bersama hasil koreksi
    nama_guru = st.text_input("Nama guru")
    mapel = st.text_input("Mata pelajaran")
    nama_tugas = st.text_input("Nama tugas atau ulangan")
    nama_siswa = st.text_input("Nama samaran atau kode siswa")

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
                    f"Kunci jawaban atau acuan {nomor}",
                    key=f"acuan_{nomor}",
                    height=130,
                    help="Isi dengan jawaban acuan atau informasi yang membantu penilaian.",
                )

            jawaban = st.text_area(
                f"Jawaban siswa untuk soal {nomor}",
                key=f"jawaban_{nomor}",
                height=160,
            )

            # Jawaban dapat diketik atau dimasukkan melalui satu foto.
            st.caption("Isi jawaban dengan teks atau gunakan salah satu pilihan foto.")

            foto_upload = st.file_uploader(
                f"Unggah foto jawaban soal {nomor}",
                type=["jpg", "jpeg", "png", "webp"],
                key=f"foto_upload_{nomor}",
            )

            foto_kamera = st.camera_input(
                f"Atau ambil foto jawaban soal {nomor}",
                key=f"foto_kamera_{nomor}",
                resolution="720p",
            )

            gambar = foto_upload if foto_upload is not None else foto_kamera
            dua_sumber_foto = foto_upload is not None and foto_kamera is not None
            if gambar is not None:
                st.image(gambar, caption=f"Pratinjau foto jawaban soal {nomor}", width=420)

            daftar_jawaban.append(
                {
                    "nomor": nomor,
                    "soal": soal,
                    "acuan": acuan,
                    "jawaban": jawaban,
                    "gambar": gambar,
                    "dua_sumber_foto": dua_sumber_foto,
                }
            )

    kirim = st.form_submit_button(
        "✨ Minta saran penilaian",
        type="primary",
        width="stretch",
    )


# Memeriksa kelengkapan data dan mengirimkannya ke AI setelah tombol diklik.
if kirim:
    st.session_state.pop("hasil_penilaian", None)

    # Menghapus status simpan lama saat koreksi baru diminta
    st.session_state.pop("metadata_koreksi", None)
    st.session_state.pop("hasil_telah_disimpan", None)

    # Memeriksa informasi tugas dan semua isian soal
    ada_yang_kosong = (
        not nama_guru.strip()
        or not mapel.strip()
        or not nama_tugas.strip()
        or not nama_siswa.strip()
        or sum(
            item["gambar"].size
            for item in daftar_jawaban
            if item["gambar"] is not None
        ) > 10 * 1024 * 1024
        or any(
            not item["soal"].strip()
            or not item["acuan"].strip()
            or (
                not item["jawaban"].strip()
                and item["gambar"] is None
            )
            or item["dua_sumber_foto"]
            or (
                item["jawaban"].strip()
                and item["gambar"] is not None
            )
            or (
                item["gambar"] is not None
                and item["gambar"].size > 5 * 1024 * 1024
            )
            for item in daftar_jawaban
        )
    )

    if not rubrik_valid:
        st.warning("Perbaiki rubrik di Pengaturan; total bobot harus 100%.")
    elif ada_yang_kosong:
        st.warning(
            "Lengkapi soal dan acuan, lalu pilih tepat satu cara untuk jawaban "
            "(ketik, unggah foto, atau ambil foto). Maksimal 5 MB per foto dan 10 MB total."
        )
    else:
        # Membaca API key dari Codespaces secret.
        api_key = os.getenv("OPENROUTER_API_KEY")

        if not api_key:
            st.error("API key belum terbaca. Periksa Codespaces secret.")
        else:
            # Menyiapkan data teks saja; objek file foto tidak ikut dimasukkan ke JSON.
            ada_foto = any(item["gambar"] is not None for item in daftar_jawaban)

            data_soal_ai = []
            for item in daftar_jawaban:
                data_soal_ai.append(
                    {
                        "nomor": item["nomor"],
                        "soal": item["soal"],
                        "acuan": item["acuan"],
                        "jawaban": (
                            "Jawaban ada pada foto yang dilampirkan."
                            if item["gambar"] is not None
                            else item["jawaban"]
                        ),
                    }
                )
            # Memberi instruksi penilaian dan format jawaban kepada AI.
            prompt = f"""
Nilai setiap penyelesaian secara menyeluruh berdasarkan kelogisan
dan ketepatan matematisnya.

Gunakan komponen rubrik dan bobot berikut:
{json.dumps(rubrik_aktif, ensure_ascii=False)}
Beri skor 0 sampai 100 untuk setiap komponen, lalu hitung skor total
sebagai jumlah skor komponen dikali bobotnya dibagi 100.

Terima metode lain yang setara dan benar. Siswa boleh melewati langkah
perantara jika penalarannya tetap jelas. Periksa seluruh jawaban sebelum
menyatakan suatu langkah tidak ada. Jangan mengarang isi jawaban.

Untuk setiap soal, berikan:
- skor usulan 0 sampai 100;
- kutipan pendek yang disalin persis dari jawaban siswa sebagai bukti;
- alasan penilaian singkat.

Jika tidak menemukan kutipan yang mendukung alasan, isi "bukti" dengan
teks kosong. Skor AI adalah saran yang harus ditinjau guru.

Keluarkan JSON saja:
{{
  "hasil": [
    {{
      "nomor": 1,
      "skor": 0,
      "rincian": [
        {{"kriteria": "Nama komponen rubrik", "skor": 0, "alasan": "Alasan komponen"}}
      ],
      "bukti": "Kutipan persis dari jawaban siswa",
      "alasan": "Alasan penilaian",
      "jawaban_terbaca": "Salinan jawaban siswa"
    }}
  ]
}}

Data:
{json.dumps(data_soal_ai, ensure_ascii=False)}
"""

            with st.spinner("Meminta saran penilaian dari AI..."):

                # Jika ada foto, gunakan model vision dan lampirkan gambarnya.
                model_dipakai = MODEL
                konten_pesan = prompt

                if ada_foto:
                    model_dipakai = MODEL_VISION
                    konten_pesan = [{"type": "text", "text": prompt}]

                    total_ukuran_foto = sum(
                        item["gambar"].size
                        for item in daftar_jawaban
                        if item["gambar"] is not None
                    )

                    if total_ukuran_foto > 10 * 1024 * 1024:
                        raise ValueError("Gabungan ukuran foto maksimal 10 MB per kiriman.")

                    for item in daftar_jawaban:
                        if item["gambar"] is not None:
                            konten_pesan.append(
                                {
                                    "type": "text",
                                    "text": f"Foto jawaban untuk soal {item['nomor']}.",
                                }
                            )
                            konten_pesan.append(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": gambar_ke_data_url(item["gambar"])
                                    },
                                }
                            )
                try:
                    # Mengirim semua soal dan jawaban dalam satu permintaan API.
                    response = requests.post(
                        OPENROUTER_URL,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model_dipakai,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": konten_pesan,
                                }
                            ],
                            "temperature": 0.1,
                        },
                        timeout=90,
                    )

                    if not response.ok:
                        st.error(
                            f"OpenRouter memberi respons "
                            f"{response.status_code}."
                        )
                        st.code(response.text[:1000])
                    else:
                        # Mengambil teks jawaban AI.
                        pesan_ai = response.json().get("choices", [{}])[0].get("message", {})
                        isi_ai = pesan_ai.get("content")
                        if not isinstance(isi_ai, str) or not isi_ai.strip():
                            raise ValueError(
                                "OpenRouter mengirim jawaban kosong. Coba kirim ulang; "
                                "jika terjadi lagi, periksa model atau batas penggunaan API."
                            )

                        # Menghapus pembungkus Markdown jika ditambahkan AI.
                        isi_ai = re.sub(
                            r"^```(?:json)?\s*|\s*```$",
                            "",
                            isi_ai.strip(),
                        )

                        data_ai = json.loads(isi_ai)
                        hasil_ai = data_ai["hasil"]

                        if not isinstance(hasil_ai, list):
                            raise ValueError("Hasil AI bukan berupa daftar.")

                        if len(hasil_ai) != int(jumlah_soal):
                            raise ValueError("Jumlah hasil AI tidak sesuai.")

                        hasil_tersimpan = []
                        nomor_ditemukan = set()

                        for item in hasil_ai:
                            nomor = int(item["nomor"])
                            skor, rincian_rubrik = hitung_skor_rubrik(item.get("rincian", []), rubrik_aktif)
                            bukti = str(item.get("bukti", ""))
                            alasan = str(item["alasan"])
                            jawaban_ai = str(item.get("jawaban_terbaca", "")).strip()

                            if nomor in nomor_ditemukan:
                                raise ValueError("AI mengulang nomor soal.")

                            if skor < 0 or skor > 100:
                                raise ValueError("Skor harus antara 0 dan 100.")

                            # Mengambil soal, acuan, dan jawaban siswa
                            data_soal = daftar_jawaban[nomor - 1]
                            jawaban_asli = data_soal["jawaban"]

                            hasil_tersimpan.append(
                                {
                                    "nomor": nomor,
                                    "skor_ai": skor,
                                    "rincian": rincian_rubrik,
                                    "bukti": bukti,
                                    "alasan": alasan,
                                    "jawaban": (
                                        jawaban_ai
                                        if data_soal["gambar"] is not None
                                        else jawaban_asli
                                    ),
                                    "jawaban_terbaca": jawaban_ai,
                                    "sumber_foto": data_soal["gambar"] is not None,
                                    "gambar_bytes": (
                                        data_soal["gambar"].getvalue()
                                        if data_soal["gambar"] is not None
                                        else None
                                    ),
                                    "soal": data_soal["soal"],
                                    "acuan": data_soal["acuan"],
                                }
                            )

                            nomor_ditemukan.add(nomor)

                        nomor_diharapkan = set(
                            range(1, int(jumlah_soal) + 1)
                        )

                        if nomor_ditemukan != nomor_diharapkan:
                            raise ValueError("Ada soal yang belum dinilai.")

                        st.session_state["hasil_penilaian"] = sorted(
                            hasil_tersimpan,
                            key=lambda item: item["nomor"],
                        )

                        # Menyimpan identitas tugas untuk tombol simpan
                        st.session_state["metadata_koreksi"] = {
                            "nama_guru": nama_guru,
                            "mapel": mapel,
                            "nama_tugas": nama_tugas,
                            "nama_siswa": nama_siswa,
                        }
                        st.session_state["hasil_telah_disimpan"] = False

                # Menampilkan pesan jika koneksi atau format respons bermasalah.
                except requests.RequestException as error:
                    st.error(f"Permintaan ke OpenRouter gagal: {error}")

                except ValueError as error:
                    st.error(f"Permintaan tidak dapat diproses: {error}")

                except (KeyError, IndexError, TypeError):
                    st.error(
                        "Hasil AI tidak terbaca atau jumlah skornya tidak lengkap. "
                        "Coba kirim ulang."
                    )

# Menampilkan hasil agar guru bisa memeriksa dan menyetujui nilai.
if st.session_state.get("hasil_penilaian"):
    hasil = st.session_state["hasil_penilaian"]
    st.divider()
    st.subheader("2. Tinjau saran penilaian")

    skor_final_semua = []
    status_setuju = []

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
                    f'<span style="font-size:1rem;opacity:.6">/100</span></div>'
                    f'<div style="text-align:right;font-size:.8rem;'
                    f'opacity:.7">saran AI</div>',
                    unsafe_allow_html=True,
                )

            st.markdown(
                f'<div class="alasan"><b>Alasan AI:</b> {item["alasan"]}</div>',
                unsafe_allow_html=True,
            )
            with st.expander("Rincian skor menurut rubrik"):
                st.dataframe(item.get("rincian", []), hide_index=True, width="stretch")

            bukti = item["bukti"]

            # Tampilkan teks yang diketik atau hasil baca AI dari foto.
            jawaban_asli = item["jawaban"]
            if item.get("sumber_foto") and not item.get("jawaban_terbaca"):
                st.warning("AI belum menghasilkan transkripsi. Periksa jawaban pada foto.")

            if bukti and normalkan(bukti) in normalkan(jawaban_asli):
                st.markdown(
                    f'<div class="chip-ok" style="margin-top:.6rem">'
                    f"✅ Kutipan ditemukan: “{bukti}”</div>",
                    unsafe_allow_html=True,
                )
            else:
                aneh = f" Kutipan AI: “{bukti}”" if bukti else ""
                st.markdown(
                    '<div class="chip-warn" style="margin-top:.6rem">'
                    "⚠️ Kutipan tidak ditemukan persis di jawaban siswa. "
                    f"Periksa jawaban asli sebelum menyetujui nilai.{aneh}</div>",
                    unsafe_allow_html=True,
                )

            judul_jawaban = (
                "Teks yang dibaca dari foto"
                if item.get("sumber_foto")
                else "Lihat jawaban asli siswa"
            )

            with st.expander(judul_jawaban):
                if item.get("sumber_foto"):
                    st.markdown("**Teks yang dibaca AI dari foto**")
                st.text(jawaban_asli)

                if item.get("sumber_foto") and item.get("gambar_bytes"):
                    st.image(item["gambar_bytes"], caption="Foto jawaban yang dikirim")

            kol_nilai, kol_setuju = st.columns([1, 2])
            with kol_nilai:
                # Human-in-the-loop: guru dapat merevisi saran skor AI secara manual.
                skor_final = st.number_input(
                    "Revisi nilai akhir guru (0–100)",
                    min_value=0,
                    max_value=100,
                    value=skor_awal,
                    step=1,
                    key=f"nilai_guru_{nomor}",
                )
                st.caption(
                    "Skor AI hanya saran. Ubah nilai ini sesuai penilaian guru; "
                    "nilai yang disetujui inilah yang akan disimpan."
                )
            with kol_setuju:
                st.write("")
                st.write("")
                
                label_persetujuan = (
                    "Saya sudah memeriksa teks foto dan meninjau nilai"
                    if item.get("sumber_foto")
                    else "Sudah saya tinjau dan setujui"
                )

                disetujui = st.checkbox(
                    label_persetujuan,
                    key=f"setuju_{nomor}",
                )

        skor_final_semua.append(float(skor_final))
        status_setuju.append(disetujui)

    # Ringkasan nilai akhir.
    nilai_akhir = sum(skor_final_semua) / len(skor_final_semua)
    jumlah_setuju = sum(status_setuju)
    total = len(status_setuju)
    semua_disetujui = jumlah_setuju == total

    st.divider()
    st.subheader("3. Nilai akhir")
    st.progress(jumlah_setuju / total, text=f"{jumlah_setuju} dari {total} soal disetujui")

    if semua_disetujui:
        st.metric("Nilai akhir yang disetujui guru", f"{nilai_akhir:.2f}/100")
        st.success("Semua soal sudah disetujui.")

        # Fitur simpan: hanya akun yang sudah login dapat menyimpan hasil ke riwayat.
        # Menyimpan hasil setelah guru menyetujui semua soal
        if not st.session_state.get("hasil_telah_disimpan", False):
            if not akun_aktif:
                st.info("Mode uji coba: login atau buat akun untuk menyimpan hasil dan melihat riwayatnya.")
                if st.button("Masuk atau buat akun untuk menyimpan", type="primary"):
                    st.session_state["auth_mode"] = "masuk"
                    st.rerun()
            elif st.button("Simpan hasil ke database", type="primary"):
                try:
                    id_koreksi = simpan_koreksi(
                        akun_aktif["id"],
                        st.session_state["metadata_koreksi"],
                        hasil,
                        skor_final_semua,
                        nilai_akhir,
                    )
                    st.session_state["hasil_telah_disimpan"] = True
                    st.success(
                        f"Hasil berhasil disimpan. Nomor data: {id_koreksi}"
                    )
                except sqlite3.Error as error:
                    st.error(f"Gagal menyimpan ke database: {error}")
        else:
            st.success("Hasil ini sudah tersimpan di akunmu.")

        # Unduhan rekap dalam format CSV.
        buffer = io.StringIO()
        penulis = csv.writer(buffer)
        penulis.writerow(["nomor", "skor_ai", "nilai_guru", "rincian_rubrik"])
        for item, nilai in zip(hasil, skor_final_semua):
            penulis.writerow([item["nomor"], f"{item['skor_ai']:g}", f"{nilai:g}", json.dumps(item.get("rincian", []), ensure_ascii=False)])
        penulis.writerow(["rata-rata", "", f"{nilai_akhir:.2f}"])

        st.download_button(
            "⬇️ Unduh rekap (CSV)",
            data=buffer.getvalue(),
            file_name="rekap_nilai.csv",
            mime="text/csv",
            width="stretch",
        )
    else:
        st.metric("Rata-rata sementara", f"{nilai_akhir:.2f}/100")
        st.warning("Setujui nilai setiap soal untuk menetapkan nilai akhir.")


# Fitur batch: masukkan soal sekali untuk satu kelas dan tinjau seluruh hasil dalam satu alur.
MAX_SISWA_BATCH = 30


def template_csv_batch(jumlah_soal):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["nama_siswa"] + [f"jawaban_{n}" for n in range(1, jumlah_soal + 1)])
    return output.getvalue()


def parse_csv_batch(file_csv, jumlah_soal):
    reader = csv.DictReader(io.StringIO(file_csv.getvalue().decode("utf-8-sig")))
    required = {"nama_siswa"} | {f"jawaban_{n}" for n in range(1, jumlah_soal + 1)}
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise ValueError("Kolom CSV kurang: " + ", ".join(sorted(missing)))
    students = []
    for row in reader:
        name = (row.get("nama_siswa") or "").strip()
        answers = [(row.get(f"jawaban_{n}") or "").strip() for n in range(1, jumlah_soal + 1)]
        if not name and not any(answers):
            continue
        if not name:
            raise ValueError("Ada baris jawaban tanpa nama_siswa.")
        students.append({"siswa_id": len(students) + 1, "nama_siswa": name, "jawaban": answers})
    if not students:
        raise ValueError("CSV belum berisi data siswa.")
    if len(students) > MAX_SISWA_BATCH:
        raise ValueError(f"Maksimal {MAX_SISWA_BATCH} siswa per batch.")
    return students


def ekspor_batch_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "nama_siswa", "mata_pelajaran", "tugas", "nomor_soal", "teks_soal",
        "jawaban_siswa", "skor_ai", "nilai_guru", "rincian_rubrik", "bukti_ai", "alasan_ai",
    ])
    meta = st.session_state.get("metadata_batch", {})
    for row in st.session_state.get("hasil_batch", []):
        key = f"nilai_batch_{row['siswa_id']}_{row['nomor']}"
        score = st.session_state.get(key, int(round(row["skor_ai"])))
        writer.writerow([
            row["nama_siswa"], meta.get("mapel", ""), meta.get("nama_tugas", ""),
            row["nomor"], row["soal"], row["jawaban"], row["skor_ai"],
            score, json.dumps(row.get("rincian", []), ensure_ascii=False), row["bukti"], row["alasan"],
        ])
    return output.getvalue()


st.divider()
st.subheader("Penilaian batch satu kelas")
st.caption(
    "Isi soal dan acuan sekali, unggah CSV jawaban beberapa siswa, "
    "revisi nilai, lalu unduh rekap kelas. Batch mendukung jawaban teks."
)
with st.expander("Format CSV dan template", expanded=False):
    st.write(
        "Satu baris untuk satu siswa. Kolom wajib: nama_siswa, jawaban_1, "
        "jawaban_2, dan seterusnya sesuai jumlah soal."
    )
    st.download_button(
        "Unduh template CSV batch",
        data=template_csv_batch(int(jumlah_soal)),
        file_name="template_batch_siswa.csv",
        mime="text/csv",
        key="unduh_template_batch",
    )

col_meta_1, col_meta_2 = st.columns(2)
with col_meta_1:
    guru_batch = st.text_input("Nama guru untuk batch", key="guru_batch")
    mapel_batch = st.text_input("Mata pelajaran batch", key="mapel_batch")
with col_meta_2:
    tugas_batch = st.text_input("Nama tugas atau ulangan batch", key="tugas_batch")
    st.caption(f"Maksimal {MAX_SISWA_BATCH} siswa per unggahan.")

soal_batch = []
for nomor in range(1, int(jumlah_soal) + 1):
    with st.container(border=True):
        st.markdown(f"**Soal dan acuan bersama · nomor {nomor}**")
        col_q, col_rubric = st.columns(2)
        with col_q:
            q_text = st.text_area(f"Teks soal {nomor} untuk batch", key=f"batch_soal_{nomor}", height=120)
        with col_rubric:
            q_ref = st.text_area(f"Kunci atau rubrik {nomor} untuk batch", key=f"batch_acuan_{nomor}", height=120)
        soal_batch.append({"nomor": nomor, "soal": q_text, "acuan": q_ref})

file_batch = st.file_uploader("Unggah CSV jawaban siswa", type=["csv"], key="csv_batch")
if st.button("Proses seluruh batch", type="primary", key="proses_batch"):
    st.session_state.pop("hasil_batch", None)
    st.session_state.pop("metadata_batch", None)
    st.session_state.pop("batch_telah_disimpan", None)
    if not rubrik_valid:
        st.error("Perbaiki rubrik di panel Pengaturan; jumlah bobot harus 100%.")
    elif not guru_batch.strip() or not mapel_batch.strip() or not tugas_batch.strip():
        st.error("Isi nama guru, mata pelajaran, dan nama tugas terlebih dahulu.")
    elif any(not q["soal"].strip() or not q["acuan"].strip() for q in soal_batch):
        st.error("Lengkapi teks soal dan acuan untuk setiap nomor.")
    elif file_batch is None:
        st.error("Unggah CSV jawaban siswa terlebih dahulu.")
    else:
        try:
            students = parse_csv_batch(file_batch, int(jumlah_soal))
            batch_data = {"soal_dan_acuan": soal_batch, "siswa": students}
            prompt_batch = f"""
Nilai jawaban uraian semua siswa dan semua nomor dengan teliti.
Gunakan rubrik berbobot ini untuk menilai setiap jawaban:
{json.dumps(rubrik_aktif, ensure_ascii=False)}
Beri skor 0 sampai 100 untuk setiap komponen dan hitung skor total
sebagai jumlah skor komponen dikali bobotnya dibagi 100.
Terima metode lain yang benar. Jangan mengarang jawaban. Jawaban kosong
dinilai berdasarkan isinya yang kosong. Untuk setiap pasangan siswa dan
nomor soal berikan skor 0 sampai 100, bukti berupa kutipan persis (atau
teks kosong), dan alasan singkat. Kembalikan tiap pasangan tepat satu kali.
Keluarkan JSON saja dengan struktur:
{{
  "hasil": [
    {{"siswa_id": 1, "nomor": 1, "skor": 0, "rincian": [{{"kriteria": "Nama komponen", "skor": 0, "alasan": ""}}], "bukti": "", "alasan": ""}}
  ]
}}
Data:
{json.dumps(batch_data, ensure_ascii=False)}
"""
            batch_key = os.getenv("OPENROUTER_API_KEY")
            if not batch_key:
                st.error("API key belum terbaca. Periksa Codespaces secret.")
            else:
                with st.spinner(f"Menilai {len(students)} siswa dalam satu permintaan..."):
                    response = requests.post(
                        OPENROUTER_URL,
                        headers={"Authorization": f"Bearer {batch_key}", "Content-Type": "application/json"},
                        json={
                            "model": MODEL,
                            "messages": [{"role": "user", "content": prompt_batch}],
                            "temperature": 0.1,
                        },
                        timeout=180,
                    )
                if not response.ok:
                    st.error(f"OpenRouter memberi respons {response.status_code}.")
                    st.code(response.text[:1000])
                else:
                    message_batch = response.json().get("choices", [{}])[0].get("message", {})
                    answer_text = message_batch.get("content")
                    if not isinstance(answer_text, str) or not answer_text.strip():
                        raise ValueError(
                            "OpenRouter mengirim jawaban batch kosong. Coba unggah batch lebih kecil "
                            "atau ulangi setelah beberapa saat."
                        )
                    answer_text = answer_text.strip()
                    lines = answer_text.splitlines()
                    fence = chr(96) * 3
                    if lines and lines[0].strip().startswith(fence):
                        lines = lines[1:]
                    if lines and lines[-1].strip() == fence:
                        lines = lines[:-1]
                    result_rows = json.loads("\n".join(lines))["hasil"]
                    by_student = {item["siswa_id"]: item for item in students}
                    by_question = {item["nomor"]: item for item in soal_batch}
                    parsed = []
                    seen = set()
                    for item in result_rows:
                        sid, q_no = int(item["siswa_id"]), int(item["nomor"])
                        score, rubric_detail = hitung_skor_rubrik(item.get("rincian", []), rubrik_aktif)
                        if sid not in by_student or q_no not in by_question or (sid, q_no) in seen:
                            raise ValueError("Hasil AI memiliki siswa atau nomor soal yang salah/berulang.")
                        if not 0 <= score <= 100:
                            raise ValueError("Skor harus berada antara 0 dan 100.")
                        student, question = by_student[sid], by_question[q_no]
                        parsed.append({
                            "siswa_id": sid,
                            "nama_siswa": student["nama_siswa"],
                            "nomor": q_no,
                            "soal": question["soal"],
                            "acuan": question["acuan"],
                            "jawaban": student["jawaban"][q_no - 1],
                            "skor_ai": score,
                            "rincian": rubric_detail,
                            "bukti": str(item.get("bukti", "")),
                            "alasan": str(item.get("alasan", "")),
                        })
                        seen.add((sid, q_no))
                    if len(seen) != len(students) * int(jumlah_soal):
                        raise ValueError("Ada jawaban yang belum mendapat penilaian.")
                    st.session_state["hasil_batch"] = sorted(parsed, key=lambda x: (x["siswa_id"], x["nomor"]))
                    st.session_state["metadata_batch"] = {
                        "nama_guru": guru_batch.strip(),
                        "mapel": mapel_batch.strip(),
                        "nama_tugas": tugas_batch.strip(),
                    }
                    st.session_state["batch_telah_disimpan"] = False
                    st.success(f"Batch selesai: {len(students)} siswa siap ditinjau.")
        except requests.RequestException as error:
            st.error(f"Permintaan batch ke OpenRouter gagal: {error}")
        except (KeyError, IndexError, TypeError, ValueError) as error:
            st.error(f"Batch belum dapat diproses: {error}")

# Human-in-the-loop: guru merevisi skor tiap jawaban dan menyetujui hasil tiap siswa.
if st.session_state.get("hasil_batch"):
    batch_results = st.session_state["hasil_batch"]
    ids = sorted({item["siswa_id"] for item in batch_results})
    all_approved = True
    scores_by_student = {}
    st.subheader("Tinjau dan revisi nilai batch")
    for sid in ids:
        rows = [item for item in batch_results if item["siswa_id"] == sid]
        name = rows[0]["nama_siswa"]
        st.markdown(f"### {name}")
        student_scores = []
        for row in rows:
            with st.container(border=True):
                info_col, score_col = st.columns([3, 1])
                with info_col:
                    st.markdown(f"**Soal {row['nomor']} · Saran AI {row['skor_ai']:g}/100**")
                    st.write(row["alasan"])
                    st.dataframe(row.get("rincian", []), hide_index=True, width="stretch")
                    with st.expander("Lihat soal, jawaban, dan acuan"):
                        st.markdown("**Soal**")
                        st.text(row["soal"])
                        st.markdown("**Jawaban siswa**")
                        st.text(row["jawaban"])
                        st.markdown("**Acuan**")
                        st.text(row["acuan"])
                    if row["bukti"] and normalkan(row["bukti"]) in normalkan(row["jawaban"]):
                        st.success(f"Kutipan AI ditemukan: “{row['bukti']}”")
                    else:
                        st.warning("Periksa bukti AI terhadap jawaban siswa.")
                with score_col:
                    score = st.number_input(
                        "Revisi nilai guru",
                        min_value=0,
                        max_value=100,
                        value=int(round(row["skor_ai"])),
                        step=1,
                        key=f"nilai_batch_{sid}_{row['nomor']}",
                    )
                student_scores.append(float(score))
        approved = st.checkbox(f"Saya sudah meninjau seluruh nilai {name}", key=f"setuju_batch_{sid}")
        all_approved = all_approved and approved
        scores_by_student[sid] = student_scores

    st.download_button(
        "Unduh rekap batch untuk Excel/e-Rapor (CSV)",
        data=ekspor_batch_csv(),
        file_name="rekap_nilai_kelas.csv",
        mime="text/csv",
        key="unduh_rekap_batch",
    )
    if not st.session_state.get("batch_telah_disimpan", False):
        if not akun_aktif:
            st.info("Mode uji coba: login diperlukan untuk menyimpan batch ke riwayat.")
            if st.button("Masuk atau buat akun untuk menyimpan batch", key="login_batch"):
                st.session_state["auth_mode"] = "masuk"
                st.rerun()
        elif all_approved:
            if st.button("Simpan semua hasil batch", type="primary", key="simpan_batch"):
                try:
                    meta = st.session_state["metadata_batch"]
                    for sid in ids:
                        rows = [item for item in batch_results if item["siswa_id"] == sid]
                        student_meta = {**meta, "nama_siswa": rows[0]["nama_siswa"]}
                        final_scores = scores_by_student[sid]
                        final_average = sum(final_scores) / len(final_scores)
                        simpan_koreksi(
                            akun_aktif["id"], student_meta, rows, final_scores, final_average
                        )
                    st.session_state["batch_telah_disimpan"] = True
                    st.success(f"Semua hasil untuk {len(ids)} siswa berhasil disimpan.")
                except sqlite3.Error as error:
                    st.error(f"Gagal menyimpan batch: {error}")
        else:
            st.warning("Setujui hasil setiap siswa sebelum menyimpan batch.")
    else:
        st.success("Hasil batch sudah tersimpan di akunmu.")


# Fitur riwayat: tampilkan hasil milik akun yang sedang login saja.
st.divider()
st.subheader("Riwayat hasil tersimpan")
if akun_aktif:
    riwayat = ambil_riwayat(akun_aktif["id"])
    if riwayat:
        st.dataframe(riwayat, width="stretch")
    else:
        st.caption("Belum ada hasil tersimpan di akun ini.")
else:
    st.caption("Masuk untuk melihat riwayat hasil yang tersimpan di akunmu.")

