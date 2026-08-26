from __future__ import annotations

import html
import mimetypes
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

from .models import BlockType, DocumentResult
from .text import wrap_ltr

CSS = """@charset "UTF-8";
body { text-align: right; line-height: 1.8; margin: 5%; font-family: serif; }
p { text-align: justify; }
h1, h2, h3 { text-align: right; page-break-after: avoid; }
.poetry { text-align: right; margin: 1em 8%; }
.poetry p { text-align: right; margin: .2em 0; }
.footnote { font-size: .85em; border-top: 1px solid #999; }
img { max-width: 100%; height: auto; }
"""


def _xhtml(title: str, body: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ar" lang="ar" dir="rtl">
<head><meta charset="utf-8"/><title>{html.escape(title)}</title><link rel="stylesheet" href="../css/book.css"/></head>
<body>{body}</body></html>"""


def _block_html(block, fix_bidi: bool = True) -> str:
    if block.type == BlockType.IMAGE and block.image_path:
        return f'<figure><img src="../images/{html.escape(block.image_path)}" alt=""/></figure>'
    content = (wrap_ltr(block.text) if fix_bidi else html.escape(block.text)).replace(
        "\n", "<br/>"
    )
    if block.type == BlockType.HEADING:
        return f"<h1>{content}</h1>"
    if block.type == BlockType.FOOTNOTE:
        return f'<aside epub:type="footnote" class="footnote">{content}</aside>'
    if block.type == BlockType.POETRY:
        lines = "".join(
            f"<p>{wrap_ltr(line) if fix_bidi else html.escape(line)}</p>"
            for line in block.text.splitlines()
            if line
        )
        return f'<div class="poetry">{lines}</div>'
    return f'<p dir="rtl">{content}</p>'


def build_epub(
    document: DocumentResult, output: Path, validate: bool = True
) -> list[str]:
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pdf2epub-") as temp:
        root = Path(temp)
        epub = root / "EPUB"
        (root / "META-INF").mkdir()
        (epub / "css").mkdir(parents=True)
        (epub / "chapters").mkdir()
        (epub / "images").mkdir()
        (root / "mimetype").write_text("application/epub+zip", encoding="ascii")
        (root / "META-INF" / "container.xml").write_text(
            """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""",
            encoding="utf-8",
        )
        (epub / "css" / "book.css").write_text(CSS, encoding="utf-8")
        chapters: list[tuple[str, str]] = []
        current: list[str] = []
        current_title = document.title
        for page in document.pages:
            for block in page.blocks:
                if block.type == BlockType.HEADING and current:
                    chapters.append((current_title, "".join(current)))
                    current = []
                    current_title = block.text.strip()[:160]
                current.append(_block_html(block, document.fix_bidi))
        if current or not chapters:
            chapters.append(
                (current_title, "".join(current) or "<p>لا يوجد نص قابل للاستخراج.</p>")
            )
        manifest: list[str] = [
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '<item id="css" href="css/book.css" media-type="text/css"/>',
        ]
        spine: list[str] = []
        nav_items: list[str] = []
        for i, (title, body) in enumerate(chapters, 1):
            name = f"chapter-{i:03d}.xhtml"
            (epub / "chapters" / name).write_text(_xhtml(title, body), encoding="utf-8")
            manifest.append(
                f'<item id="c{i}" href="chapters/{name}" media-type="application/xhtml+xml"/>'
            )
            spine.append(f'<itemref idref="c{i}"/>')
            nav_items.append(
                f'<li><a href="chapters/{name}">{html.escape(title)}</a></li>'
            )
        for i, image_path in enumerate(document.images, 1):
            shutil.copy2(image_path, epub / "images" / image_path.name)
            media = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
            manifest.append(
                f'<item id="img{i}" href="images/{html.escape(image_path.name)}" media-type="{media}"/>'
            )
        nav = f"""<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ar" dir="rtl"><head><title>الفهرس</title></head><body><nav epub:type="toc" id="toc"><h1>الفهرس</h1><ol>{"".join(nav_items)}</ol></nav></body></html>"""
        (epub / "nav.xhtml").write_text(nav, encoding="utf-8")
        uid = uuid4()
        package = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="ar" dir="rtl" prefix="rendition: http://www.idpf.org/vocab/rendition/#">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">urn:uuid:{uid}</dc:identifier><dc:title>{html.escape(document.title)}</dc:title><dc:language>ar</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>
<manifest>{"".join(manifest)}</manifest><spine page-progression-direction="rtl">{"".join(spine)}</spine></package>"""
        (epub / "package.opf").write_text(package, encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w") as archive:
            archive.write(
                root / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED
            )
            for item in sorted(root.rglob("*")):
                if item.is_file() and item.name != "mimetype":
                    archive.write(
                        item, item.relative_to(root), compress_type=zipfile.ZIP_DEFLATED
                    )
    if validate and shutil.which("epubcheck"):
        run = subprocess.run(
            ["epubcheck", str(output)], capture_output=True, text=True, check=False
        )
        if run.returncode:
            warnings.append(
                "EPUBCheck reported errors: " + (run.stdout + run.stderr)[-2000:]
            )
    elif validate:
        warnings.append(
            "EPUBCheck is not installed; ZIP and XML structure were generated but external validation was skipped"
        )
    return warnings
