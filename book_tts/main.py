"""CLI entry point for the book_tts package."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from tqdm import tqdm

from book_tts.config import DEFAULT_BASE_URL, DEFAULT_VOICE, DEFAULT_OUTPUT_DIR
from book_tts.models import TTSConfig
from book_tts.pipeline import (
    ConversionPipeline,
    PipelineConfig,
    ProgressEvent,
    ChapterDoneEvent,
    CompletedEvent,
    CancelledEvent,
    ErrorEvent,
)

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
        "--format",
        choices=["mp3", "m4b"],
        default="mp3",
        help="Output format (default: mp3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and split text without calling TTS; write per-chapter .txt files",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from checkpoint (skip completed chapters)",
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


def _build_tts_config(args: argparse.Namespace) -> TTSConfig:
    api_keys = _collect_api_keys(args)
    voice = args.voice or DEFAULT_VOICE
    style = args.style or ""
    base_url = args.base_url or os.environ.get(_ENV_BASE_URL_VAR) or DEFAULT_BASE_URL
    return TTSConfig(
        api_keys=tuple(api_keys),
        voice=voice,
        style=style,
        base_url=base_url,
    )


def run_cli(args: argparse.Namespace) -> None:
    input_path = Path(args.input)

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
            _run_single(args, f)
        return

    if not input_path.is_file():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    _run_single(args, input_path)


def _run_single(args: argparse.Namespace, input_path: Path) -> None:
    if args.dry_run:
        _run_dry(args, input_path)
    else:
        _run_convert(args, input_path)


def _run_convert(args: argparse.Namespace, input_path: Path) -> None:
    api_keys = _collect_api_keys(args)
    if not api_keys:
        print(
            f"Error: provide API keys via --api-key, --api-keys, "
            f"or the {_ENV_KEY_VAR} environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    from book_tts.utils.history import record as history_record
    voice = args.voice or DEFAULT_VOICE
    style = args.style or ""
    history_record(voice=voice, style=style)

    tts_config = _build_tts_config(args)
    config = PipelineConfig(
        tts=tts_config,
        output_dir=Path(args.output),
        output_format=getattr(args, "format", "mp3"),
    )

    pipeline = ConversionPipeline(config)
    print(f"Parsing {input_path.name} ...")

    chapter_count = 0
    pbar = tqdm(desc="Chapters", unit="ch")

    for event in pipeline.convert(input_path, resume=args.resume):
        match event:
            case ProgressEvent(current, total, message):
                if pbar.total is None or pbar.total != total:
                    pbar.total = total
                    pbar.refresh()
                pbar.set_description_str(message)
            case ChapterDoneEvent(index, title, path):
                chapter_count += 1
                pbar.update(1)
            case CompletedEvent(metadata, chapter_files, elapsed):
                pbar.close()
                print(
                    f"Done! {len(chapter_files)} chapters saved "
                    f"in {elapsed:.0f}s"
                )
            case CancelledEvent(partial_files):
                pbar.close()
                print(f"Cancelled. {len(partial_files)} chapters were saved.")
            case ErrorEvent(error):
                pbar.close()
                print(f"Error: {error}", file=sys.stderr)
                sys.exit(1)


def _run_dry(args: argparse.Namespace, input_path: Path) -> None:
    config = PipelineConfig(
        tts=TTSConfig(),
        output_dir=Path(args.output),
        output_format=getattr(args, "format", "mp3"),
    )

    pipeline = ConversionPipeline(config)
    print(f"Parsing {input_path.name} ...")

    for event in pipeline.dry_run(input_path):
        match event:
            case ProgressEvent(current, total, message):
                print(f"  [{current}/{total}] {message}")
            case ChapterDoneEvent(index, title, path):
                print(f"Dry run complete. Output: {path}")
            case CancelledEvent(partial_files):
                print("Cancelled.")
            case ErrorEvent(error):
                print(f"Error: {error}", file=sys.stderr)
                sys.exit(1)


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
