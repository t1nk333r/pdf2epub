from pdf2epub.text import evaluate_text, normalize_arabic, wrap_ltr


def test_clean_arabic_scores_high():
    text = "هذا نص عربي سليم لاختبار جودة استخراج الكلمات من ملف الكتاب. " * 4
    assert evaluate_text(text).score >= 0.68


def test_disconnected_arabic_is_rejected():
    quality = evaluate_text("ا ل ك ت ا ب ا ل ع ر ب ي " * 5)
    assert quality.suspicious_spacing
    assert quality.score < 0.48


def test_reversed_heading_is_suspicious():
    quality = evaluate_text("لوألا لصفلا\nميلس يبرع صن اذه " * 4)
    assert "probable visual-order/reversed Arabic" in quality.reasons
    assert quality.score < 0.68


def test_normalization_is_conservative():
    assert normalize_arabic("  أُسامة   يحيى \n\n\n ١٢٣ ") == "أُسامة يحيى\n\n١٢٣"


def test_ltr_islands_are_escaped_and_isolated():
    result = wrap_ltr("استخدم docker compose up ثم <هنا>")
    assert '<span dir="ltr" class="ltr">docker compose up</span>' in result
    assert "&lt;هنا&gt;" in result
