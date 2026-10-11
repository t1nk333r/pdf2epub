from pdf2epub.text import (
    dropped_character_findings,
    evaluate_text,
    normalize_arabic,
    numeral_mismatches,
    wrap_ltr,
)


def test_clean_arabic_scores_high():
    text = "هذا نص عربي سليم لاختبار جودة استخراج الكلمات من ملف الكتاب. " * 4
    assert evaluate_text(text).score >= 0.68


def test_disconnected_arabic_is_rejected():
    quality = evaluate_text("ا ل ك ت ا ب ا ل ع ر ب ي " * 5)
    assert quality.suspicious_spacing
    assert quality.score < 0.48


def test_disconnected_extended_arabic_is_detected():
    quality = evaluate_text("ݐ ݑ ݒ ݓ ݔ " * 3)
    assert "disconnected Arabic letters" in quality.reasons


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


def test_scan_coverage_can_ignore_a_corrupted_previous_ocr_layer():
    from pdf2epub.text import ocr_covers_embedded

    embedded = "ال!ب الم!لمين ال!لحاد مخ!جقا ت!ليك ما!ر ؤخا!ق " * 8
    reread = "هذا نص عربي سليم لاختبار جودة استخراج الكلمات من ملف الكتاب. " * 6
    assert not ocr_covers_embedded(embedded, reread)
    assert ocr_covers_embedded(embedded, reread, compare_words=False)
    assert not ocr_covers_embedded(embedded, "نص مبتور", compare_words=False)


def test_plain_lam_alef_transposition_is_detected():
    # "الانتخاب" arrives as "االنتخاب" when the ligature comes back reversed.
    quality = evaluate_text("االنتخاب الطبيعي في العالم الحي")
    assert "reversed lam-alef ligatures" in quality.reasons
    assert "reversed lam-alef ligatures" not in evaluate_text(
        "الانتخاب الطبيعي في العالم الحي"
    ).reasons


_FILLER = "هذا نص عربي سليم لاختبار جودة استخراج الكلمات من ملف الكتاب المطبوع. " * 3


def test_dropped_character_is_reported_with_both_forms():
    embedded = _FILLER + "كان الانحياز للتخندق الفكري واضحا."
    ocr = _FILLER + "كان الانحياز للخندق الفكري واضحا."
    assert dropped_character_findings(embedded, ocr) == [("للتخندق", "للخندق")]


def test_dropped_character_hint_reports_known_orthographic_variants():
    # "سلفا"/"سلف" is a tanween-alef spelling variant, not an OCR error.
    # Measured precision is ~50% precisely because such pairs are reported.
    # That is accepted behavior: this is a review hint, and any noise filter
    # must be re-measured on a real conversion before it is added. Do not
    # "fix" this test by filtering.
    embedded = _FILLER + "قلنا ذلك سلفا في الفصل الأول."
    ocr = _FILLER + "قلنا ذلك سلف في الفصل الأول."
    assert dropped_character_findings(embedded, ocr) == [("سلفا", "سلف")]


def test_dropped_character_check_skips_short_pages():
    # Below MIN_COMPARABLE_TEXT the comparison means nothing, same as the
    # coverage gate.
    assert dropped_character_findings("للتخندق", "للخندق") == []


def test_dropped_character_check_ignores_words_shorter_than_four_letters():
    embedded = _FILLER + "زرع فيه الأمل."
    ocr = _FILLER + "زع فيه الأمل."
    assert dropped_character_findings(embedded, ocr) == []


def test_dropped_character_check_requires_exactly_one_missing_character():
    # "اعتر" is "اعتراف" minus two characters; only exactly-one drops are
    # reported — this constraint is what keeps the list reviewable.
    embedded = _FILLER + "تحدث عن اعتراف الشهود."
    ocr = _FILLER + "تحدث عن اعتر الشهود."
    assert dropped_character_findings(embedded, ocr) == []


def test_dropped_character_check_is_silent_on_identical_texts():
    embedded = _FILLER + "كان الانحياز للتخندق الفكري واضحا."
    assert dropped_character_findings(embedded, embedded) == []


def test_dot_leaders_do_not_look_like_dropped_content():
    from pdf2epub.text import ocr_covers_embedded

    # A contents page pads every entry with dot leaders. OCR reproduces the
    # entries and omits the decoration, which is correct — the length gap is
    # typography, not lost text, and must not be read as a failed read.
    entries = [
        "مقدمة" + "." * 48 + " 18",
        "التطور الصغير" + "." * 40 + " 28",
        "التطور الكبير" + "." * 40 + " 38",
        "النوع" + "." * 48 + " 53",
        "الانتخاب الطبيعي" + "." * 36 + " 77",
    ]
    embedded = "\n".join(entries)
    ocr = "\n".join(e.replace(".", "").replace("  ", " ") for e in entries)
    assert ocr_covers_embedded(embedded, ocr)


def test_genuine_truncation_is_still_refused():
    from pdf2epub.text import ocr_covers_embedded

    # Stripping leaders must not blind the gate to real content loss.
    embedded = "هذه فقرة كاملة من نص الكتاب تحتوي على كلمات كثيرة ومفيدة جدا. " * 6
    truncated = "هذه فقرة كاملة من نص الكتاب"
    assert not ocr_covers_embedded(embedded, truncated)


def test_numeral_mismatch_reports_different_values():
    # The measured page-18 failure: embedded 73, OCR read ٩٣.
    embedded = _FILLER + "انظر صفحة 73 وطبعة عام 1985."
    ocr = _FILLER + "انظر صفحة ٩٣ وطبعة عام ١٩٨٥."
    assert numeral_mismatches(embedded, ocr) == (["73"], ["93"])


def test_numeral_mismatch_ignores_digit_system_differences():
    embedded = _FILLER + "في عام ١٤٠٥ هجرية."
    ocr = _FILLER + "في عام 1405 هجرية."
    assert numeral_mismatches(embedded, ocr) == ([], [])


def test_numeral_mismatch_identical_numbers_cancel():
    embedded = _FILLER + "الباب 3 والفصل 12."
    ocr = _FILLER + "الباب 3 والفصل 12."
    assert numeral_mismatches(embedded, ocr) == ([], [])


def test_numeral_mismatch_reports_omitted_contents_numbers():
    # A contents entry whose destination number OCR omitted.
    embedded = _FILLER + "المقدمة 9 الفصل الأول 15"
    ocr = _FILLER + "المقدمة 9 الفصل الأول"
    assert numeral_mismatches(embedded, ocr) == (["15"], [])


def test_numeral_mismatch_requires_comparable_text():
    assert numeral_mismatches("صفحة 73", "صفحة 93") == ([], [])


def test_numeral_mismatch_normalizes_persian_digits():
    embedded = _FILLER + "طبع سنة ۱۳۶۲ شمسية."
    ocr = _FILLER + "طبع سنة 1362 شمسية."
    assert numeral_mismatches(embedded, ocr) == ([], [])
