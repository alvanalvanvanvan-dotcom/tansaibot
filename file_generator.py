"""File Generator untuk tansaibot AI Agent.

Mendeteksi permintaan pembuatan file dari pesan user, menginject instruksi
ke prompt AI agar output terstruktur, lalu menghasilkan dan memvalidasi
file nyata siap dikirim sebagai dokumen Telegram.

Rules:
  - Excel: satu file .xlsx dengan tabel dari reply AI
  - Kode:  satu atau lebih file individual, nama diambil langsung dari
           komentar pertama kode (misal # File: index.html).
           Dikirim per file — BUKAN zip.
"""
from __future__ import annotations

import ast
import io
import logging
import re
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword detection
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
    "buat halaman", "buatkan halaman", "buat index", "buatkan index",
    "create program", "create script", "write code", "generate code",
    "write a program", "write script", "write a script", "write function",
    "create function", "create class", "create app", "create application",
    "buat html", "buatkan html", "buat css", "buat javascript",
    "buatkan javascript", "buat js", "buatkan js",
]

# Language → file extension mapping
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

# Comment styles per language (for filename hint detection)
_COMMENT_PREFIXES = [
    "# File:", "// File:", "<!-- File:", "/* File:",
    "-- File:", "' File:", "# filename:", "// filename:",
]

# Excel styling
_EXCEL_HEADER_COLOR = "1F6FEB"
_EXCEL_ROW_ALT_COLOR = "EEF4FF"
_EXCEL_BORDER_COLOR = "D0D7E0"


# ---------------------------------------------------------------------------
# Public — Intent detection
# ---------------------------------------------------------------------------

def detect_file_intent(user_message: str) -> Literal["excel", "code", None]:
    """Deteksi apakah user meminta file Excel atau kode program."""
    lower = user_message.lower()
    for kw in _EXCEL_KEYWORDS:
        if kw in lower:
            return "excel"
    for kw in _CODE_KEYWORDS:
        if kw in lower:
            return "code"
    return None


def derive_filename(user_message: str, intent: Literal["excel", "code"]) -> str:
    """Buat nama file yang relevan dari pesan user (tanpa ekstensi)."""
    stopwords = {
        "buatkan", "buat", "tolong", "coba", "aku", "saya", "minta",
        "sebuah", "dengan", "untuk", "yang", "dari", "ke", "di",
        "dalam", "format", "file", "program", "script", "kode", "excel",
        "spreadsheet", "tabel", "aplikasi", "create", "make", "write",
        "generate", "a", "an", "the", "in", "for", "with", "of", "and",
        "html", "css", "javascript", "python", "js", "halaman", "website",
    }
    words = re.findall(r"[a-zA-Z0-9]+", user_message.lower())
    filtered = [w for w in words if w not in stopwords and len(w) > 2]
    name = "_".join(filtered[:3]) if filtered else ("output" if intent == "excel" else "program")
    name = re.sub(r"[^\w]+", "_", name).strip("_") or "output"
    return name[:40]


# ---------------------------------------------------------------------------
# Public — AI prompt injections
# ---------------------------------------------------------------------------

def get_excel_system_prompt(user_message: str) -> str:
    """Instruksi sistem agar AI menghasilkan tabel Markdown yang valid untuk Excel."""
    return (
        "\n\n[INSTRUKSI WAJIB — OUTPUT EXCEL]\n"
        "User meminta data dalam format Excel. Kamu HARUS:\n"
        "1. Buat tabel Markdown yang LENGKAP dan AKURAT:\n"
        "   | Kolom1 | Kolom2 | Kolom3 |\n"
        "   |--------|--------|--------|\n"
        "   | Data1  | Data2  | Data3  |\n"
        "2. Pastikan kolom dan baris sesuai PERSIS dengan permintaan user.\n"
        "3. Sertakan minimal 5 baris data yang realistis dan relevan.\n"
        "4. JANGAN sertakan blok kode (```python dll) — cukup tabel Markdown.\n"
        "5. Setelah tabel, beri penjelasan singkat tentang isi file.\n"
        f"Permintaan user: {user_message[:400]}"
    )


def get_code_system_prompt(user_message: str) -> str:
    """Instruksi sistem agar AI menghasilkan kode yang bersih dan siap pakai."""
    return (
        "\n\n[INSTRUKSI WAJIB — OUTPUT KODE PROGRAM]\n"
        "User meminta kode program. Kamu HARUS mematuhi aturan berikut:\n"
        "1. Tulis kode yang LENGKAP, BENAR, dan SIAP DIJALANKAN — bukan pseudocode.\n"
        "2. WAJIB sertakan nama file di baris PERTAMA sebagai komentar:\n"
        "   - Python : # File: nama_file.py\n"
        "   - HTML   : <!-- File: index.html -->\n"
        "   - JS/TS  : // File: script.js\n"
        "   - CSS    : /* File: style.css */\n"
        "   - Java   : // File: Main.java\n"
        "3. Bungkus setiap file dalam fenced code block dengan bahasa yang benar:\n"
        "   ```python\n   # File: nama.py\n   # kode di sini\n   ```\n"
        "4. Sertakan SEMUA import/require yang dibutuhkan.\n"
        "5. Tambahkan komentar/dokumentasi minimal di setiap fungsi utama.\n"
        "6. Jika ada beberapa file, tulis MASING-MASING dalam code block TERPISAH.\n"
        "7. Setelah semua kode, berikan cara singkat menjalankan program.\n"
        f"Permintaan user: {user_message[:400]}"
    )


# ---------------------------------------------------------------------------
# Public — File builders
# ---------------------------------------------------------------------------

async def build_excel_from_ai(ai_reply: str, filename: str = "output") -> io.BytesIO | None:
    """Buat file Excel .xlsx dengan styling profesional dari reply AI."""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = (filename[:31] or "Sheet1")

        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill("solid", fgColor=_EXCEL_HEADER_COLOR)
        header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
        alt_fill = PatternFill("solid", fgColor=_EXCEL_ROW_ALT_COLOR)
        normal_align = Alignment(vertical="center", wrap_text=True)
        thin = Side(style="thin", color=_EXCEL_BORDER_COLOR)
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        rows = _parse_markdown_table(ai_reply)

        if rows:
            ws.freeze_panes = "A2"
            for row_idx, row in enumerate(rows, start=1):
                is_header = (row_idx == 1)
                ws.row_dimensions[row_idx].height = 22 if is_header else 18
                for col_idx, val in enumerate(row, start=1):
                    cell = ws.cell(row=row_idx, column=col_idx, value=val)
                    cell.border = border
                    if is_header:
                        cell.font = header_font
                        cell.fill = header_fill
                        cell.alignment = header_align
                    else:
                        cell.alignment = normal_align
                        if row_idx % 2 == 0:
                            cell.fill = alt_fill

            # Auto-fit column widths
            for col in ws.columns:
                max_len = 10
                col_letter = col[0].column_letter
                for cell in col:
                    try:
                        length = len(str(cell.value or ""))
                        if length > max_len:
                            max_len = length
                    except Exception:
                        pass
                ws.column_dimensions[col_letter].width = min(max_len + 4, 55)
        else:
            # Fallback: tulis konten teks baris per baris
            ws["A1"] = filename
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
        logger.warning("openpyxl tidak terinstall. Jalankan: pip install openpyxl")
        return None
    except Exception as exc:
        logger.exception("Gagal membuat file Excel: %s", exc)
        return None


async def build_code_files(
    ai_reply: str,
    fallback_name: str = "program",
) -> list[tuple[io.BytesIO, str, list[str]]]:
    """Buat dan validasi file kode dari reply AI.

    Mengembalikan list of (buffer, filename, warnings).
    Setiap file dikirim sebagai dokumen terpisah — BUKAN zip.
    Filename diambil dari komentar '# File: ...' dalam kode.
    Jika tidak ada hint, dibuat dari bahasa + fallback_name.

    Returns [] jika tidak ada blok kode valid.
    """
    blocks = _extract_code_blocks(ai_reply)
    if not blocks:
        return []

    results: list[tuple[io.BytesIO, str, list[str]]] = []
    seen_names: set[str] = set()

    for lang, fname_hint, code in blocks:
        ext = _LANG_EXT.get(lang, "txt")

        # Tentukan nama file: dari hint > fallback
        if fname_hint:
            # Pastikan ekstensi sudah benar
            if "." not in fname_hint:
                fname_hint = f"{fname_hint}.{ext}"
            out_name = _sanitize_filename(fname_hint)
        else:
            out_name = f"{fallback_name}.{ext}"

        # Hindari nama duplikat
        if out_name in seen_names:
            base, dot, ext_part = out_name.rpartition(".")
            counter = 2
            while out_name in seen_names:
                out_name = f"{base}_{counter}.{ext_part}"
                counter += 1
        seen_names.add(out_name)

        # Validasi kode sebelum kirim
        warnings = _validate_code(lang, code, out_name)

        buf = io.BytesIO(code.encode("utf-8"))
        buf.seek(0)
        results.append((buf, out_name, warnings))

    return results


# ---------------------------------------------------------------------------
# Code validation
# ---------------------------------------------------------------------------

def _validate_code(lang: str, code: str, filename: str) -> list[str]:
    """Lakukan pengecekan dasar kode sebelum dikirim.

    Returns list of warning strings (kosong = tidak ada masalah).
    """
    warnings: list[str] = []

    if not code.strip():
        warnings.append("Kode kosong")
        return warnings

    if lang in ("python", "py"):
        try:
            ast.parse(code)
        except SyntaxError as e:
            warnings.append(f"Python SyntaxError baris {e.lineno}: {e.msg}")

    elif lang in ("html",):
        # Cek tag <html>, <body>, atau setidaknya ada tag HTML
        if "<html" not in code.lower() and "<body" not in code.lower() and "<div" not in code.lower():
            warnings.append("HTML tampak tidak memiliki struktur yang valid")
        open_tags = len(re.findall(r"<[a-zA-Z][^/!>]*>", code))
        close_tags = len(re.findall(r"</[a-zA-Z]+>", code))
        if abs(open_tags - close_tags) > 5:
            warnings.append(f"Ketidakseimbangan tag HTML: {open_tags} buka, {close_tags} tutup")

    elif lang in ("javascript", "js", "typescript", "ts"):
        # Cek keseimbangan bracket dasar
        opens = code.count("{")
        closes = code.count("}")
        if opens != closes:
            warnings.append(f"Ketidakseimbangan kurung kurawal: {opens} buka, {closes} tutup")

    elif lang in ("json",):
        import json as _json
        try:
            _json.loads(code)
        except _json.JSONDecodeError as e:
            warnings.append(f"JSON tidak valid: {e.msg} baris {e.lineno}")

    return warnings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_code_blocks(ai_reply: str) -> list[tuple[str, str, str]]:
    """Ekstrak semua blok kode Markdown dari reply AI.

    Returns list of (language, filename_hint, code).
    filename_hint diambil dari komentar baris pertama jika ada.
    """
    pattern = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
    blocks: list[tuple[str, str, str]] = []

    for match in pattern.finditer(ai_reply):
        lang = match.group(1).strip().lower()
        code = match.group(2).strip()
        if not code:
            continue

        # Cari filename hint di baris pertama kode
        fname_hint = ""
        first_line = code.split("\n")[0].strip()
        for prefix in _COMMENT_PREFIXES:
            if first_line.lower().startswith(prefix.lower()):
                raw = first_line[len(prefix):].strip()
                # Bersihkan akhiran comment (*/  -->, dll)
                raw = re.sub(r"\s*(\*/|-->)\s*$", "", raw).strip()
                fname_hint = _sanitize_filename(raw)
                break

        blocks.append((lang, fname_hint, code))

    return blocks


def _parse_markdown_table(text: str) -> list[list[str]]:
    """Parse semua tabel Markdown dari teks.

    Returns list of rows (list of cell strings).
    Row pertama adalah header. Baris separator dibuang.
    """
    rows: list[list[str]] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Buang baris separator (----)
        if all(re.match(r"^[-:]+$", c) for c in cells if c):
            continue
        # Bersihkan sel kosong di tepi
        while cells and cells[0] == "":
            cells.pop(0)
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            rows.append(cells)
    return rows


def _sanitize_filename(name: str) -> str:
    """Sanitasi nama file: buang karakter ilegal, batasi panjang."""
    # Izinkan huruf, angka, titik, underscore, strip
    name = re.sub(r"[^\w.\-]", "_", name.strip())
    # Hindari path traversal
    name = name.replace("..", "_")
    return name[:80] or "file"
