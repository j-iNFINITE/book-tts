"""CLI entry point for the book_tts package."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from tqdm import tqdm

from book_tts.config import DEFAULT_BASE_URL, DEFAULT_VOICE, DEFAULT_OUTPUT_DIR, AUDIO_FORMAT
from book_tts.models import TTSConfig, ConversionStatus
from book_tts.parsers.epub_parser import EPUBParser
from book_tts.parsers.mobi_parser import MOBIParser
from book_tts.parsers.markdown_parser import MarkdownParser
from book_tts.parsers.text_cleaner import TextCleaner
from book_tts.tts.client import MiMoTTSClient
from book_tts.tts.synthesizer import ParagraphSynthesizer
from book_tts.tts.synthesizer import ParagraphSynthesizer
from book_tts.tts.sml import strip_sml_tokens
from book_tts.audio.merger import AudioMerger
from book_tts.utils.progress import ProgressTracker
from book_tts.utils.file_utils import sanitize_filename, safe_json_save

logger = logging.getLogger(__name__)

_ENV_KEY_VAR = "MIMO_TTS_API_KEYS"
_ENV_BASE_URL_VAR = "MIMO_TTS_BASE_URL"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="book-tts",
        description="Convert ebooks (EPUB/MOBI) to audiobooks using MiMo TTS",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Input ebook file or directory containing ebooks",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--voice",
        default=None,
        help=f"TTS voice name (default: {DEFAULT_VOICE})",
    )
    parser.add_argument(
        "--style",
        default=None,
        help="TTS style description",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"TTS API base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Single MiMo API key",
    )
    parser.add_argument(
        "--api-keys",
        nargs="+",
        default=None,
        help="Multiple MiMo API keys (space-separated)",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the Gradio web interface instead of CLI conversion",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and split text without calling TTS; write per-chapter .txt files",
    )
    return parser


def _collect_api_keys(args: argparse.Namespace) -> list[str]:
    keys: list[str] = []

    if args.api_key:
        keys.append(args.api_key)
    if args.api_keys:
        keys.extend(args.api_keys)

    if not keys:
        env_val = os.environ.get(_ENV_KEY_VAR, "")
        if env_val:
            keys = [k.strip() for k in env_val.split(",") if k.strip()]

    seen: set[str] = set()
    unique: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def _select_parser(input_path: Path):
    ext = input_path.suffix.lower()
    if ext == ".epub":
        return EPUBParser()
    elif ext in (".mobi", ".azw", ".azw3"):
        return MOBIParser()
    elif ext in (".md", ".markdown"):
        return MarkdownParser()
    else:
        raise ValueError(f"Unsupported file format: {ext}")


def _save_podcast_metadata(
    output_dir: Path,
    metadata,
    chapter_titles: list[str],
    chapter_files: list[Path],
) -> None:
    podcast = {
        "title": metadata.title or "Untitled",
        "author": metadata.author or "Unknown",
        "description": metadata.description or f"{metadata.title} - Audiobook",
        "language": metadata.language or "zh-CN",
        "explicit": False,
        "category": "Arts/Books",
        "chapters": [
            {"title": t, "file": str(p.name)}
            for t, p in zip(chapter_titles, chapter_files)
        ],
    }
    safe_json_save(output_dir / "podcast.json", podcast)


def run_cli(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    process = _run_dry if args.dry_run else _process_file

    if input_path.is_dir():
        files = sorted(
            f for f in input_path.iterdir()
            if f.suffix.lower() in (".epub", ".mobi", ".azw", ".azw3", ".md", ".markdown")
        )
        if not files:
            print(f"No supported files found in {input_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Found {len(files)} files to process")
        for f in files:
            print(f"\n{'='*60}")
            process(args, f)
        return

    if not input_path.is_file():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    process(args, input_path)


def _process_file(args: argparse.Namespace, input_path: Path) -> None:
    api_keys = _collect_api_keys(args)
    if not api_keys:
        print(
            f"Error: provide API keys via --api-key, --api-keys, "
            f"or the {_ENV_KEY_VAR} environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    voice = args.voice or DEFAULT_VOICE
    style = args.style or ""
    base_url = args.base_url or os.environ.get(_ENV_BASE_URL_VAR) or DEFAULT_BASE_URL

    from book_tts.utils.history import record as history_record
    history_record(voice=voice, style=style)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    book_stem = input_path.stem
    book_dir = output_dir / book_stem
    book_dir.mkdir(parents=True, exist_ok=True)
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Parsing %s ...", input_path.name)
    print(f"Parsing {input_path.name} ...")

    parser = _select_parser(input_path)
    parse_result = parser.parse(input_path)

    chapters = parse_result.chapters
    if not chapters:
        print("No chapters found in the ebook.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(chapters)} chapters.")

    tts_config = TTSConfig(
        api_keys=tuple(api_keys),
        voice=voice,
        style=style,
        base_url=base_url,
    )
    tts_client = MiMoTTSClient(tts_config)
    cleaner = TextCleaner()
    progress_tracker = ProgressTracker(total_chapters=len(chapters))
    synthesizer = ParagraphSynthesizer(
        tts_client=tts_client,
        cleaner=cleaner,
        progress_tracker=progress_tracker,
    )
    merger = AudioMerger()

    progress_tracker.start()

    chapter_audio_paths: list[Path] = []
    chapter_titles: list[str] = []

    for chapter in tqdm(chapters, desc="Chapters", unit="ch"):
        ch_idx = chapter.index
        ch_title = chapter.title
        safe_name = sanitize_filename(ch_title) or f"chapter_{ch_idx:04d}"
        chapter_output = chapters_dir / f"{ch_idx:04d}_{safe_name}.{AUDIO_FORMAT}"

        logger.info("Synthesizing chapter %d: %s", ch_idx + 1, ch_title)
        para_dir = chapters_dir / f"_para_{ch_idx:04d}"
        para_dir.mkdir(parents=True, exist_ok=True)

        para_paths = synthesizer.synthesize_chapter(
            paragraphs=list(chapter.paragraphs),
            output_dir=para_dir,
            chapter_index=ch_idx,
            total_chapters=len(chapters),
        )

        if para_paths:
            merged = merger.merge_to_chapter(
                audio_paths=para_paths,
                output_path=chapter_output,
            )
            if merged:
                chapter_audio_paths.append(merged)
                chapter_titles.append(ch_title)

        shutil.rmtree(para_dir, ignore_errors=True)

    if not chapter_audio_paths:
        print("No chapter audio was generated.", file=sys.stderr)
        sys.exit(1)

    _save_podcast_metadata(
        output_dir=book_dir,
        metadata=parse_result.metadata,
        chapter_titles=chapter_titles,
        chapter_files=chapter_audio_paths,
    )

    progress_tracker.finish(ConversionStatus.COMPLETED)
    print(f"Done! {len(chapter_audio_paths)} chapters saved to: {chapters_dir}")


def _run_dry(args: argparse.Namespace, input_path: Path) -> None:
    """Parse and split text without calling TTS; write per-chapter .txt preview files."""
    from book_tts.tts.sml import intersperse_break_tokens, SML_BREAK, SML_PAUSE

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    book_stem = input_path.stem
    book_dir = output_dir / book_stem
    book_dir.mkdir(parents=True, exist_ok=True)
    dry_dir = book_dir / "_dry_text"
    dry_dir.mkdir(parents=True, exist_ok=True)

    parser = _select_parser(input_path)
    parse_result = parser.parse(input_path)

    chapters = parse_result.chapters
    if not chapters:
        print("No chapters found in the ebook.", file=sys.stderr)
        sys.exit(1)

    cleaner = TextCleaner()
    total_chunks = 0
    total_chars = 0

    for ch in chapters:
        ch_idx = ch.index
        safe_name = sanitize_filename(ch.title) or f"chapter_{ch_idx:04d}"
        out_path = dry_dir / f"{ch_idx:04d}_{safe_name}.txt"

        lines: list[str] = []
        lines.append(f"# {ch.title}")
        lines.append(f"# Paragraphs: {len(ch.paragraphs)}")
        lines.append("")

        for para in ch.paragraphs:
            cleaned = cleaner.clean(para)
            if not cleaned.strip():
                continue
            # Split into TTS-sized chunks
            chunks = ParagraphSynthesizer._split_text_multi_pass(cleaned)
            for chunk in chunks:
                tts_text = strip_sml_tokens(chunk).strip()
                if tts_text:
                    # One chunk = one line (internal newlines collapsed).
                    lines.append(tts_text.replace("\n", " "))
                    total_chunks += 1
                    total_chars += len(tts_text)

        out_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  [{ch_idx + 1}/{len(chapters)}] {safe_name} → {out_path}")

    print(f"Dry run complete. {total_chunks} TTS chunks ({total_chars} chars) written to: {dry_dir}")


def _launch_gui() -> None:
    try:
        from book_tts.gui import app as gui_app
        gui_app.launch()
    except ImportError:
        print(
            "Error: Gradio is required for the GUI. "
            "Install it with: pip install gradio",
            file=sys.stderr,
        )
        sys.exit(1)
    except AttributeError:
        print(
            "Error: The GUI module (book_tts.gui) does not expose a launchable app. "
            "Ensure book_tts.gui.app is defined.",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.gui:
        _launch_gui()
        return

    if not args.input:
        parser.error("the following arguments are required: input")

    run_cli(args)


if __name__ == "__main__":
    main()
