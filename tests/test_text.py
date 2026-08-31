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


def test_arabic_looking_gibberish_with_improbable_glyph_density_is_rejected():
    quality = evaluate_text("ائوضؤع اتمضؤة")
    assert "improbable Arabic glyph density" in quality.reasons
    assert quality.score < 0.48


def test_normalization_is_conservative():
    assert normalize_arabic("  أُسامة   يحيى \n\n\n ١٢٣ ") == "أُسامة يحيى\n\n١٢٣"


def test_arabic_presentation_forms_are_normalized():
    assert normalize_arabic("ﻻ") == "لا"


def test_ltr_islands_are_escaped_and_isolated():
    result = wrap_ltr("استخدم docker compose up ثم <هنا>")
    assert '<span dir="ltr" class="ltr">docker compose up</span>' in result
    assert "&lt;هنا&gt;" in result


def test_common_alef_hamza_letters_are_not_glyph_noise():
    quality = evaluate_text("إلى أبي وأمي")
    assert "improbable Arabic glyph density" not in quality.reasons


def test_ornate_quran_parentheses_are_normal_punctuation():
    quality = evaluate_text("﴿وَقُل رَّبِّ ارْحَمْهُمَا كَمَا رَبَّيَانِي صَغِيرًا﴾")
    assert "unexpected symbol noise" not in quality.reasons


def test_bullets_and_middle_dots_are_normal_punctuation():
    quality = evaluate_text("• الفصل الأول · الفصل الثاني • الفصل الثالث")
    assert "unexpected symbol noise" not in quality.reasons


def test_reversed_lam_alef_ligature_is_detected():
    quality = evaluate_text("الباب األول من الكتاب")
    assert "reversed lam-alef ligatures" in quality.reasons


def test_correct_lam_alef_is_not_flagged():
    quality = evaluate_text("الباب الأول من الكتاب")
    assert "reversed lam-alef ligatures" not in quality.reasons


def test_legitimate_hamza_before_lam_is_not_flagged():
    # "سأل" is an ordinary word; only the alef-initial transposition is a defect.
    assert "reversed lam-alef ligatures" not in evaluate_text("سأل الرجل").reasons


def test_ocr_that_drops_half_the_page_does_not_cover_it():
    from pdf2epub.text import ocr_covers_embedded

    embedded = "هذه فقرة كاملة من نص الكتاب تحتوي على كلمات كثيرة ومفيدة. " * 4
    truncated = "هذه فقرة كاملة من نص الكتاب"
    assert not ocr_covers_embedded(embedded, truncated)


def test_invented_text_of_the_right_length_does_not_cover_the_page():
    from pdf2epub.text import ocr_covers_embedded

    embedded = "هذه فقرة كاملة من نص الكتاب تحتوي على كلمات كثيرة ومفيدة. " * 4
    invented = "قصيدة مختلفة تماما عن موضوع آخر بعيد جدا لا صلة له بالصفحة. " * 4
    assert not ocr_covers_embedded(embedded, invented)


def test_a_faithful_reread_covers_the_page():
    from pdf2epub.text import ocr_covers_embedded

    # The broken layer transposes ligatures but keeps the words; a good OCR
    # read fixes the spelling and must still be accepted.
    embedded = "هذه فقرة كاملة من نص الكتاب تحتوي على كلمات كثيرة ومفيدة. " * 4
    corrected = "هذه فقرة كاملة من نص الكتاب تحتوي على كلمات كثيرة ومفيدة جدا. " * 4
    assert ocr_covers_embedded(embedded, corrected)


def test_short_pages_are_not_subject_to_the_coverage_gate():
    from pdf2epub.text import ocr_covers_embedded

    assert ocr_covers_embedded("عنوان", "عنوان الصفحة الكامل بعد المسح الضوئي")


def test_plain_lam_alef_transposition_is_detected():
    # "الانتخاب" arrives as "االنتخاب" when the ligature comes back reversed.
    quality = evaluate_text("االنتخاب الطبيعي في العالم الحي")
    assert "reversed lam-alef ligatures" in quality.reasons
    assert "reversed lam-alef ligatures" not in evaluate_text(
        "الانتخاب الطبيعي في العالم الحي"
    ).reasons
