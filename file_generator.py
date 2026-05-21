"""File Generator untuk tansaibot AI Agent.

Mendeteksi permintaan pembuatan file dari pesan user, menginject instruksi
khusus ke prompt AI agar output terstruktur, lalu menghasilkan file nyata
(Excel .xlsx atau kode program .zip/.py/.js dll) siap dikirim sebagai
dokumen Telegram.

Supported outputs:
  - Excel (.xlsx)  — dibuat menggunakan openpyxl dengan styling profesional
  - Kode / Script  — file tunggal (misal .py) atau bundel .zip (multi-file)
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent detection — keyword matching berbasis bahasa Indonesia & Inggris
# ---------------------------------------------------------------------------

_EXCEL_KEYWORDS = [
    "excel", "xlsx", "spreadsheet", "tabel excel", "file excel",
    "buat excel", "buatkan excel", "format excel", "data excel",
    "buat spreadsheet", "buatkan spreadsheet", "buat tabel",
    "buatkan tabel", "dalam excel", "ke excel", "to excel",
    "create excel", "generate excel", "make excel", "make spreadsheet",
]

_CODE_KEYWORDS = [
    "buat program", "buatkan program", "buat script", "buatkan script",
    "buat kode", "buatkan kode", "buat aplikasi", "buatkan aplikasi",
    "buat file kode", "buatkan file kode", "buat fungsi", "buatkan fungsi",
    "buat class", "buatkan class", "buat website", "buatkan website",
    "create program", "create script", "write code", "generate code",
    "write a program", "write script", "write a script", "write function",
    "create function", "create class", "create app", "create application",
]

# Ekstensi file yang dikenal untuk tiap bahasa pemrograman
_LANG_EXT: dict[str, str] = {
    "python": "py", "py": "py",
    "javascript": "js", "js": "js",
    "typescript": "ts", "ts": "ts",
    "html": "html",
    "css": "css",
    "java": "java",
    "cpp": "cpp", "c++": "cpp", "c": "c",
    "csharp": "cs", "cs": "cs", "c#": "cs",
    "go": "go",
    "rust": "rs",
    "php": "php",
    "ruby": "rb", "rb": "rb",
    "bash": "sh", "shell": "sh", "sh": "sh",
    "sql": "sql",
    "r": "r",
    "kotlin": "kt",
    "swift": "swift",
    "dart": "dart",
    "json": "json",
    "yaml": "yaml", "yml": "yaml",
    "xml": "xml",
    "markdown": "md", "md": "md",
    "text": "txt", "txt": "txt",
    "": "txt",
}

# Warna alternating row untuk Excel (dark mode style)
_EXCEL_HEADER_COLOR = "1F6FEB"     # Biru primer
_EXCEL_ROW_ALT_COLOR = "F0F4FF"    # Biru sangat muda (baris genap)
_EXCEL_BORDER_COLOR = "D0D7E0"     # Abu-abu border


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_file_intent(user_message: str) -> Literal["excel", "code", None]:
    """Deteksi apakah pesan user meminta pembuatan file Excel atau kode program."""
    lower = user_message.lower()
    for kw in _EXCEL_KEYWORDS:
        if kw in lower:
            return "excel"
    for kw in _CODE_KEYWORDS:
        if kw in lower:
            return "code"
    return None


def derive_filename(user_message: str, intent: Literal["excel", "code"]) -> str:
    """Buat nama file yang relevan dari pesan user.

    Mengambil kata-kata bermakna dari pesan user untuk dijadikan nama file.
    Contoh: "Buatkan tabel stok barang" → "stok_barang"
    """
    # Buang kata-kata umum / stopwords
    stopwords = {
        "buatkan", "buat", "tolong", "coba", "aku", "saya", "minta",
        "satu", "sebuah", "dengan", "untuk", "yang", "dari", "ke", "di",
        "dalam", "format", "file", "program", "script", "kode", "excel",
        "spreadsheet", "tabel", "aplikasi", "create", "make", "write",
        "generate", "a", "an", "the", "in", "for", "with", "of", "and",
    }
    words = re.findall(r"[a-zA-Z0-9]+", user_message.lower())
    filtered = [w for w in words if w not in stopwords and len(w) > 2]

    # Ambil 3 kata pertama yang bermakna
    name = "_".join(filtered[:3]) if filtered else ("output" if intent == "excel" else "program")

    # Sanitasi untuk filesystem
    name = re.sub(r"[^\w]+", "_", name).strip("_") or "output"
    return name[:40]  # Batasi panjang nama


def get_excel_system_prompt(user_message: str) -> str:
    """Kembalikan instruksi sistem untuk meminta AI menghasilkan output Excel-friendly."""
    return (
        "\n\n[INSTRUKSI PENTING — FILE EXCEL]\n"
        "User meminta data dalam format Excel. Kamu HARUS memformat respons sebagai:\n"
        "1. Buat tabel Markdown yang lengkap dan akurat dengan format:\n"
        "   | Kolom1 | Kolom2 | Kolom3 |\n"
        "   |--------|--------|--------|\n"
        "   | Data1  | Data2  | Data3  |\n"
        "2. Pastikan kolom dan data sesuai persis dengan permintaan user.\n"
        "3. Sertakan setidaknya 5-10 baris data yang realistis dan relevan.\n"
        "4. Setelah tabel, berikan penjelasan singkat tentang isi file.\n"
        "5. Jangan sertakan kode Python/script — cukup tabel Markdown.\n"
        f"Konteks permintaan: {user_message[:300]}"
    )


def get_code_system_prompt(user_message: str) -> str:
    """Kembalikan instruksi sistem untuk meminta AI menghasilkan kode yang bersih."""
    return (
        "\n\n[INSTRUKSI PENTING — FILE KODE]\n"
        "User meminta kode program. Kamu HARUS:\n"
        "1. Tulis kode yang LENGKAP, BERFUNGSI, dan SIAP DIJALANKAN — bukan pseudocode.\n"
        "2. Selalu bungkus kode dengan fenced code block yang benar:\n"
        "   ```python\n   # kode di sini\n   ```\n"
        "3. Sertakan komentar/dokumentasi singkat dalam kode.\n"
        "4. Pastikan semua import/require sudah disertakan.\n"
        "5. Jika ada beberapa file, bungkus masing-masing dalam code block terpisah "
        "dengan nama file di komentar pertama (misal: # File: main.py).\n"
        "6. Setelah kode, berikan instruksi singkat cara menjalankan program.\n"
        f"Konteks permintaan: {user_message[:300]}"
    )


def extract_code_blocks(ai_reply: str) -> list[tuple[str, str, str]]:
    """Ekstrak semua blok kode dari reply AI (markdown fenced code blocks).

    Returns list of (language, filename_hint, code) tuples.
    filename_hint diambil dari komentar '# File: ...' atau '// File: ...' jika ada.
    """
    pattern = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
    blocks: list[tuple[str, str, str]] = []
    for match in pattern.finditer(ai_reply):
        lang = match.group(1).strip().lower()
        code = match.group(2).strip()
        if not code:
            continue

        # Cari filename hint di komentar pertama
        filename_hint = ""
        first_line = code.split("\n")[0].strip()
        for prefix in ("# File:", "// File:", "/* File:", "-- File:", "' File:"):
            if first_line.lower().startswith(prefix.lower()):
                raw_hint = first_line[len(prefix):].strip().rstrip("*/").strip()
                # Sanitasi nama file
                filename_hint = re.sub(r"[^\w.\-]", "_", raw_hint)[:50]
                break

        blocks.append((lang, filename_hint, code))
    return blocks


async def build_excel_from_ai(ai_reply: str, filename: str = "output") -> io.BytesIO | None:
    """Buat file Excel (.xlsx) dengan styling profesional dari reply AI.

    Mencoba parse tabel Markdown dari reply. Jika tidak ada tabel,
    menulis konten AI baris per baris.

    Returns BytesIO buffer siap kirim, atau None jika gagal.
    """
    try:
        import openpyxl
        from openpyxl.styles import (
            Alignment, Border, Font, PatternFill, Side,
        )

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = (filename[:31] or "Sheet1")

        # Style definitions
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill("solid", fgColor=_EXCEL_HEADER_COLOR)
        header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

        alt_fill = PatternFill("solid", fgColor=_EXCEL_ROW_ALT_COLOR)
        normal_align = Alignment(vertical="center", wrap_text=True)

        thin_side = Side(style="thin", color=_EXCEL_BORDER_COLOR)
        cell_border = Border(
            left=thin_side, right=thin_side,
            top=thin_side, bottom=thin_side,
        )

        # Parse tabel Markdown
        rows = _parse_markdown_table(ai_reply)

        if rows:
            ws.freeze_panes = "A2"  # Freeze header row
            for row_idx, row in enumerate(rows, start=1):
                is_header = (row_idx == 1)
                ws.row_dimensions[row_idx].height = 20 if is_header else 18

                for col_idx, cell_val in enumerate(row, start=1):
                    cell = ws.cell(row=row_idx, column=col_idx, value=cell_val)
                    cell.border = cell_border
                    if is_header:
                        cell.font = header_font
                        cell.fill = header_fill
                        cell.alignment = header_align
                    else:
                        cell.alignment = normal_align
                        if row_idx % 2 == 0:
                            cell.fill = alt_fill

            # Auto-fit column widths (berdasarkan konten)
            for col in ws.columns:
                max_len = 10
                col_letter = col[0].column_letter
                for cell in col:
                    try:
                        val_len = len(str(cell.value or ""))
                        if val_len > max_len:
                            max_len = val_len
                    except Exception:
                        pass
                ws.column_dimensions[col_letter].width = min(max_len + 4, 55)

        else:
            # Fallback: tulis teks baris per baris
            ws["A1"] = f"Data: {filename}"
            ws["A1"].font = Font(bold=True, color="FFFFFF", size=12)
            ws["A1"].fill = PatternFill("solid", fgColor=_EXCEL_HEADER_COLOR)
            ws["A1"].alignment = Alignment(horizontal="center")
            ws.column_dimensions["A"].width = 80

            lines = [ln.strip() for ln in ai_reply.split("\n") if ln.strip()]
            for i, line in enumerate(lines, start=2):
                clean = re.sub(r"[*_`#>]+", "", line).strip()
                if clean:
                    cell = ws.cell(row=i, column=1, value=clean)
                    cell.alignment = Alignment(wrap_text=True)
                    if i % 2 == 0:
                        cell.fill = alt_fill

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf

    except ImportError:
        logger.warning(
            "openpyxl tidak terinstall. Jalankan: pip install openpyxl"
        )
        return None
    except Exception as exc:
        logger.exception("Gagal membuat file Excel: %s", exc)
        return None


async def build_code_file(
    ai_reply: str,
    filename: str = "program",
) -> tuple[io.BytesIO, str] | None:
    """Buat file kode atau ZIP dari blok kode yang ada di reply AI.

    - 0 blok  → None (tidak ada yang dikirim)
    - 1 blok  → file tunggal (misal: program.py)
    - >1 blok → semua file dibundel menjadi program.zip

    Returns (BytesIO buffer, nama_file_dengan_ekstensi) atau None.
    """
    try:
        blocks = extract_code_blocks(ai_reply)
        if not blocks:
            return None

        if len(blocks) == 1:
            lang, fname_hint, code = blocks[0]
            ext = _LANG_EXT.get(lang, "txt")
            # Gunakan filename hint jika tersedia, else gunakan context filename
            out_filename = fname_hint if fname_hint else f"{filename}.{ext}"
            buf = io.BytesIO(code.encode("utf-8"))
            buf.seek(0)
            return buf, out_filename

        # Multiple blocks → ZIP
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            seen_names: dict[str, int] = {}

            for lang, fname_hint, code in blocks:
                ext = _LANG_EXT.get(lang, "txt")

                if fname_hint:
                    entry_name = fname_hint
                else:
                    base = f"file.{ext}"
                    count = seen_names.get(base, 0)
                    if count == 0:
                        entry_name = base
                    else:
                        name_part = base.rsplit(".", 1)[0]
                        entry_name = f"{name_part}_{count}.{ext}"
                    seen_names[base] = count + 1

                # Pastikan tidak ada duplikat nama di ZIP
                if entry_name in seen_names:
                    name_part, _, ext_part = entry_name.rpartition(".")
                    entry_name = f"{name_part}_{seen_names.get(entry_name, 1)}.{ext_part}"
                seen_names[entry_name] = seen_names.get(entry_name, 0) + 1

                zf.writestr(entry_name, code.encode("utf-8"))

            # Tambahkan README.md berisi instruksi dari AI
            readme_content = _extract_readme_content(ai_reply)
            if readme_content:
                zf.writestr("README.md", readme_content.encode("utf-8"))

        zip_buf.seek(0)
        return zip_buf, f"{filename}.zip"

    except Exception as exc:
        logger.exception("Gagal membuat file kode: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_markdown_table(text: str) -> list[list[str]]:
    """Parse tabel Markdown dari teks.

    Returns list of rows (list of strings).
    Baris separator (---) dibuang. Baris pertama adalah header.
    """
    rows: list[list[str]] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        # Pisahkan sel berdasarkan pipe
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Buang baris separator
        if all(re.match(r"^[-:]+$", c) for c in cells if c):
            continue
        # Buang sel kosong di awal/akhir yang merupakan artefak parsing
        while cells and cells[0] == "":
            cells.pop(0)
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            rows.append(cells)
    return rows


def _extract_readme_content(ai_reply: str) -> str:
    """Ekstrak teks non-kode dari reply AI untuk dijadikan README.md."""
    # Hapus semua fenced code block
    clean = re.sub(r"```[\w]*\n.*?```", "", ai_reply, flags=re.DOTALL)
    # Hapus markdown formatting berat
    clean = re.sub(r"#{1,6}\s", "", clean)
    clean = clean.strip()
    lines = [ln for ln in clean.split("\n") if ln.strip()]
    if not lines:
        return ""
    return "# README\n\n" + "\n".join(lines)
