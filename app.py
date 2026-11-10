import json
import os
import re

import requests
import streamlit as st

# Menentukan model AI dan alamat API OpenRouter.
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# Memeriksa kutipan AI di jawaban siswa, dengan mengabaikan spasi
# dan perbedaan huruf besar-kecil.
def normalkan(teks):
    return re.sub(r"\s+", " ", str(teks)).strip().casefold()


# Mengatur judul tab dan halaman aplikasi.
st.set_page_config(
    page_title="Asisten Koreksi Uraian",
    page_icon="📝",
)

st.title("Asisten Koreksi Uraian")
st.write(
    "AI memberikan saran penilaian berdasarkan kelogisan penyelesaian. "
    "Guru tetap meninjau dan menetapkan nilai akhir."
)


# Meminta guru memilih jumlah soal.
jumlah_soal = st.number_input(
    "Jumlah soal",
    min_value=1,
    max_value=10,
    value=2,
    step=1,
)

daftar_jawaban = []


# Membuat kolom input untuk setiap soal, acuan, dan jawaban siswa.
with st.form("form_koreksi"):
    st.subheader("Masukkan soal dan jawaban")

    for nomor in range(1, int(jumlah_soal) + 1):
        st.markdown(f"### Soal {nomor}")

        soal = st.text_area(
            f"Teks soal {nomor}",
            key=f"soal_{nomor}",
        )

        acuan = st.text_area(
            f"Kunci jawaban atau acuan {nomor}",
            key=f"acuan_{nomor}",
            help="Isi dengan jawaban acuan atau informasi yang membantu penilaian.",
        )

        jawaban = st.text_area(
            f"Jawaban siswa untuk soal {nomor}",
            key=f"jawaban_{nomor}",
        )

        daftar_jawaban.append(
            {
                "nomor": nomor,
                "soal": soal,
                "acuan": acuan,
                "jawaban": jawaban,
            }
        )

    kirim = st.form_submit_button("Minta saran penilaian")


# Memeriksa kelengkapan data dan mengirimkannya ke AI setelah tombol diklik.
if kirim:
    st.session_state.pop("hasil_penilaian", None)

    ada_yang_kosong = any(
        not item["soal"].strip()
        or not item["acuan"].strip()
        or not item["jawaban"].strip()
        for item in daftar_jawaban
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

                            jawaban_asli = daftar_jawaban[nomor - 1]["jawaban"]

                            hasil_tersimpan.append(
                                {
                                    "nomor": nomor,
                                    "skor_ai": skor,
                                    "bukti": bukti,
                                    "alasan": alasan,
                                    "jawaban": jawaban_asli,
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
    st.subheader("Tinjau saran penilaian")

    skor_final_semua = []
    semua_disetujui = True

    for item in hasil:
        nomor = item["nomor"]
        skor_awal = int(round(item["skor_ai"]))

        st.markdown(f"### Soal {nomor}")
        st.write(f"**Saran skor AI:** {item['skor_ai']:g}/100")
        st.write(f"**Alasan AI:** {item['alasan']}")

        bukti = item["bukti"]
        jawaban_asli = item["jawaban"]

        if bukti and normalkan(bukti) in normalkan(jawaban_asli):
            st.success(f"Kutipan ditemukan: “{bukti}”")
        else:
            st.warning(
                "Kutipan tidak ditemukan persis di jawaban siswa. "
                "Periksa jawaban asli sebelum menyetujui nilai."
            )

            if bukti:
                st.write(f"**Kutipan AI:** “{bukti}”")

        with st.expander("Lihat jawaban asli siswa"):
            st.text(jawaban_asli)

        skor_final = st.number_input(
            f"Nilai final guru untuk soal {nomor}",
            min_value=0,
            max_value=100,
            value=skor_awal,
            step=1,
            key=f"nilai_guru_{nomor}",
        )

        disetujui = st.checkbox(
            f"Saya sudah meninjau dan menyetujui soal {nomor}",
            key=f"setuju_{nomor}",
        )

        skor_final_semua.append(float(skor_final))

        if not disetujui:
            semua_disetujui = False

    nilai_akhir = sum(skor_final_semua) / len(skor_final_semua)

    if semua_disetujui:
        st.metric(
            "Nilai akhir yang disetujui guru",
            f"{nilai_akhir:.2f}/100",
        )
    else:
        st.metric(
            "Rata-rata sementara",
            f"{nilai_akhir:.2f}/100",
        )
        st.warning("Setujui nilai setiap soal untuk menetapkan nilai akhir.")