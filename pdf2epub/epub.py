from __future__ import annotations

import html
import io
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

from .models import BlockType, DocumentResult
from .text import wrap_ltr

CSS = """@charset "UTF-8";
body { text-align: right; line-height: 1.8; margin: 5%; font-family: serif; }
p { text-align: justify; margin: .45em 0; }
h1, h2, h3 { text-align: right; page-break-after: avoid; }
.poetry { text-align: right; margin: 1em 8%; }
.poetry p { text-align: right; margin: .2em 0; }
.footnote { font-size: .85em; border-top: 1px solid #999; }
img { max-width: 100%; height: auto; }
"""


class EpubValidationError(RuntimeError):
    pass


def _validate_internal(path: Path) -> None:
    errors: list[str] = []
    required = {
        "mimetype",
        "META-INF/container.xml",
        "EPUB/package.opf",
        "EPUB/nav.xhtml",
    }
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if not names or names[0] != "mimetype":
                errors.append("mimetype must be the first archive entry")
            elif archive.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
                errors.append("mimetype must be stored without compression")
            if archive.read("mimetype") != b"application/epub+zip":
                errors.append("mimetype has the wrong content")
            missing = sorted(required.difference(names))
            if missing:
                errors.append("missing required files: " + ", ".join(missing))
            for name in names:
                if name.endswith((".xml", ".opf", ".xhtml", ".svg")):
                    try:
                        ElementTree.fromstring(archive.read(name))
                    except ElementTree.ParseError as exc:
                        errors.append(f"{name} is not valid XML: {exc}")
            if "EPUB/package.opf" in names:
                package = ElementTree.fromstring(archive.read("EPUB/package.opf"))
                namespace = {"opf": "http://www.idpf.org/2007/opf"}
                items = package.findall("opf:manifest/opf:item", namespace)
                ids = {item.attrib.get("id") for item in items}
                for item in items:
                    href = item.attrib.get("href", "")
                    target = str(Path("EPUB", href)).replace("\\", "/")
                    if not href or target not in names:
                        errors.append(
                            f"manifest resource is missing: {href or '<empty>'}"
                        )
                        continue
                    media_type = item.attrib.get("media-type", "")
                    if (
                        media_type.startswith("image/")
                        and media_type != "image/svg+xml"
                    ):
                        try:
                            from PIL import Image

                            with Image.open(io.BytesIO(archive.read(target))) as image:
                                image.verify()
                        except Exception as exc:  # noqa: BLE001 - all decoders fail closed
                            errors.append(f"invalid image resource {href}: {exc}")
                for itemref in package.findall("opf:spine/opf:itemref", namespace):
                    if itemref.attrib.get("idref") not in ids:
                        errors.append(
                            f"spine references unknown item: {itemref.attrib.get('idref')}"
                        )
                nav_items = [
                    item
                    for item in items
                    if "nav" in item.attrib.get("properties", "").split()
                ]
                if len(nav_items) != 1:
                    errors.append(
                        "manifest must contain exactly one navigation document"
                    )
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        errors.append(f"EPUB archive could not be inspected: {exc}")
    if errors:
        raise EpubValidationError(
            "Internal EPUB validation failed: " + "; ".join(errors)
        )


def _run_epubcheck(path: Path) -> list[str]:
    executable = shutil.which("epubcheck")
    if not executable:
        raise EpubValidationError(
            "EPUBCheck is required for publication but was not found on PATH"
        )
    try:
        run = subprocess.run(
            [executable, str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise EpubValidationError("EPUBCheck timed out after 180 seconds") from exc
    if run.returncode:
        details = (run.stdout + run.stderr)[-4000:]
        raise EpubValidationError(f"EPUBCheck reported errors: {details}")
    return []


def _xhtml(title: str, body: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ar" lang="ar" dir="rtl">
<head><meta charset="utf-8"/><title>{html.escape(title)}</title><link rel="stylesheet" href="../css/book.css"/></head>
<body epub:type="bodymatter" dir="rtl" lang="ar">{body}</body></html>"""


FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]]+)\]")


def _footnote_target(identifier: str, page: int) -> str:
    # Footnote numbering restarts on every page, so the id has to carry the
    # page too: one chapter file now holds several pages and EPUBCheck rejects
    # a duplicate id.
    safe = re.sub(r"[^\w.-]+", "-", identifier, flags=re.UNICODE).strip("-")
    return f"fn-{page}-{safe or 'note'}"


def _inline_html(text: str, fix_bidi: bool, page: int = 1) -> str:
    pieces: list[str] = []
    end = 0
    for match in FOOTNOTE_REF_RE.finditer(text):
        plain = text[end : match.start()]
        pieces.append(wrap_ltr(plain) if fix_bidi else html.escape(plain))
        identifier = match.group(1)
        pieces.append(
            f'<a epub:type="noteref" href="#{_footnote_target(identifier, page)}">'
            f'{html.escape(identifier)}</a>'
        )
        end = match.end()
    plain = text[end:]
    pieces.append(wrap_ltr(plain) if fix_bidi else html.escape(plain))
    return "".join(pieces)


# A cover, a half-title and a colophon each carry a large line of type that
# reads as a heading. Emitting one file per such line fragments the front
# matter into stubs, so anything this short is folded into its neighbour.
MIN_CHAPTER_TEXT = 240


def _merge_stub_chapters(chapters: list[tuple[str, str]]) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    for title, body in chapters:
        visible = len(re.sub(r"<[^>]+>", "", body).strip())
        if merged and visible < MIN_CHAPTER_TEXT:
            previous_title, previous_body = merged[-1]
            merged[-1] = (previous_title, previous_body + body)
        else:
            merged.append((title, body))
    # A cover and a half-title carry the same words; keep one file, not two.
    if len(merged) > 1 and merged[0][0].strip() == merged[1][0].strip():
        merged[0] = (merged[0][0], merged[0][1] + merged[1][1])
        del merged[1]
    return merged


def _block_html(block, fix_bidi: bool = True, page: int = 1) -> str:
    if block.type == BlockType.IMAGE and block.image_path:
        return f'<figure><img src="../images/{html.escape(block.image_path)}" alt=""/></figure>'
    content = _inline_html(block.text, fix_bidi, page).replace("\n", "<br/>")
    if block.type == BlockType.HEADING:
        level = 2 if block.heading_level == 2 else 1
        return f"<h{level}>{content}</h{level}>"
    if block.type == BlockType.FOOTNOTE:
        identifier = block.footnote_id or "note"
        return (
            f'<aside epub:type="footnote" class="footnote" '
            f'id="{_footnote_target(identifier, page)}">{content}</aside>'
        )
    if block.type == BlockType.POETRY:
        lines = "".join(
            f"<p>{_inline_html(line, fix_bidi, page)}</p>"
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
                if block.type == BlockType.HEADING and block.heading_level == 1:
                    if current:
                        chapters.append((current_title, "".join(current)))
                        current = []
                    current_title = block.text.strip()[:160]
                current.append(_block_html(block, document.fix_bidi, page.page))
        if current or not chapters:
            chapters.append(
                (current_title, "".join(current) or "<p>لا يوجد نص قابل للاستخراج.</p>")
            )
        chapters = _merge_stub_chapters(chapters)
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
        nav = f"""<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ar" dir="rtl"><head><title>الفهرس</title></head><body><nav epub:type="toc" id="toc"><h1>الفهرس</h1><ol>{"".join(nav_items)}</ol></nav><nav epub:type="landmarks" hidden="hidden"><h2>معالم الكتاب</h2><ol><li><a epub:type="bodymatter" href="chapters/chapter-001.xhtml">بداية الكتاب</a></li></ol></nav></body></html>"""
        (epub / "nav.xhtml").write_text(nav, encoding="utf-8")
        uid = uuid4()
        modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        package = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="ar" dir="rtl" prefix="rendition: http://www.idpf.org/vocab/rendition/#">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">urn:uuid:{uid}</dc:identifier><dc:title>{html.escape(document.title)}</dc:title><dc:language>ar</dc:language><meta property="dcterms:modified">{modified}</meta><meta property="rendition:layout">reflowable</meta></metadata>
<manifest>{"".join(manifest)}</manifest><spine page-progression-direction="rtl">{"".join(spine)}</spine></package>"""
        (epub / "package.opf").write_text(package, encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=output.parent, prefix=f".{output.name}.", suffix=".epub"
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            with zipfile.ZipFile(temporary, "w") as archive:
                archive.write(
                    root / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED
                )
                for item in sorted(root.rglob("*")):
                    if item.is_file() and item.name != "mimetype":
                        archive.write(
                            item,
                            item.relative_to(root),
                            compress_type=zipfile.ZIP_DEFLATED,
                        )
            _validate_internal(temporary)
            if validate:
                warnings.extend(_run_epubcheck(temporary))
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    return warnings
