"""Quick-prompt templates accessible via inline buttons.

Each template wraps the user's free-form input with an optimized prompt
that the AI tends to respond to well.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuickPrompt:
    key: str
    emoji: str
    label: str
    placeholder: str  # shown when bot asks the user for input
    template: str  # use ``{input}`` as the placeholder for user text


QUICK_PROMPTS: dict[str, QuickPrompt] = {
    "translate": QuickPrompt(
        key="translate",
        emoji="\U0001f310",
        label="Translate",
        placeholder=(
            "Kirim teks yang ingin diterjemahkan. Sebutkan bahasa target di "
            "akhir kalau perlu (misal: \u201c... ke Jepang\u201d)."
        ),
        template=(
            "Translate the following text. If a target language is mentioned, "
            "translate to that language. Otherwise: if the source is "
            "Indonesian, translate to English; if it is English, translate "
            "to Indonesian. Reply with the translation only.\n\n"
            "---\n{input}\n---"
        ),
    ),
    "summarize": QuickPrompt(
        key="summarize",
        emoji="\U0001f4dd",
        label="Summarize",
        placeholder=(
            "Paste artikel / teks panjang yang ingin diringkas (boleh "
            "beberapa pesan, akan diringkas pesan terakhir kamu)."
        ),
        template=(
            "Ringkas teks berikut dalam 5-7 poin bullet yang padat, dalam "
            "bahasa yang sama dengan teks. Akhiri dengan satu kalimat "
            "kesimpulan.\n\n---\n{input}\n---"
        ),
    ),
    "explain_code": QuickPrompt(
        key="explain_code",
        emoji="\U0001f4bb",
        label="Explain Code",
        placeholder=(
            "Paste potongan kode yang ingin dijelaskan. Boleh sebut bahasa "
            "atau apa yang bingung di akhir."
        ),
        template=(
            "Jelaskan kode berikut. Awali dengan ringkasan singkat 1-2 "
            "kalimat, lalu walk-through bagian penting, sebut potensi bug "
            "atau perbaikan. Pakai format yang rapi.\n\n```\n{input}\n```"
        ),
    ),
    "brainstorm": QuickPrompt(
        key="brainstorm",
        emoji="\U0001f4a1",
        label="Brainstorm",
        placeholder="Sebutkan topik / tujuan brainstorm.",
        template=(
            "Brainstorm 10 ide kreatif terkait topik berikut. Setiap ide "
            "1 kalimat, beragam pendekatan, dan beri label kategori "
            "singkat di depan tiap ide.\n\nTopik: {input}"
        ),
    ),
    "reply": QuickPrompt(
        key="reply",
        emoji="\u2709\ufe0f",
        label="Bantu Balas",
        placeholder=(
            "Paste pesan / email yang ingin kamu balas. Boleh tulis tone "
            "yang diinginkan di akhir (formal, santai, dll)."
        ),
        template=(
            "Bantu saya menyusun balasan untuk pesan berikut. Hasilkan 2 "
            "varian balasan: (1) ringkas, (2) lebih hangat. Pakai bahasa "
            "yang sama dengan pesan asli.\n\n---\n{input}\n---"
        ),
    ),
    "improve": QuickPrompt(
        key="improve",
        emoji="\u2728",
        label="Perbaiki Tulisan",
        placeholder=(
            "Paste tulisan yang ingin diperbaiki (grammar, kejelasan, "
            "alur)."
        ),
        template=(
            "Perbaiki tulisan berikut tanpa mengubah artinya. Fokus pada "
            "grammar, kejelasan, dan alur. Kirim hasil perbaikan dulu, "
            "lalu di bawah beri 3 catatan singkat apa yang diubah.\n\n"
            "---\n{input}\n---"
        ),
    ),
}


def get(key: str) -> QuickPrompt | None:
    return QUICK_PROMPTS.get(key)
