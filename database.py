# Menyimpan dan membaca data koreksi menggunakan SQLite
import sqlite3
from pathlib import Path

# Menempatkan database di folder yang sama dengan file aplikasi
DB_FILE = Path(__file__).with_name("koreksi.db")

# Membuat tabel database jika belum tersedia
def siapkan_database():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS koreksi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nama_guru TEXT NOT NULL,
                mapel TEXT NOT NULL,
                nama_tugas TEXT NOT NULL,
                nama_siswa TEXT NOT NULL,
                nilai_akhir REAL NOT NULL,
                dibuat_pada TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                skor_guru REAL NOT NULL,
                FOREIGN KEY (koreksi_id) REFERENCES koreksi(id)
            )
        """)

# Menyimpan satu lembar koreksi beserta nilai per soalnya
def simpan_koreksi(metadata, hasil, skor_final, nilai_akhir):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        cursor = conn.execute(
            """
            INSERT INTO koreksi (
                nama_guru, mapel, nama_tugas, nama_siswa, nilai_akhir
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
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
                    skor_ai, bukti_ai, alasan_ai, skor_guru
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    id_koreksi,
                    item["nomor"],
                    item["soal"],
                    item["acuan"],
                    item["jawaban"],
                    item["skor_ai"],
                    item["bukti"],
                    item["alasan"],
                    skor,
                ),
            )

    return id_koreksi

# Mengambil 20 hasil koreksi terakhir
def ambil_riwayat():
    with sqlite3.connect(DB_FILE) as conn:
        baris = conn.execute("""
            SELECT id, nama_guru, mapel, nama_tugas, nama_siswa,
                   nilai_akhir, dibuat_pada
            FROM koreksi
            ORDER BY id DESC
            LIMIT 20
        """).fetchall()

    return [
        {
            "ID": row[0],
            "Guru": row[1],
            "Mapel": row[2],
            "Tugas": row[3],
            "Siswa": row[4],
            "Nilai akhir": row[5],
            "Waktu disimpan": row[6],
        }
        for row in baris
    ]