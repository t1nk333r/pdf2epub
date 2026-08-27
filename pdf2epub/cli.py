from __future__ import annotations

import argparse
from pathlib import Path

from .models import ConversionOptions
from .ocr import provider_names
from .pipeline import convert


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
    args = parser.parse_args()
    options = ConversionOptions(
        mode="force_ocr" if args.force_ocr else "automatic",
        preserve_images=not args.no_images,
        ocr_language=args.ocr_language,
        ocr_provider=args.ocr_provider,
    )
    outputs = convert(
        args.pdf,
        args.output,
        options,
        lambda stage, done, total: print(
            f"\r{stage}: {done}/{total}", end="", flush=True
        ),
    )
    print()
    for kind, path in outputs.items():
        print(f"{kind}: {path}")


if __name__ == "__main__":
    main()
