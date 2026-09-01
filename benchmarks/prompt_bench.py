"""Benchmark OCR prompt variants against a corpus of PDFs with embedded text.

Operator-run; never invoked by pytest. Usage (from the repo root):

    .venv/bin/python benchmarks/prompt_bench.py index --corpus ~/pdf2epub-bench-corpus
    .venv/bin/python benchmarks/prompt_bench.py run --corpus ~/pdf2epub-bench-corpus \
        --prompt benchmarks/prompts/00-baseline.txt --runs 3 \
        --out benchmarks/results/baseline
    .venv/bin/python benchmarks/prompt_bench.py compare \
        benchmarks/results/baseline benchmarks/results/v01 --primary dropped
    .venv/bin/python benchmarks/prompt_bench.py selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

from pdf2epub.ocr import OCR_PROMPT, OcrSettings, create_ocr_provider
from pdf2epub.text import (
    MIN_COMPARABLE_TEXT,
    dropped_character_findings,
    evaluate_text,
    normalize_arabic,
    numeral_mismatches,
    ocr_covers_embedded,
    word_overlap,
)

MANIFEST_NAME = "corpus-manifest.json"
MIN_TOTAL_COMPARABLE = 40
MIN_CORPUS_FILES = 2
COMPARABLE_SCORE_FLOOR = 0.48


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_manifest(corpus_dir: Path, manifest: dict) -> Path:
    manifest_path = corpus_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest_path


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


def build_manifest(corpus_dir: Path) -> dict:
    pdf_paths = sorted(corpus_dir.glob("*.pdf"))
    if not pdf_paths:
        print(f"No PDFs found in {corpus_dir}")
        raise SystemExit(2)

    files: dict[str, dict] = {}
    total_comparable = 0
    for pdf_path in pdf_paths:
        doc = pymupdf.open(pdf_path)
        try:
            page_count = doc.page_count
            comparable = []
            for page_number in range(1, page_count + 1):
                page = doc[page_number - 1]
                embedded = normalize_arabic(page.get_text("text"))
                if len(embedded.strip()) < MIN_COMPARABLE_TEXT:
                    continue
                if evaluate_text(embedded).score < COMPARABLE_SCORE_FLOOR:
                    continue
                comparable.append(page_number)
        finally:
            doc.close()
        files[pdf_path.name] = {
            "sha256": _sha256_file(pdf_path),
            "pages": page_count,
            "comparable": comparable,
        }
        total_comparable += len(comparable)
        print(f"{pdf_path.name}: {page_count} pages, {len(comparable)} comparable")

    manifest = {
        "corpus": str(corpus_dir),
        "files": files,
        "total_comparable": total_comparable,
    }
    if total_comparable < MIN_TOTAL_COMPARABLE or len(files) < MIN_CORPUS_FILES:
        print(
            "WARNING: corpus is underpowered (see plan 13); "
            "results are directional only."
        )
    return manifest


def cmd_index(args: argparse.Namespace) -> int:
    corpus_dir = Path(args.corpus)
    manifest = build_manifest(corpus_dir)
    manifest_path = _write_manifest(corpus_dir, manifest)
    print(f"Wrote {manifest_path} ({manifest['total_comparable']} comparable pages)")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _load_manifest(corpus_dir: Path) -> dict:
    manifest_path = corpus_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"No {MANIFEST_NAME} in {corpus_dir}; run `index` first.")
        raise SystemExit(2)
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _verify_corpus_unchanged(corpus_dir: Path, manifest: dict) -> None:
    for name, entry in manifest["files"].items():
        path = corpus_dir / name
        if not path.is_file() or _sha256_file(path) != entry["sha256"]:
            print(
                f"Corpus file {name} has changed since indexing; "
                "re-run `index` before comparing."
            )
            raise SystemExit(2)


def render_page_png(page, dpi: int) -> bytes:
    scale = dpi / 72
    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False
    )
    return pix.tobytes("png")


def _page_metrics(embedded: str, ocr_text: str) -> dict:
    dropped_pairs = dropped_character_findings(embedded, ocr_text)
    missing, added = numeral_mismatches(embedded, ocr_text)
    return {
        "dropped": len(dropped_pairs),
        "dropped_pairs": [list(pair) for pair in dropped_pairs],
        "overlap": word_overlap(embedded, ocr_text),
        "numerals_missing": missing,
        "numerals_added": added,
        "covers": ocr_covers_embedded(embedded, ocr_text),
        "score": evaluate_text(ocr_text).score,
        "latin": bool(re.search(r"[A-Za-z]", ocr_text)),
        "ocr_text": ocr_text,
    }


def execute_run(
    *,
    corpus_dir: Path,
    manifest: dict,
    manifest_sha256: str,
    prompt_file: str,
    prompt_text: str,
    provider,
    provider_name: str,
    model_name: str,
    runs: int,
    dpi: int,
    out_dir: Path,
) -> None:
    if not hasattr(provider, "prompt"):
        print(f"Provider {provider_name!r} does not take a prompt; refusing.")
        raise SystemExit(2)
    provider.prompt = prompt_text
    if hasattr(provider, "fallback_model"):
        provider.fallback_model = None

    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_sha256 = _sha256_text(prompt_text)

    for run_index in range(1, runs + 1):
        started = datetime.now(UTC).isoformat()
        pages: list[dict] = []
        for name in sorted(manifest["files"]):
            entry = manifest["files"][name]
            doc = pymupdf.open(corpus_dir / name)
            try:
                for page_number in entry["comparable"]:
                    page = doc[page_number - 1]
                    embedded = page.get_text("text")
                    png = render_page_png(page, dpi)
                    result = provider.recognize(png, "ara+eng")
                    metrics = _page_metrics(embedded, result.text)
                    metrics["file"] = name
                    metrics["page"] = page_number
                    pages.append(metrics)
            finally:
                doc.close()

        record = {
            "prompt_file": prompt_file,
            "prompt_sha256": prompt_sha256,
            "provider": provider_name,
            "model": model_name,
            "dpi": dpi,
            "corpus_manifest_sha256": manifest_sha256,
            "started": started,
            "pages": pages,
        }
        total_dropped = sum(p["dropped"] for p in pages)
        total_numerals = sum(
            len(p["numerals_missing"]) + len(p["numerals_added"]) for p in pages
        )
        print(
            f"run {run_index}/{runs}: {len(pages)} pages, "
            f"dropped={total_dropped} numerals={total_numerals}"
        )

        target = out_dir / f"run-{run_index:02d}.json"
        tmp = out_dir / f".run-{run_index:02d}.json.tmp"
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        os.replace(tmp, target)


def cmd_run(args: argparse.Namespace) -> int:
    corpus_dir = Path(args.corpus)
    manifest = _load_manifest(corpus_dir)
    _verify_corpus_unchanged(corpus_dir, manifest)
    manifest_sha256 = _sha256_file(corpus_dir / MANIFEST_NAME)

    prompt_path = Path(args.prompt)
    prompt_text = prompt_path.read_text(encoding="utf-8").rstrip("\n")
    if prompt_path.name == "00-baseline.txt" and prompt_text != OCR_PROMPT:
        print(
            "benchmarks/prompts/00-baseline.txt no longer matches "
            "pdf2epub.ocr.OCR_PROMPT; regenerate it before comparing."
        )
        return 2

    settings = OcrSettings.from_env()
    if args.model:
        settings.ollama_model = args.model
    provider = create_ocr_provider(args.provider, settings)
    model_name = getattr(provider, "model", args.model) or "unknown"

    execute_run(
        corpus_dir=corpus_dir,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        prompt_file=str(prompt_path),
        prompt_text=prompt_text,
        provider=provider,
        provider_name=args.provider,
        model_name=model_name,
        runs=args.runs,
        dpi=args.dpi,
        out_dir=Path(args.out),
    )
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    total_dropped: int
    total_numerals: int
    mean_overlap: float
    mean_score: float
    pages_not_covered: int
    pages_latin: int


def _load_runs(out_dir: Path) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(out_dir.glob("run-*.json"))
    ]


def _summarize(run: dict) -> RunSummary:
    pages = run["pages"]
    if not pages:
        return RunSummary(0, 0, 0.0, 0.0, 0, 0)
    return RunSummary(
        total_dropped=sum(p["dropped"] for p in pages),
        total_numerals=sum(
            len(p["numerals_missing"]) + len(p["numerals_added"]) for p in pages
        ),
        mean_overlap=mean(p["overlap"] for p in pages),
        mean_score=mean(p["score"] for p in pages),
        pages_not_covered=sum(1 for p in pages if not p["covers"]),
        pages_latin=sum(1 for p in pages if p["latin"]),
    )


def decide(
    baseline: list[RunSummary], variant: list[RunSummary], primary: str
) -> dict[str, bool]:
    def totals(runs, key):
        return [getattr(run, key) for run in runs]

    key = "total_dropped" if primary == "dropped" else "total_numerals"
    b, v = totals(baseline, key), totals(variant, key)
    rules = {
        # Non-deterministic runs: require the variant's WORST run to beat the
        # baseline's BEST run, so noise alone cannot produce an accept.
        "primary_disjoint": max(v) < min(b),
        # ...and the improvement must be material, not marginal.
        "primary_magnitude": mean(v) <= 0.8 * mean(b),
    }
    guards = {
        "total_dropped": "lower",
        "total_numerals": "lower",
        "pages_not_covered": "lower",
        "pages_latin": "lower",
        "mean_overlap": "higher",
        "mean_score": "higher",
    }
    del guards[key]
    for name, direction in guards.items():
        b_vals, v_vals = totals(baseline, name), totals(variant, name)
        spread = max(b_vals) - min(b_vals)  # the baseline's own run-to-run noise
        if direction == "lower":
            rules[f"guard_{name}"] = mean(v_vals) <= max(b_vals) + spread
        else:
            rules[f"guard_{name}"] = mean(v_vals) >= min(b_vals) - max(spread, 0.01)
    return rules


def _pairs_by_page(run: dict) -> dict[tuple[str, int], set[tuple[str, str]]]:
    return {
        (page["file"], page["page"]): {tuple(pair) for pair in page["dropped_pairs"]}
        for page in run["pages"]
    }


def cmd_compare(args: argparse.Namespace) -> int:
    baseline_dir = Path(args.baseline)
    variant_dir = Path(args.variant)
    baseline_runs = _load_runs(baseline_dir)
    variant_runs = _load_runs(variant_dir)

    if len(baseline_runs) < 2 or len(variant_runs) < 2:
        print("Each side needs at least 2 runs to compare; refusing.")
        return 2

    for field_name in ("corpus_manifest_sha256", "provider", "model", "dpi"):
        b_val = baseline_runs[0][field_name]
        v_val = variant_runs[0][field_name]
        if b_val != v_val:
            print(
                f"Baseline and variant disagree on {field_name} "
                f"({b_val!r} != {v_val!r}); refusing to compare."
            )
            return 2

    baseline_summaries = [_summarize(run) for run in baseline_runs]
    variant_summaries = [_summarize(run) for run in variant_runs]
    rules = decide(baseline_summaries, variant_summaries, args.primary)

    def totals(summaries, key):
        return [getattr(summary, key) for summary in summaries]

    key = "total_dropped" if args.primary == "dropped" else "total_numerals"
    metric_for_rule = {"primary_disjoint": key, "primary_magnitude": key}
    for guard_name in (
        "total_dropped",
        "total_numerals",
        "pages_not_covered",
        "pages_latin",
        "mean_overlap",
        "mean_score",
    ):
        if guard_name != key:
            metric_for_rule[f"guard_{guard_name}"] = guard_name

    for name, passed in rules.items():
        metric = metric_for_rule[name]
        b_vals = totals(baseline_summaries, metric)
        v_vals = totals(variant_summaries, metric)
        status = "PASS" if passed else "FAIL"
        print(f"{name} ({metric}): baseline={b_vals} variant={v_vals} -> {status}")

    verdict_accept = all(rules.values())
    print(f"verdict: {'ACCEPT' if verdict_accept else 'REJECT'}")

    baseline_pairs = _pairs_by_page(baseline_runs[0])
    variant_pairs = _pairs_by_page(variant_runs[0])
    gained = []
    lost = []
    for page_key in sorted(set(baseline_pairs) | set(variant_pairs)):
        b_set = baseline_pairs.get(page_key, set())
        v_set = variant_pairs.get(page_key, set())
        for pair in sorted(v_set - b_set):
            gained.append((*page_key, *pair))
        for pair in sorted(b_set - v_set):
            lost.append((*page_key, *pair))

    print(f"gained (variant introduced, {len(gained)}):")
    for file_name, page_number, embedded_word, ocr_word in gained:
        print(f"  {file_name} p{page_number}: {embedded_word!r} -> {ocr_word!r}")
    print(f"lost (variant fixed, {len(lost)}):")
    for file_name, page_number, embedded_word, ocr_word in lost:
        print(f"  {file_name} p{page_number}: {embedded_word!r} -> {ocr_word!r}")

    return 0 if verdict_accept else 1


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

_SELFTEST_LINES = [
    "This selftest page exists purely to push the embedded text layer of",
    "the rendered probe PDF past the two hundred character comparability",
    "floor so the benchmark harness always finds pages worth measuring here.",
    "The receipt recorded exactly 42 items delivered before closing today.",
]


class _EchoProvider:
    """Deterministic in-process provider: echoes fixed text, no I/O."""

    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self.prompt = "selftest probe prompt"
        self._text = text

    def recognize(self, image_png: bytes, language: str) -> SimpleNamespace:
        return SimpleNamespace(text=self._text)


def _build_selftest_pdf(path: Path) -> None:
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), _SELFTEST_LINES, fontsize=9)
    doc.save(path)
    doc.close()


def cmd_selftest(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        _build_selftest_pdf(corpus_dir / "probe.pdf")

        doc = pymupdf.open(corpus_dir / "probe.pdf")
        embedded_text = doc[0].get_text("text")
        doc.close()
        assert "receipt" in embedded_text, "ASCII round-trip lost the probe word"
        assert re.search(r"\b42\b", embedded_text), "ASCII round-trip lost the digits"

        mutilated_text = embedded_text.replace("receipt", "eceipt")
        mutilated_text = re.sub(r"\b42\b", "24", mutilated_text, count=1)

        manifest = build_manifest(corpus_dir)
        assert manifest["total_comparable"] == 3, (
            f"expected 3 comparable pages, got {manifest['total_comparable']}"
        )
        manifest_path = _write_manifest(corpus_dir, manifest)
        manifest_sha256 = _sha256_file(manifest_path)

        baseline_dir = tmp_path / "baseline"
        variant_dir = tmp_path / "variant"
        execute_run(
            corpus_dir=corpus_dir,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            prompt_file="selftest-faithful",
            prompt_text=OCR_PROMPT,
            provider=_EchoProvider("selftest-faithful", embedded_text),
            provider_name="selftest",
            model_name="selftest-model",
            runs=2,
            dpi=150,
            out_dir=baseline_dir,
        )
        execute_run(
            corpus_dir=corpus_dir,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            prompt_file="selftest-mutilator",
            prompt_text=OCR_PROMPT,
            provider=_EchoProvider("selftest-mutilator", mutilated_text),
            provider_name="selftest",
            model_name="selftest-model",
            runs=2,
            dpi=150,
            out_dir=variant_dir,
        )

        rc_reject = cmd_compare(
            argparse.Namespace(
                baseline=str(baseline_dir),
                variant=str(variant_dir),
                primary="dropped",
            )
        )
        assert rc_reject == 1, f"expected REJECT (exit 1), got exit {rc_reject}"

        rc_accept = cmd_compare(
            argparse.Namespace(
                baseline=str(variant_dir),
                variant=str(baseline_dir),
                primary="dropped",
            )
        )
        assert rc_accept == 0, f"expected ACCEPT (exit 0), got exit {rc_accept}"

    print("selftest: PASS")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prompt_bench.py",
        description=(
            "Benchmark OCR prompt variants against a corpus of PDFs with "
            "embedded text."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Build a corpus manifest.")
    index_parser.add_argument("--corpus", required=True)
    index_parser.set_defaults(func=cmd_index)

    run_parser = subparsers.add_parser("run", help="Run a prompt against the corpus.")
    run_parser.add_argument("--corpus", required=True)
    run_parser.add_argument("--prompt", required=True)
    run_parser.add_argument("--runs", type=int, required=True)
    run_parser.add_argument("--out", required=True)
    run_parser.add_argument("--provider", default="ollama")
    run_parser.add_argument("--dpi", type=int, default=300)
    run_parser.add_argument("--model", default=None)
    run_parser.set_defaults(func=cmd_run)

    compare_parser = subparsers.add_parser(
        "compare", help="Compare two runs and decide accept/reject."
    )
    compare_parser.add_argument("baseline")
    compare_parser.add_argument("variant")
    compare_parser.add_argument(
        "--primary", choices=("dropped", "numerals"), required=True
    )
    compare_parser.set_defaults(func=cmd_compare)

    selftest_parser = subparsers.add_parser(
        "selftest", help="Offline plumbing check; no network, no corpus."
    )
    selftest_parser.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
