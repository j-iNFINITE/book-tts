"""Main Gradio application for book_tts."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Generator, List, Optional

import gradio as gr

from book_tts.config import DEFAULT_OUTPUT_DIR, DEFAULT_VOICE, DEFAULT_STYLE
from book_tts.gui.components import (
    create_audio_preview,
    create_chapter_selector,
    create_file_upload,
    create_progress_display,
    create_tts_settings,
)
from book_tts.gui.state import ConversionState
from book_tts.models import ConversionStatus
from book_tts.tts.synthesizer import ParagraphSynthesizer
from book_tts.tts.sml import strip_sml_tokens
from book_tts.parsers.text_cleaner import TextCleaner
from book_tts.utils.file_utils import sanitize_filename
from book_tts.utils.history import record as history_record, load_history

POLL_INTERVAL = 0.5


def create_app() -> gr.Blocks:
    state = ConversionState()

    with gr.Blocks(
        title="Book TTS - Ebook to Audiobook",
        theme=gr.themes.Soft(),
    ) as app:
        gr.Markdown("# Book TTS\nConvert ebooks to audiobooks using TTS synthesis.")

        with gr.Row():
            with gr.Column(scale=1):
                file_upload = create_file_upload()
                tts_settings = create_tts_settings()
                parse_btn = gr.Button("Parse Ebook", variant="secondary")
                chapter_selector = create_chapter_selector()
                book_info = gr.Markdown("")

            with gr.Column(scale=1):
                with gr.Row():
                    convert_btn = gr.Button(
                        "Start Conversion", variant="primary", interactive=False
                    )
                    stop_btn = gr.Button(
                        "Stop", variant="stop", interactive=False
                    )
                    dry_run_btn = gr.Button(
                        "Dry Run Preview", variant="secondary", interactive=False
                    )
                progress_display = create_progress_display()
                dry_run_info = gr.Markdown("")
                audio_preview = create_audio_preview()
                output_dir_input = gr.Textbox(
                    label="Output Directory",
                    value=str(DEFAULT_OUTPUT_DIR),
                )

        # ── Parse handler ─────────────────────────────────────────────

        def handle_parse(file_paths: Optional[List[str]]) -> tuple:
            if not file_paths:
                gr.Warning("Please upload file(s) first.")
                return (
                    gr.update(choices=[], value=[]),
                    gr.update(interactive=False),
                    gr.update(interactive=False),
                    "No file uploaded.",
                )

            all_choices = []
            all_info = []
            errors = []

            for fpath in file_paths:
                try:
                    result = state.parse_file(fpath)
                    fname = Path(fpath).name
                    for ch in result.chapters:
                        label = f"[{fname}] {ch.index}: {ch.title} ({ch.word_count} chars)"
                        all_choices.append(label)
                    meta = result.metadata
                    info = f"**{fname}**: {len(result.chapters)} chapters"
                    if meta.author:
                        info += f" by {meta.author}"
                    all_info.append(info)
                except Exception as exc:
                    errors.append(f"{Path(fpath).name}: {exc}")

            if errors:
                gr.Warning(f"Some files failed: {'; '.join(errors)}")

            if not all_choices:
                return (
                    gr.update(choices=[], value=[]),
                    gr.update(interactive=False),
                    gr.update(interactive=False),
                    "No chapters found.",
                )

            return (
                gr.update(choices=all_choices, value=list(all_choices)),
                gr.update(interactive=True),
                gr.update(interactive=True),
                " | ".join(all_info),
            )

        parse_btn.click(
            fn=handle_parse,
            inputs=[file_upload],
            outputs=[chapter_selector, convert_btn, dry_run_btn, book_info],
        )

        # ── Dry-run handler ───────────────────────────────────────────

        def handle_dry_run(
            file_paths: Optional[List[str]],
            chapter_values: List[str],
            output_dir: str,
        ) -> tuple[str, str]:
            if not file_paths:
                return "", "**Error:** No file uploaded."
            if not chapter_values:
                return "", "**Error:** No chapters selected."

            out_dir = Path(output_dir) if output_dir else Path(DEFAULT_OUTPUT_DIR)
            cleaner = TextCleaner()
            summary_lines: list[str] = []
            total_chunks = 0
            total_chars = 0

            for fpath in file_paths:
                fname = Path(fpath).name
                # Group selected chapters for this file
                file_indices: list[int] = []
                for val in chapter_values:
                    if val.startswith(f"[{fname}]"):
                        try:
                            fname_end = val.index("]")
                            idx = int(val[fname_end + 2:].split(":")[0])
                            file_indices.append(idx)
                        except (ValueError, IndexError):
                            continue
                if not file_indices:
                    continue

                result = state.parse_file(fpath)
                book_stem = Path(fname).stem
                dry_dir = out_dir / book_stem / "_dry_text"
                dry_dir.mkdir(parents=True, exist_ok=True)

                file_chunks = 0
                file_chars = 0
                for ch_idx in file_indices:
                    ch = result.chapters[ch_idx]
                    safe_name = sanitize_filename(ch.title) or f"chapter_{ch_idx:04d}"
                    out_path = dry_dir / f"{ch_idx:04d}_{safe_name}.txt"

                    lines = [f"# {ch.title}", f"# Paragraphs: {len(ch.paragraphs)}", ""]
                    for para in ch.paragraphs:
                        cleaned = cleaner.clean(para)
                        if not cleaned.strip():
                            continue
                        chunks = ParagraphSynthesizer._split_text_multi_pass(cleaned)
                        for chunk in chunks:
                            tts_text = strip_sml_tokens(chunk).strip()
                            if tts_text:
                                lines.append(tts_text.replace("\n", " "))
                                file_chunks += 1
                                file_chars += len(tts_text)
                    out_path.write_text("\n".join(lines), encoding="utf-8")

                total_chunks += file_chunks
                total_chars += file_chars
                summary_lines.append(
                    f"**{fname}**: {file_chunks} TTS chunks ({file_chars} chars) → `{dry_dir}`"
                )

            info = (
                f"### Dry Run Complete\n"
                + "\n".join(summary_lines)
                + f"\n\n**Total**: {total_chunks} chunks, {total_chars} chars"
            )
            status = f"Dry run done. {total_chunks} chunks written."
            return status, info

        dry_run_btn.click(
            fn=handle_dry_run,
            inputs=[file_upload, chapter_selector, output_dir_input],
            outputs=[progress_display["status_text"], dry_run_info],
        )

        # ── Convert handler ───────────────────────────────────────────

        def handle_convert(
            file_paths: Optional[List[str]],
            chapter_values: List[str],
            voice: str,
            style: str,
            api_keys_str: str,
            base_url: str,
            output_dir: str,
        ) -> Generator:
            # Record new voice/style values for future dropdown suggestions.
            if voice and voice.strip():
                history_record(voice=voice.strip(), style=(style or "").strip())

            if not file_paths:
                gr.Warning("No file uploaded.")
                yield {
                    progress_display["status_text"]: "Error: No file uploaded",
                    progress_display["progress_bar"]: 0,
                }
                return

            api_keys = [
                k.strip() for k in api_keys_str.strip().split("\n") if k.strip()
            ]
            if not api_keys:
                gr.Warning("Please enter at least one API key.")
                yield {
                    progress_display["status_text"]: "Error: No API keys provided",
                    progress_display["progress_bar"]: 0,
                }
                return

            # Group selected chapters by file
            file_chapters: dict[str, list[int]] = {}
            for val in chapter_values:
                try:
                    # Format: [filename] idx: title (chars)
                    if val.startswith("["):
                        fname_end = val.index("]")
                        fname = val[1:fname_end]
                        idx = int(val[fname_end+2:].split(":")[0])
                        file_chapters.setdefault(fname, []).append(idx)
                except (ValueError, IndexError):
                    continue

            if not file_chapters:
                gr.Warning("Please select at least one chapter.")
                yield {
                    progress_display["status_text"]: "Error: No chapters selected",
                    progress_display["progress_bar"]: 0,
                }
                return

            out_dir = Path(output_dir) if output_dir else Path(DEFAULT_OUTPUT_DIR)
            total_files = len(file_chapters)
            completed = 0

            for fpath in file_paths:
                fname = Path(fpath).name
                if fname not in file_chapters:
                    continue

                completed += 1
                chapter_indices = file_chapters[fname]

                yield {
                    progress_display["status_text"]: f"Processing file {completed}/{total_files}: {fname}",
                    progress_display["progress_bar"]: 0,
                }

                try:
                    state.parse_file(fpath)
                    state.set_selected_chapters(chapter_indices)
                    state.start_conversion(
                        voice=voice or DEFAULT_VOICE,
                        style=style or DEFAULT_STYLE,
                        api_keys=api_keys,
                        base_url=base_url,
                        output_dir=out_dir,
                    )
                except Exception as exc:
                    gr.Warning(f"Failed to process {fname}: {exc}")
                    continue

                yield from _stream_progress(state, progress_display, audio_preview)

            yield {
                progress_display["status_text"]: f"All {total_files} files completed",
                progress_display["progress_bar"]: 100,
            }

        def update_stop_state() -> dict:
            return {
                convert_btn: gr.update(interactive=False),
                stop_btn: gr.update(interactive=True),
            }

        def re_enable_convert() -> dict:
            return {
                convert_btn: gr.update(interactive=True),
                stop_btn: gr.update(interactive=False),
            }

        def refresh_settings_choices() -> dict:
            voices, styles = load_history()
            if DEFAULT_VOICE not in voices:
                voices.insert(0, DEFAULT_VOICE)
            if DEFAULT_STYLE in styles:
                styles.remove(DEFAULT_STYLE)
            styles.insert(0, DEFAULT_STYLE)
            return {
                tts_settings["voice"]: gr.update(choices=voices),
                tts_settings["style"]: gr.update(choices=styles),
            }

        convert_btn.click(
            fn=update_stop_state,
            inputs=[],
            outputs=[convert_btn, stop_btn],
            queue=False,
        ).then(
            fn=handle_convert,
            inputs=[
                file_upload,
                chapter_selector,
                tts_settings["voice"],
                tts_settings["style"],
                tts_settings["api_keys"],
                tts_settings["base_url"],
                output_dir_input,
            ],
            outputs=[
                progress_display["status_text"],
                progress_display["progress_bar"],
                audio_preview,
            ],
        ).then(
            fn=re_enable_convert,
            inputs=[],
            outputs=[convert_btn, stop_btn],
            queue=False,
        ).then(
            fn=refresh_settings_choices,
            inputs=[],
            outputs=[tts_settings["voice"], tts_settings["style"]],
            queue=False,
        )

        # ── Stop handler ──────────────────────────────────────────────

        def handle_stop() -> str:
            state.cancel()
            return "Cancelling..."

        stop_btn.click(
            fn=handle_stop,
            inputs=[],
            outputs=[progress_display["status_text"]],
        )

    return app


def _stream_progress(
    state: ConversionState,
    progress_display: dict,
    audio_preview: gr.Audio,
) -> Generator:
    while state.is_converting:
        progress = state.get_progress()
        pct = _compute_percent(progress)
        status = _format_status(progress)

        yield {
            progress_display["status_text"]: status,
            progress_display["progress_bar"]: pct,
        }
        time.sleep(POLL_INTERVAL)

    final = state.get_progress()
    pct = _compute_percent(final)
    status = _format_status(final)

    audio_path = _find_audiobook(final)

    yield {
        progress_display["status_text"]: status,
        progress_display["progress_bar"]: pct,
        audio_preview: audio_path,
    }


def _compute_percent(progress) -> float:
    total = progress.total_chapters
    if total <= 0:
        return 0.0
    return round((progress.current_chapter / total) * 100, 1)


def _format_status(progress) -> str:
    status = progress.status
    msg = progress.message

    if status == ConversionStatus.IDLE:
        return "Ready"
    if status == ConversionStatus.PARSING:
        return "Parsing ebook..."
    if status == ConversionStatus.CONVERTING:
        elapsed = progress.elapsed_seconds
        remaining = progress.estimated_remaining
        parts = [msg] if msg else []
        parts.append(f"Elapsed: {elapsed:.0f}s")
        if remaining > 0:
            parts.append(f"ETA: {remaining:.0f}s")
        return " | ".join(parts)
    if status == ConversionStatus.COMPLETED:
        return f"Completed in {progress.elapsed_seconds:.0f}s"
    if status == ConversionStatus.CANCELLED:
        return "Cancelled"
    if status == ConversionStatus.ERROR:
        return f"Error: {msg}"
    return str(status)


def _find_audiobook(progress) -> Optional[str]:
    for f in progress.chapter_files:
        path = Path(str(f))
        if path.suffix == ".mp3" and path.is_file():
            return str(path)
    return None


def launch() -> None:
    app = create_app()
    app.launch()
