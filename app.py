import sqlite3
import csv
import io
import json
import os
import re

import requests
import streamlit as st
from database import ambil_riwayat, siapkan_database, simpan_koreksi

# Menentukan model AI dan alamat API OpenRouter.
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


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


st.set_page_config(
    page_title="Asisten Koreksi Uraian",
    page_icon="📝",
    layout="centered",
)

# Memastikan tabel database siap sebelum aplikasi digunakan
siapkan_database()

# Gaya tampilan: kartu, lencana skor, dan header.
st.markdown(
    """
<style>
.block-container {max-width: 860px; padding-top: 4rem;}
.hero {
    background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
    color: #fff; padding: 1.6rem 1.8rem; border-radius: 18px;
    margin-bottom: 1.2rem;
}
.hero h1 {color:#fff; margin:0; font-size:1.8rem; padding:0;}
.hero p {color:#e0e7ff; margin:.4rem 0 0 0; font-size:.95rem;}
.badge {
    display:inline-block; color:#fff; font-weight:700;
    padding:.25rem .8rem; border-radius:999px; font-size:.9rem;
}
.skor-besar {font-size:2.2rem; font-weight:800; line-height:1;}
.chip-ok {background:#dcfce7; color:#166534; padding:.5rem .8rem;
          border-radius:10px; font-size:.9rem;}
.chip-warn {background:#fef3c7; color:#92400e; padding:.5rem .8rem;
            border-radius:10px; font-size:.9rem;}
.alasan {background:rgba(127,127,127,.1); padding:.7rem .9rem;
         border-radius:10px; font-size:.92rem;}
div[data-testid="stVerticalBlockBorderWrapper"] {border-radius:14px;}
.stButton>button, .stFormSubmitButton>button {
    border-radius:12px; font-weight:600; padding:.6rem 1rem;
}
</style>
<div class="hero">
  <h1>📝 Asisten Koreksi Uraian</h1>
  <p>Asisten AI untuk membantu guru memberikan saran nilai pada ujian uraian.
  Guru tetap perlu meninjau dan menetapkan nilai akhir.</p>
</div>
""",
    unsafe_allow_html=True,
)

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

            daftar_jawaban.append(
                {
                    "nomor": nomor,
                    "soal": soal,
                    "acuan": acuan,
                    "jawaban": jawaban,
                }
            )

    kirim = st.form_submit_button(
        "✨ Minta saran penilaian",
        type="primary",
        use_container_width=True,
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
        or any(
            not item["soal"].strip()
            or not item["acuan"].strip()
            or not item["jawaban"].strip()
            for item in daftar_jawaban
        )
    )

    if ada_yang_kosong:
        st.warning("Lengkapi soal, acuan, dan jawaban untuk setiap nomor.")
    else:
        # Membaca API key dari Codespaces secret.
        api_key = os.getenv("OPENROUTER_API_KEY")

        if not api_key:
            st.error("API key belum terbaca. Periksa Codespaces secret.")
        else:
            # Memberi instruksi penilaian dan format jawaban kepada AI.
            prompt = f"""
Nilai setiap penyelesaian secara menyeluruh berdasarkan kelogisan
dan ketepatan matematisnya.

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
      "bukti": "Kutipan persis dari jawaban siswa",
      "alasan": "Alasan penilaian"
    }}
  ]
}}

Data:
{json.dumps(daftar_jawaban, ensure_ascii=False)}
"""

            with st.spinner("Meminta saran penilaian dari AI..."):
                try:
                    # Mengirim semua soal dan jawaban dalam satu permintaan API.
                    response = requests.post(
                        OPENROUTER_URL,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": MODEL,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": prompt,
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
                        isi_ai = response.json()["choices"][0]["message"]["content"]

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
                            skor = float(item["skor"])
                            bukti = str(item.get("bukti", ""))
                            alasan = str(item["alasan"])

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
                                    "bukti": bukti,
                                    "alasan": alasan,
                                    "jawaban": jawaban_asli,
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

                except (KeyError, IndexError, TypeError, ValueError):
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

            bukti = item["bukti"]
            jawaban_asli = item["jawaban"]

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

            with st.expander("Lihat jawaban asli siswa"):
                st.text(jawaban_asli)

            kol_nilai, kol_setuju = st.columns([1, 2])
            with kol_nilai:
                skor_final = st.number_input(
                    "Nilai final guru",
                    min_value=0,
                    max_value=100,
                    value=skor_awal,
                    step=1,
                    key=f"nilai_guru_{nomor}",
                )
            with kol_setuju:
                st.write("")
                st.write("")
                disetujui = st.checkbox(
                    "Sudah saya tinjau dan setujui",
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

        # Menyimpan hasil setelah guru menyetujui semua soal
        if not st.session_state.get("hasil_telah_disimpan", False):
            if st.button("Simpan hasil ke database", type="primary"):
                try:
                    id_koreksi = simpan_koreksi(
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
            st.success("Hasil ini sudah tersimpan di database.")

        # Unduhan rekap dalam format CSV.
        buffer = io.StringIO()
        penulis = csv.writer(buffer)
        penulis.writerow(["nomor", "skor_ai", "nilai_guru"])
        for item, nilai in zip(hasil, skor_final_semua):
            penulis.writerow([item["nomor"], f"{item['skor_ai']:g}", f"{nilai:g}"])
        penulis.writerow(["rata-rata", "", f"{nilai_akhir:.2f}"])

        st.download_button(
            "⬇️ Unduh rekap (CSV)",
            data=buffer.getvalue(),
            file_name="rekap_nilai.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.metric("Rata-rata sementara", f"{nilai_akhir:.2f}/100")
        st.warning("Setujui nilai setiap soal untuk menetapkan nilai akhir.")

# Menampilkan 20 hasil terakhir dari database
st.divider()
st.subheader("Riwayat hasil tersimpan")
st.dataframe(ambil_riwayat(), use_container_width=True)
