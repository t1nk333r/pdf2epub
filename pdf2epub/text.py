from __future__ import annotations

import re
import unicodedata

from .models import TextQuality

ARABIC_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
HEADING_RE = re.compile(
    r"^(?:الفصل|الباب|الجزء|المقدمة|التمهيد|الخاتمة|المبحث|المطلب)(?:\s|$)"
)
LTR_RE = re.compile(
    r"((?:https?://|www\.)\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"(?:[A-Za-z][A-Za-z0-9_.+/#:-]*)(?:\s+[A-Za-z][A-Za-z0-9_.+/#:-]*)*|"
    r"\b\d+(?:[.:/-]\d+)+\b)"
)
INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
# A lam-alef ligature that the PDF font maps back in visual order arrives with
# the lam and the alef transposed: "الأول" becomes "األول", "الانتخاب" becomes
# "االنتخاب", and "الله" becomes "اهلل". None of these sequences occur in
# correctly extracted Arabic.
REVERSED_LIGATURE_RE = re.compile(r"ا[اأإآ]ل|اهلل")
# Only the rare medial/final hamza carriers signal mis-mapped glyphs. The
# alef-based forms (أ إ آ) are ordinary high-frequency letters and flagging
# them made every short line of valid Arabic look corrupt.
RARE_HAMZA_FORMS = frozenset("ءؤئ")
NORMAL_PUNCTUATION = frozenset("،؛؟?.!,:()[]{}\"'«»…ـ-–—−/\\+%٪=*_#@&·•﴿﴾")
SEVERE_TEXT_REASONS = frozenset(
    {
        "replacement characters",
        "disconnected Arabic letters",
        "probable visual-order/reversed Arabic",
        "improbable Arabic glyph density",
        "unexpected symbol noise",
        "reversed lam-alef ligatures",
    }
)
# Reasons that mean the embedded text layer is untrustworthy enough to be worth
# a second opinion from OCR. Word-splitting is the classic symptom of a broken
# PDF font mapping, so it triggers a comparison even though it is not severe.
BROKEN_EXTRACTION_REASONS = SEVERE_TEXT_REASONS | {"excessive one-character tokens"}
# Below this many characters a page carries too little text for the
# comparison to mean anything, so OCR is trusted on its own.
MIN_COMPARABLE_TEXT = 200
MIN_LENGTH_RATIO = 0.6
MIN_WORD_OVERLAP = 0.5


def word_overlap(left: str, right: str) -> float:
    """Share of the left text's words that also appear in the right text."""
    left_words = {w for w in WORD_RE.findall(normalize_arabic(left)) if len(w) > 1}
    if not left_words:
        return 1.0
    right_words = {w for w in WORD_RE.findall(normalize_arabic(right)) if len(w) > 1}
    return len(left_words & right_words) / len(left_words)


def ocr_covers_embedded(embedded: str, ocr: str) -> bool:
    """Is the OCR read a plausible transcription of the same page?

    A vision model can drop half a page or invent fluent text, and neither
    shows up in a quality score: invented Arabic scores well. The embedded
    layer, even with broken glyph mapping, still preserves the words. So OCR
    is only allowed to replace substantial embedded text when it keeps roughly
    the same length and most of the same words.
    """
    if len(embedded.strip()) < MIN_COMPARABLE_TEXT:
        return True
    if len(ocr.strip()) < len(embedded.strip()) * MIN_LENGTH_RATIO:
        return False
    return word_overlap(embedded, ocr) >= MIN_WORD_OVERLAP


def evaluate_text(text: str) -> TextQuality:
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return TextQuality(reasons=["no extractable text"])
    arabic = sum(bool(ARABIC_RE.match(c)) for c in visible)
    replacements = sum(c in "\ufffd�" for c in visible)
    words = WORD_RE.findall(text)
    singles = sum(len(word) == 1 for word in words)
    arabic_chars = [c for c in visible if ARABIC_RE.match(c)]
    hamza_ratio = sum(c in RARE_HAMZA_FORMS for c in arabic_chars) / max(
        len(arabic_chars), 1
    )
    unexpected_symbols = sum(
        not c.isalnum() and not ARABIC_RE.match(c) and c not in NORMAL_PUNCTUATION
        for c in visible
    )
    unexpected_symbol_ratio = unexpected_symbols / len(visible)
    spaced_arabic = bool(re.search(r"(?:[\u0600-\u06ff]\s){4,}[\u0600-\u06ff]", text))
    reversed_ligatures = len(REVERSED_LIGATURE_RE.findall(text))
    probable_visual_order = any(
        HEADING_RE.match(line[::-1].strip())
        for line in text.splitlines()
        if line.strip()
    )
    arabic_ratio = arabic / len(visible)
    replacement_ratio = replacements / len(visible)
    single_ratio = singles / max(len(words), 1)
    length_score = min(len(visible) / 80, 1.0)
    script_score = (
        min(arabic_ratio / 0.35, 1.0)
        if arabic
        else (0.65 if re.search(r"[A-Za-z]", text) else 0.0)
    )
    score = 0.42 * length_score + 0.58 * script_score
    score -= min(replacement_ratio * 8, 0.65)
    if single_ratio > 0.45:
        score -= 0.25
    if spaced_arabic:
        score -= 0.35
    if reversed_ligatures:
        score -= 0.35
    if probable_visual_order:
        score -= 0.4
    improbable_glyph_density = len(arabic_chars) >= 8 and hamza_ratio > 0.16
    if improbable_glyph_density:
        score -= 0.4
    if unexpected_symbol_ratio > 0.035:
        score -= min(0.4, unexpected_symbol_ratio * 3)
    reasons: list[str] = []
    if replacement_ratio > 0.01:
        reasons.append("replacement characters")
    if single_ratio > 0.45:
        reasons.append("excessive one-character tokens")
    if spaced_arabic:
        reasons.append("disconnected Arabic letters")
    if reversed_ligatures:
        reasons.append("reversed lam-alef ligatures")
    if probable_visual_order:
        reasons.append("probable visual-order/reversed Arabic")
    if improbable_glyph_density:
        reasons.append("improbable Arabic glyph density")
    if unexpected_symbol_ratio > 0.035:
        reasons.append("unexpected symbol noise")
    if arabic_ratio < 0.12 and arabic:
        reasons.append("low Arabic character ratio")
    return TextQuality(
        score=max(0.0, min(score, 1.0)),
        arabic_ratio=arabic_ratio,
        replacement_ratio=replacement_ratio,
        single_char_ratio=single_ratio,
        suspicious_spacing=spaced_arabic,
        reasons=reasons,
        suspicious_glyph_ratio=max(hamza_ratio, unexpected_symbol_ratio),
    )


def normalize_arabic(text: str) -> str:
    """Conservative cleanup: preserve letters, digits, and diacritics."""
    text = "".join(
        unicodedata.normalize("NFKC", character)
        if 0xFB50 <= ord(character) <= 0xFDFF
        or 0xFE70 <= ord(character) <= 0xFEFF
        else character
        for character in text
    )
    text = unicodedata.normalize("NFC", text)
    text = INVISIBLE_RE.sub("", text).replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([،؛؟.!,:])", r"\1", text)
    return text.strip()


def wrap_ltr(text: str) -> str:
    """Return escaped XHTML text with LTR islands isolated."""
    import html

    pieces: list[str] = []
    end = 0
    for match in LTR_RE.finditer(text):
        pieces.append(html.escape(text[end : match.start()]))
        pieces.append(
            f'<span dir="ltr" class="ltr">{html.escape(match.group(0))}</span>'
        )
        end = match.end()
    pieces.append(html.escape(text[end:]))
    return "".join(pieces)
