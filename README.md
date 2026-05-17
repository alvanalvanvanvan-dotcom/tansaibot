# Telegram Tans AI Bot

Bot Telegram yang meneruskan pesan pengguna ke **Tans AI API Gateway**
(`http://localhost:20130/api/v1`) dan membalas dengan respons AI. Mendukung
ganti model, daftar model, dan riwayat percakapan per pengguna.

## Fitur

- **Tombol persistent** di bawah area ketik — `New Chat`, `History`, `Pilih Model`, `Status`, `Bantuan`, `Reset Chat`. Tap langsung, tidak perlu ngetik command.
- **New Chat (📝)** — mulai sesi percakapan baru kosong. Sesi sebelumnya tetap tersimpan di History.
- **History (📚)** — daftar riwayat percakapan kamu sebagai tombol inline (judul = pesan pertama kamu, plus tanggal). Tap untuk melanjutkan sesi lama — AI akan tetap dapat konteks dari pesan-pesan sebelumnya.
- **Persisten ke SQLite** — semua sesi dan pesan disimpan di `chat_history.db`. Riwayat tidak hilang walau bot di-restart.
- **Inline keyboard** saat pilih model: tap nama model untuk langsung ganti (ada centang ✅ di model aktif).
- Chat dasar: kirim pesan biasa → AI balas dengan konteks sesi aktif.
- `/start`, `/help` — sapaan dan bantuan (juga tampilkan ulang tombol).
- `/status` — cek koneksi & validitas API key (via `GET /api/v1/status`).
- `/models` — daftar model AI yang tersedia (via `GET /api/v1/models`).
- `/model <nama>` — ganti model aktif via teks (alternatif tombol).
- `/new` — alias `New Chat`.
- `/history` — alias `History`.
- `/reset` — hapus sesi aktif sekarang (juga hilang dari History).
- **Context memory per sesi**: 20 pesan terakhir dikirim ke AI sebagai konteks (bisa diubah lewat `HISTORY_MAX_MESSAGES`).

## Prasyarat

- Python **3.10+**
- Server Tans AI aktif di `http://localhost:20130` di mesin yang sama dengan bot.
- Token bot Telegram dari [@BotFather](https://t.me/BotFather).
- API Key Tans AI (format `tans_...`) dari halaman API Gateway.

## Setup

```bash
# 1. Clone / pindah ke folder proyek
cd telegram-tans-ai-bot

# 2. Buat virtualenv (opsional tapi disarankan)
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Salin template env dan isi nilainya
cp .env.example .env
# Edit .env, isi TELEGRAM_BOT_TOKEN dan TANS_AI_API_KEY
```

Isi `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF-your-token-here
TANS_AI_BASE_URL=http://localhost:20130/api/v1
TANS_AI_API_KEY=tans_your_api_key_here
TANS_AI_DEFAULT_MODEL=gpt-4o
HISTORY_MAX_MESSAGES=20
TANS_AI_TIMEOUT=60
```

## Menjalankan

Pastikan server Tans AI sudah jalan di `localhost:20130`, lalu:

```bash
python bot.py
```

Bot akan menggunakan **long polling**, jadi tidak perlu expose port apa pun
ke internet. Buka Telegram, cari bot kamu, lalu kirim `/start`.

## Cara pakai (dari Telegram)

### Lewat tombol (persistent reply keyboard)

Setelah `/start`, akan muncul tombol di bawah area ketik:

| Tombol | Aksi |
|---|---|
| 📝 New Chat | Mulai sesi percakapan baru kosong (sesi lama tetap di History) |
| 📚 History | Lihat & lanjutkan riwayat percakapan |
| 🧠 Pilih Model | Tampilkan daftar model (lalu tap tombol model untuk ganti) |
| 🔌 Status | Cek koneksi & validitas API key ke Tans AI |
| ℹ️ Bantuan | Tampilkan daftar tombol/perintah |
| 🧹 Reset Chat | Hapus sesi aktif sekarang (juga hilang dari History) |

### Lewat command (alternatif teks)

| Perintah | Keterangan |
|---|---|
| `/start` | Tampilkan ulang tombol + lihat model aktif |
| `/help` | Daftar tombol & perintah |
| `/status` | Cek koneksi & validitas API key ke Tans AI |
| `/models` | Tampilkan daftar model dari Tans AI |
| `/model gpt-4o` | Ganti model aktif kamu ke `gpt-4o` |
| `/model` | Tampilkan model aktif kamu saat ini |
| `/new` | Mulai sesi percakapan baru |
| `/history` | Lihat riwayat percakapan |
| `/reset` | Hapus sesi aktif sekarang |
| (pesan biasa) | Kirim ke AI dan terima balasan |

### Bagaimana session/history bekerja

- Setiap pesan kamu masuk ke "sesi aktif". Kalau belum ada, sesi baru otomatis dibuat.
- Judul sesi diambil dari pesan pertama kamu (dipotong ke 40 karakter).
- Tap **📝 New Chat** → buat sesi baru kosong, sesi lama tetap di History.
- Tap **📚 History** → daftar sesi (latest first, max 20). Tap salah satu untuk lanjutkan.
- Saat lanjut sesi lama, AI tetap dapat konteks dari `HISTORY_MAX_MESSAGES` pesan terakhir.
- Tap **🧹 Reset Chat** → hapus sesi aktif sekarang (juga hilang dari History).
- Sesi tersimpan di `chat_history.db` (SQLite, di folder bot). Tidak hilang saat bot restart.

## Catatan teknis

- Endpoint `/api/v1/chat` Tans AI menerima `{"message": "...", "model": "..."}`
  sebagai body. Bot ini menyusun riwayat percakapan menjadi satu prompt
  bergaya `User: ...\nAssistant: ...` agar AI tetap dapat konteks meskipun
  endpoint hanya menerima satu field `message`.
- Riwayat disimpan di memori proses (RAM). Kalau bot di-restart, riwayat
  hilang. Untuk persistence, ganti `USER_HISTORY` dengan storage seperti
  SQLite/Redis.
- Parser response cukup permisif (mendukung field `reply`, `response`,
  `message`, `content`, `choices[0].message.content`, dll). Kalau Tans AI
  pakai format berbeda, sesuaikan `TansAIClient._extract_reply` di
  `tans_client.py`.

## Troubleshooting

- **"Tidak bisa terhubung ke Tans AI"** — pastikan server Tans AI jalan dan
  `TANS_AI_BASE_URL` benar. Tes manual:
  ```bash
  curl -X POST http://localhost:20130/api/v1/chat \
    -H "Authorization: Bearer $TANS_AI_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"message":"Hi","model":"gpt-4o"}'
  ```
- **"HTTP 401 / 403"** — API key salah atau sudah di-revoke. Generate ulang
  dari halaman API Gateway Tans AI.
- **"Format response Tans AI tidak dikenali"** — kirim contoh response
  mentah, lalu sesuaikan `_extract_reply` di `tans_client.py`.

## Struktur file

```
telegram-tans-ai-bot/
├── bot.py            # Entry point + Telegram handlers
├── tans_client.py    # HTTP client untuk Tans AI API
├── db.py             # SQLite helpers (sessions + messages)
├── chat_history.db   # SQLite DB (auto-dibuat saat run pertama; di-gitignore)
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```
