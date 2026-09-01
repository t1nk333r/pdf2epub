from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analyzer import MissingDependency
from .models import ConversionOptions
from .ocr import OLLAMA_BOOK_MODELS, OLLAMA_QUALITY_MODEL, provider_names
from .pipeline import ConversionIntegrityError, ConversionPublicationError, convert


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert an Arabic PDF to a reflowable EPUB 3"
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("output"))
    parser.add_argument("--force-ocr", action="store_true")
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("--ocr-language", default="ara+eng")
    parser.add_argument(
        "--ocr-provider",
        choices=provider_names(),
        default="tesseract",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Publish an EPUB even when pages need review. The quality report "
            "lists every affected page; the output is diagnostic, not final."
        ),
    )
    parser.add_argument(
        "--ollama-model",
        choices=OLLAMA_BOOK_MODELS,
        default=OLLAMA_QUALITY_MODEL,
    )
    args = parser.parse_args()
    options = ConversionOptions(
        mode="force_ocr" if args.force_ocr else "automatic",
        preserve_images=not args.no_images,
        ocr_language=args.ocr_language,
        ocr_provider=args.ocr_provider,
        ollama_model=args.ollama_model if args.ocr_provider == "ollama" else None,
        allow_partial=args.allow_partial,
    )
    try:
        outputs = convert(
            args.pdf,
            args.output,
            options,
            lambda stage, done, total: print(
                f"\r{stage}: {done}/{total}", end="", flush=True
            ),
        )
    except (ConversionIntegrityError, ConversionPublicationError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        if getattr(exc, "report_path", None):
            print(f"report: {exc.report_path}", file=sys.stderr)
        raise SystemExit(2)
    except MissingDependency as exc:
        print(f"\n{exc}", file=sys.stderr)
        raise SystemExit(3)
    print()
    for kind, path in outputs.items():
        print(f"{kind}: {path}")


if __name__ == "__main__":
    main()
