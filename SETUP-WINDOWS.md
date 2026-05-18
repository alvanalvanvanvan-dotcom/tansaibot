# Setup tansaibot di Windows (dari nol)

Panduan langkah-demi-langkah supaya kamu bisa clone repo, install dependency,
dan menjalankan bot di Windows 10 / 11.

> **TL;DR**: install Git + Python + buka PowerShell → 6 perintah → bot jalan.

---

## 0. Yang harus dipasang sekali saja

### 0.1. Git for Windows

1. Download dari https://git-scm.com/download/win — file `.exe` (64-bit).
2. Double-click installer, klik **Next** terus pakai default (yang penting opsi
   "Use Git from the Windows Command Prompt" tercentang).
3. Selesai. Buka **PowerShell** (Start → ketik *powershell*) lalu cek:
   ```powershell
   git --version
   ```
   Harus muncul `git version 2.x.x`.

> Bonus: installer-nya juga ikut nge-pasang **Git Bash** — terminal mirip Linux
> di Windows. Banyak yang lebih nyaman pakai itu daripada PowerShell. Bisa dibuka
> dari Start → *Git Bash*. Semua perintah di bawah jalan di kedua terminal.

### 0.2. Python 3.10+ (disarankan 3.12)

1. Download dari https://www.python.org/downloads/windows/ — pilih **Windows
   installer (64-bit)** untuk versi 3.12.x.
2. Jalankan installer. **WAJIB centang** `Add python.exe to PATH` di layar
   pertama, baru klik **Install Now**.
3. Cek di PowerShell:
   ```powershell
   python --version
   pip --version
   ```
   Harus muncul `Python 3.12.x` dan `pip 24.x` (versi pip bisa beda).

### 0.3. (Opsional) GitHub CLI biar push/pull tidak ditanya login terus

Download dari https://cli.github.com/ → install → di PowerShell:

```powershell
gh auth login
```

Pilih `GitHub.com` → `HTTPS` → `Login with a web browser` → ikuti kode di
browser. Sekali setup, selamanya jalan untuk semua `git clone`, `git pull`,
`git push`.

---

## 1. Clone repo (sekali saja)

Pilih folder kerja yang kamu suka — misalnya `Documents`:

```powershell
cd $env:USERPROFILE\Documents
git clone https://github.com/alvanalvanvanvan-dotcom/tansaibot.git
cd tansaibot
```

> Kalau git minta username + password → username = `alvanalvanvanvan-dotcom`,
> password = **Personal Access Token** (bukan password GitHub). Cara bikin PAT:
> https://github.com/settings/tokens/new — centang `repo` scope → generate →
> copy.

Cek branch yang ada:

```powershell
git branch -a
```

Branch utama saat ini:
- `baseline` — versi bot original (sebelum 24 fitur baru)
- `devin/1779055168-refresh-ux-foundation` — PR #1 (UX refresh)
- `devin/1779056691-rate-limit-waitlist` — PR #2 (rate limit + waitlist)
- `devin/1779057125-futuristic` — **PR #3** (paling lengkap, ada voice, inline, follow-up, dll)

Pindah ke versi yang paling lengkap:

```powershell
git checkout devin/1779057125-futuristic
```

---

## 2. Bikin virtualenv & install dependency

Selalu pakai virtualenv supaya package bot tidak mengotori Python global.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

Setelah `Activate.ps1`, prompt PowerShell-mu akan ada awalan `(.venv)`.
Itu artinya virtualenv aktif. Kalau mau keluar, ketik `deactivate`.

> **Kena `PSSecurityException` / "execution of scripts is disabled"?**
> PowerShell defaultnya block menjalankan script. Sekali setup:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> lalu ulangi `Activate.ps1`.

> Pakai **Git Bash** dan lebih suka cara Linux? Aktifkan virtualenv-nya begini:
> ```bash
> source .venv/Scripts/activate
> ```

---

## 3. Konfigurasi `.env`

```powershell
copy .env.example .env
notepad .env
```

Isi minimal yang **wajib**:

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF-bot-token-dari-BotFather
TANS_AI_BASE_URL=http://localhost:20130/api/v1
TANS_AI_API_KEY=tans_xxxxxxxxxxxx
TANS_AI_DEFAULT_MODEL=gpt-4o
```

Cara dapat **Telegram Bot Token**:
1. Buka https://t.me/BotFather di Telegram.
2. Ketik `/newbot` → ikuti panduan (kasih nama bot + username yang berakhiran `bot`).
3. BotFather kasih token (`123456:ABC-DEF-...`) — paste ke `.env`.

Cara dapat **Tans AI API Key**:
- Buka dashboard Tans AI Gateway kamu → halaman API → generate key.
- Pastikan server Tans AI-nya **jalan** di komputer yang sama dengan bot,
  port 20130 (atau ganti `TANS_AI_BASE_URL` di `.env`).

Opsional tapi berguna (lihat `.env.example` untuk daftar lengkap):

```env
# Admin (Telegram user ID kamu - cek lewat @userinfobot)
ADMIN_TELEGRAM_IDS=123456789

# Rate limit (0 = mati)
RATE_LIMIT_PER_MINUTE=30
RATE_LIMIT_PER_DAY=500

# Waitlist mode — user baru harus di-approve admin dulu
WAITLIST_MODE=0

# Voice (Whisper STT + TTS) — biarkan kosong kalau tidak pakai
VOICE_API_KEY=
VOICE_API_BASE=https://api.openai.com/v1

# Smart context summarization (0 = mati, default 24)
SUMMARIZATION_THRESHOLD=24
SUMMARIZATION_KEEP_LAST=10
SUMMARIZATION_DELTA=12

# Follow-up suggestions
FOLLOW_UPS_ENABLED=1
```

Simpan & tutup notepad.

---

## 4. Jalankan bot

Dengan virtualenv masih aktif `(.venv)`:

```powershell
python bot.py
```

Kalau sukses, di terminal akan muncul log seperti:

```
INFO:root:Application started
INFO:root:Bot @namamubot connected
INFO:root:Polling...
```

**Buka Telegram → cari nama bot kamu → kirim `/start`**.
User baru akan diarahkan ke wizard onboarding 3 langkah (bahasa → persona → model).

Stop bot dengan `Ctrl + C`.

---

## 5. Update kode (sekali bot sudah jalan, mau ambil versi terbaru)

```powershell
cd $env:USERPROFILE\Documents\tansaibot
git fetch origin
git checkout devin/1779057125-futuristic   # atau branch lain
git pull
```

Kalau ada update di `requirements.txt`, jalankan lagi:
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## Cheatsheet perintah harian

| Aksi | Perintah |
| ---- | -------- |
| Buka folder proyek | `cd $env:USERPROFILE\Documents\tansaibot` |
| Aktifkan venv (PowerShell) | `.\.venv\Scripts\Activate.ps1` |
| Aktifkan venv (Git Bash) | `source .venv/Scripts/activate` |
| Jalankan bot | `python bot.py` |
| Stop bot | `Ctrl + C` |
| Update ke kode terbaru | `git pull` |
| Ganti ke branch lain | `git checkout <nama-branch>` |
| Lihat semua branch | `git branch -a` |
| Lihat perubahan terakhir | `git log --oneline -10` |
| Lihat status file | `git status` |

---

## Troubleshooting

### "python is not recognized as an internal or external command"
Centang `Add python.exe to PATH` di installer Python (step 0.2) tidak dicentang.
Solusi cepat: jalankan ulang installer Python → pilih **Modify** → centang
"Add Python to environment variables".

### `pip install` gagal dengan error SSL atau timeout
Kemungkinan kamu di balik firewall kantor. Coba:
```powershell
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt
```

### Bot crash dengan "TELEGRAM_BOT_TOKEN belum di-set"
File `.env` belum dibuat / belum diisi. Cek step 3.

### Bot jalan tapi tidak balas
Buka Telegram, cek apakah botmu sudah kamu start dengan `/start`. Kalau iya,
cek log di terminal — kemungkinan ada error koneksi ke Tans AI (server-nya
mati / URL salah / API key salah).

### Voice / inline / follow-up tidak jalan
- **Voice**: butuh `VOICE_API_KEY` di `.env` (key OpenAI Whisper-compatible).
- **Inline mode**: buka BotFather → `/setinline` → pilih bot kamu → set placeholder.
- **Follow-up**: harus `FOLLOW_UPS_ENABLED=1` di `.env`.

### File `chat_history.db` membengkak terus
Itu normal — semua pesan disimpan. Mau bersihkan? Hapus file-nya (saat bot
mati), bot akan auto-bikin yang baru saat di-jalankan lagi. Atau hapus sesi
satu per satu lewat menu History → ⋯ → Delete.

---

## Pertanyaan lanjutan

- Mau jalankan di **Windows Server tanpa GUI / auto-start saat boot**? Pakai
  [NSSM](https://nssm.cc/) untuk daftarkan `python bot.py` sebagai Windows
  Service.
- Mau jalankan di **WSL (Ubuntu di Windows)**? Ikuti panduan di README utama
  (`README.md`) — perintahnya sama persis dengan Linux.
- Mau **deploy** ke VPS / Railway / Fly.io? Repo ini polling-based jadi tidak
  perlu expose port; cukup folder + `.env` + `python bot.py`. PM2 atau systemd
  bisa dipakai untuk supervisor.
