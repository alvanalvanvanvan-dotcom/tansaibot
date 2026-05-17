# Telegram Tans AI Bot

Bot Telegram yang meneruskan pesan pengguna ke **Tans AI API Gateway**
(`http://localhost:20130/api/v1`) dan membalas dengan respons AI. Mendukung
ganti model, persona, riwayat percakapan, quick prompt, dan banyak fitur
quality-of-life lain — semuanya bisa diakses lewat tombol di area ketik.

## Fitur

### Inti
- **Tombol persistent** di bawah area ketik: `New Chat`, `History`, `Quick Prompt`,
  `Persona`, `Model`, `Status`, `Settings`, `Bantuan`, `Reset Chat`.
- **Chat dasar**: kirim pesan biasa → AI balas dengan konteks sesi aktif.
- **Persisten ke SQLite**: semua sesi, pesan, dan preferensi user disimpan di
  `chat_history.db`. Tidak hilang saat bot di-restart.
- **Context memory per sesi**: 20 pesan terakhir dikirim ke AI sebagai konteks
  (atur lewat `HISTORY_MAX_MESSAGES`).

### Refresh UX
- **Streaming feel** — placeholder dengan animasi titik berjalan saat menunggu
  jawaban AI, lalu di-edit ke jawaban final. Tidak butuh perubahan API.
- **Markdown rendering** — `**bold**`, `*italic*`, `code`, code blocks, link,
  bullet list, dan heading dirender rapi di Telegram (HTML mode).
- **Tombol Regenerate** muncul di bawah tiap jawaban AI — sekali tap, jawaban
  digenerate ulang dari pertanyaan yang sama.
- **Auto-title cerdas** — setelah pertukaran pertama selesai, judul sesi di-set
  otomatis dengan minta AI buatkan ringkasan 3–6 kata (bisa dimatikan via
  `AUTO_TITLE=0`).

### Persona & Quick Prompt
- **Persona / Mode AI** — pilih gaya jawaban: Default, Coder, Writer, Translator,
  Tutor, Analyst, atau Custom (tulis system prompt sendiri). System prompt
  preset di-prepend otomatis ke tiap pesan.
- **Quick Prompt templates** — tap `⚡ Quick Prompt` lalu pilih template
  (Translate, Summarize, Explain Code, Brainstorm, Bantu Balas, Perbaiki Tulisan).
  Bot tanya isi → langsung jalankan prompt yang sudah dioptimasi.

### Manajemen Sesi & History
- **History dengan pagination** — 8 sesi per halaman, navigasi `⬅️ Prev / Next ➡️`.
- **Menu per sesi (⋯)** — Rename, Pin, Archive, Export, Delete. Sesi yang di-pin
  selalu nongol di atas.
- **Pencarian** — `/find <kata>` atau tombol `🔍 Cari` cari berdasarkan judul
  atau isi pesan.
- **Export sesi** ke file Markdown (`.md`) lewat tombol Export.

### Preferences & Onboarding
- **Onboarding wizard** otomatis untuk user baru — pilih bahasa, persona, dan
  model default dalam 3 langkah.
- **Settings panel** lewat `/settings` atau tombol ⚙️ Settings: model, persona,
  bahasa UI, custom prompt, statistik usage.
- **Multi-bahasa UI** (Indonesia / English) — beberapa label menyesuaikan
  bahasa user.

### Stats & Admin
- **`/stats`** — sesi, jumlah pesan diproses, estimasi token in/out per user.
- **Admin commands** (set `ADMIN_TELEGRAM_IDS` di `.env`):
  - `/admin` — stats global (users, sessions, messages).
  - `/broadcast <pesan>` — kirim pengumuman ke semua user yang pernah pakai bot.

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

Isi `.env` (lihat `.env.example` untuk daftar lengkap):

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF-your-token-here
TANS_AI_BASE_URL=http://localhost:20130/api/v1
TANS_AI_API_KEY=tans_your_api_key_here
TANS_AI_DEFAULT_MODEL=gpt-4o
HISTORY_MAX_MESSAGES=20
TANS_AI_TIMEOUT=60
AUTO_TITLE=1
# ADMIN_TELEGRAM_IDS=123456789
```

## Menjalankan

```bash
python bot.py
```

Bot menggunakan **long polling**, jadi tidak perlu expose port apa pun. Buka
Telegram, cari bot kamu, kirim `/start`. User baru akan dipandu lewat
onboarding wizard (3 langkah).

## Daftar Command

| Perintah | Keterangan |
|---|---|
| `/start` | Tampilkan ulang keyboard, lihat status pengaturan |
| `/help` | Daftar tombol & perintah |
| `/new` | Mulai sesi baru |
| `/history` | Lihat riwayat (dengan pagination, pin, rename, dll) |
| `/quick` | Pilih template quick prompt |
| `/persona` | Ganti gaya jawaban AI |
| `/models` | Daftar model + ganti via tombol |
| `/model <nama>` | Ganti model lewat teks |
| `/settings` | Panel preferences |
| `/find <kata>` / `/search <kata>` | Cari di riwayat |
| `/export` | Export sesi aktif sebagai file `.md` |
| `/stats` | Statistik penggunaan kamu |
| `/status` | Cek koneksi ke Tans AI |
| `/reset` | Hapus sesi aktif |
| `/cancel` | Batalkan aksi pending (rename, quick prompt input, dll) |
| `/admin` | (admin) Stats global |
| `/broadcast <msg>` | (admin) Kirim pengumuman ke semua user |

## Cara kerja history & persona

- Setiap pesan masuk ke "sesi aktif". Kalau belum ada, sesi baru otomatis dibuat.
- Judul sesi awalnya dari pesan pertama, lalu di-replace otomatis oleh AI
  (kalau `AUTO_TITLE=1`).
- Tap **📝 New Chat** → buat sesi baru, sesi lama tetap di History.
- Tap **📚 History** → lihat sesi, lalu **⋯** untuk menu rename/pin/archive/export.
- Tap **🎭 Persona** → ganti gaya AI (Coder, Writer, Translator, dll). Persona
  per-user di-default, persona per-session juga di-set saat sesi dibuat.
- **🧹 Reset Chat** → hapus sesi aktif sekarang (juga hilang dari History).
- Semua data tersimpan di `chat_history.db` (SQLite, di folder bot).

## Catatan teknis

- Endpoint `/api/v1/chat` Tans AI menerima `{"message": "...", "model": "..."}`
  sebagai body. Bot menyusun riwayat percakapan menjadi satu prompt
  bergaya `System: ...\nUser: ...\nAssistant: ...` agar AI tetap dapat konteks
  dan persona meskipun endpoint hanya menerima satu field `message`.
- Parser response permisif (mendukung field `reply`, `response`, `message`,
  `content`, `choices[0].message.content`, dll).
- Token tracker memakai estimasi cepat (`len(text) // 4`), bukan tokenizer
  resmi — angkanya cuma indikatif.

## Troubleshooting

- **"Tidak bisa terhubung ke Tans AI"** — pastikan server Tans AI jalan dan
  `TANS_AI_BASE_URL` benar. Tes manual:
  ```bash
  curl -X POST http://localhost:20130/api/v1/chat \
    -H "Authorization: Bearer $TANS_AI_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"message":"Hi","model":"gpt-4o"}'
  ```
- **"HTTP 401 / 403"** — API key salah atau sudah di-revoke. Generate ulang.
- **"Format response Tans AI tidak dikenali"** — kirim contoh response mentah,
  lalu sesuaikan `_extract_reply` di `tans_client.py`.

## Struktur file

```
telegram-tans-ai-bot/
├── bot.py              # Entry point + Telegram handlers
├── tans_client.py      # HTTP client untuk Tans AI API
├── db.py               # SQLite helpers (sessions, messages, prefs, usage)
├── ui.py               # Keyboard builders + callback prefixes
├── personas.py         # System prompt presets
├── quick_prompts.py    # Quick-prompt templates
├── streaming.py        # "Typing animation" placeholder helper
├── markdown_utils.py   # Markdown → Telegram HTML converter
├── chat_history.db     # SQLite DB (auto-dibuat saat run pertama; di-gitignore)
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```
