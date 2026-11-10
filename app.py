import json
import os
import re

import requests
import streamlit as st

#Menentukan model AI yang digunakan dan alamat API OpenRouter.
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

#Mengatur judul/tab halaman serta menampilkan judul dan keterangan aplikasi.
st.set_page_config(
    page_title="Asisten Koreksi Uraian",
    page_icon="📝",
)

st.title("Asisten Koreksi Uraian")
st.write(
    "AI memberikan saran penilaian berdasarkan kelogisan penyelesaian. "
    "Guru tetap meninjau dan menetapkan nilai akhir."
)

#Meminta guru memilih banyaknya soal yang akan dinilai. Kode lalu membuat kolom input sebanyak jumlah tersebut.
jumlah_soal = st.number_input(
    "Jumlah soal",
    min_value=1,
    max_value=10,
    value=2,
    step=1,
)

daftar_jawaban = []

#Menampilkan kolom untuk teks soal, kunci/acuan jawaban, dan jawaban siswa. Data tiap soal dikumpulkan ke dalam daftar_jawaban
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

#Berjalan saat tombol diklik. Aplikasi memastikan semua soal, acuan, dan jawaban sudah diisi sebelum mengirimkannya ke AI.
if kirim:
    ada_yang_kosong = any(
        not item["soal"].strip()
        or not item["acuan"].strip()
        or not item["jawaban"].strip()
        for item in daftar_jawaban
    )

    if ada_yang_kosong:
        st.warning("Lengkapi soal, acuan, dan jawaban untuk setiap nomor.")
    else:
        #Mengambil API key dari Codespaces secret. Key tidak ditulis langsung di app.py dan tidak ditampilkan kepada pengguna.
        api_key = os.getenv("OPENROUTER_API_KEY")

        if not api_key:
            st.error("API key belum terbaca. Periksa Codespaces secret.")
        else:
            #Menjelaskan cara AI menilai: fokus pada kelogisan dan ketepatan, menerima metode lain yang benar, dan tidak mengurangi nilai hanya karena langkah rutin dilewati. Bagian ini juga meminta AI mengembalikan skor dan alasan dalam format JSON.
            prompt = f"""
Nilai setiap penyelesaian siswa secara menyeluruh berdasarkan kelogisan
dan ketepatan matematisnya.

Aturan penilaian:
- Jangan mewajibkan urutan langkah yang sama dengan acuan.
- Terima metode lain yang setara dan benar.
- Siswa boleh melewati langkah perantara jika penyelesaian yang ditulis
  tetap cukup jelas untuk menunjukkan penalaran yang benar.
- Jangan mengurangi nilai hanya karena langkah rutin tidak ditulis.
- Kurangi nilai jika ada kesalahan konsep, penalaran tidak valid,
  perhitungan keliru, atau kesimpulan tidak didukung oleh penyelesaian.
- Jika jawaban akhir benar tetapi ada kesalahan kecil, seperti satuan,
  jelaskan kesalahannya dan usulkan pengurangan secara proporsional.
  Jangan otomatis menggugurkan seluruh jawaban karena kesalahan kecil.
- Berikan skor usulan 0 sampai 100 untuk setiap soal.
- Sertakan alasan yang merujuk pada jawaban siswa.
- Jika bukti penalaran tidak cukup untuk memastikan kebenarannya,
  nyatakan bahwa guru perlu meninjau.

Keluarkan JSON saja, tanpa teks tambahan dan tanpa tanda Markdown,
dengan format:
{{
  "hasil": [
    {{
      "nomor": 1,
      "skor": 0,
      "alasan": "Alasan penilaian berdasarkan jawaban siswa"
    }}
  ]
}}

Data soal, acuan, dan jawaban:
{json.dumps(daftar_jawaban, ensure_ascii=False)}
"""

            with st.spinner("Meminta saran penilaian dari AI..."):
                try:
                    #Mengirim semua soal dan jawaban dalam satu permintaan ke model yang dipilih. Karena itu, mengoreksi satu lembar berisi banyak soal tetap memakai satu request API.
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
                        #Mengambil hasil AI, mengubah JSON menjadi data yang bisa dibaca Python, lalu memeriksa apakah semua nomor soal punya skor antara 0 dan 100.
                        isi_ai = response.json()["choices"][0]["message"]["content"]

                        #Menghapus pembungkus Markdown jika AI menambahkannya.
                        isi_ai = re.sub(
                            r"^```(?:json)?\s*|\s*```$",
                            "",
                            isi_ai.strip(),
                        )

                        data_ai = json.loads(isi_ai)
                        hasil = data_ai["hasil"]

                        if not isinstance(hasil, list):
                            raise ValueError("Hasil AI bukan berupa daftar.")

                        if len(hasil) != int(jumlah_soal):
                            raise ValueError("Jumlah hasil AI tidak sesuai jumlah soal.")

                        skor_per_nomor = {}
                        alasan_per_nomor = {}

                        for item in hasil:
                            nomor = int(item["nomor"])
                            skor = float(item["skor"])
                            alasan = str(item["alasan"])

                            if nomor in skor_per_nomor:
                                raise ValueError("AI mengulang nomor soal.")

                            if skor < 0 or skor > 100:
                                raise ValueError("Skor harus berada antara 0 dan 100.")

                            skor_per_nomor[nomor] = skor
                            alasan_per_nomor[nomor] = alasan

                        nomor_diharapkan = set(
                            range(1, int(jumlah_soal) + 1)
                        )

                        if set(skor_per_nomor) != nomor_diharapkan:
                            raise ValueError("Ada nomor soal yang belum dinilai.")

                        st.subheader("Saran penilaian AI")

                        for nomor in range(1, int(jumlah_soal) + 1):
                            skor = skor_per_nomor[nomor]
                            kontribusi = skor / int(jumlah_soal)
                            bobot = 100 / int(jumlah_soal)

                            #Menampilkan skor serta alasan untuk tiap soal. Kode menghitung nilai akhir dengan merata-ratakan skor semua soal, sehingga setiap soal berbobot sama.
                            st.markdown(f"**Soal {nomor}: {skor:g}/100**")
                            st.write(alasan_per_nomor[nomor])
                            st.caption(
                                f"Bobot soal: {bobot:.2f}% · "
                                f"Kontribusi ke nilai akhir: "
                                f"{kontribusi:.2f} poin"
                            )

                        nilai_akhir = (
                            sum(skor_per_nomor.values()) / int(jumlah_soal)
                        )

                        st.metric(
                            "Rata-rata nilai usulan AI",
                            f"{nilai_akhir:.2f}/100",
                        )
                        st.info(
                            "Ini saran AI, bukan nilai final. "
                            "Guru perlu meninjau dan menyetujuinya."
                        )

                #Menampilkan pesan jika koneksi gagal, respons AI tidak sesuai format, atau OpenRouter mengembalikan error.
                except requests.RequestException as error:
                    st.error(f"Permintaan ke OpenRouter gagal: {error}")
                except (KeyError, IndexError, TypeError, ValueError):
                    st.error(
                        "Hasil AI tidak terbaca atau jumlah skornya tidak lengkap. "
                        "Coba kirim ulang."
                    )