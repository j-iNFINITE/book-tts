"""Main Gradio application for book_tts."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Generator, List, Optional

import gradio as gr

from book_tts.config import DEFAULT_BASE_URL, DEFAULT_OUTPUT_DIR, DEFAULT_VOICE, DEFAULT_STYLE
from book_tts.gui.components import (
    ProgressDisplay,
    TTSSettings,
    create_chapter_selector,
    create_checkpoint_status,
    create_completion_summary,
    create_cost_estimator,
    create_file_upload,
    create_progress_display,
    create_retry_button,
    create_tts_settings,
    create_voice_preview,
)
from book_tts.gui.state import ConversionState
from book_tts.models import ConversionStatus
from book_tts.pipeline import (
    ConversionPipeline,
    PipelineConfig,
    TTSConfig,
    ChapterDoneEvent,
    ErrorEvent,
)
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
                voice_preview = create_voice_preview()
                parse_btn = gr.Button("Parse Ebook", variant="secondary")
                chapter_selector = create_chapter_selector()
                chapter_preview = create_chapter_preview()
                book_info = gr.Markdown("")
                cost_estimator = create_cost_estimator()

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
                    retry_btn = create_retry_button()
                progress_display = create_progress_display()
                completion_summary = create_completion_summary()
                dry_run_info = gr.Markdown("")
                checkpoint_status = create_checkpoint_status()
                output_dir_input = gr.Textbox(
                    label="Output Directory",
                    value=str(DEFAULT_OUTPUT_DIR),
                )

        # ── Voice preview handler ────────────────────────────────────

        def handle_voice_preview(preview_text, voice, style, api_keys_str, base_url):
            if not preview_text.strip():
                gr.Warning("Please enter text to preview")
                return None

            api_keys = [k.strip() for k in api_keys_str.strip().split("\n") if k.strip()]

            try:
                if api_keys:
                    from book_tts.tts.client import MiMoTTSClient
                    from book_tts.models import TTSConfig

                    config = TTSConfig(
                        api_keys=tuple(api_keys),
                        voice=voice or DEFAULT_VOICE,
                        style=style or "",
                        base_url=base_url or DEFAULT_BASE_URL,
                    )
                    client = MiMoTTSClient(config)
                else:
                    from book_tts.tts.edge_client import EdgeTTSClient

                    client = EdgeTTSClient(voice=voice or "zh-CN-XiaoyiNeural")

                audio_bytes = client.synthesize(text=preview_text)

                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    f.write(audio_bytes)
                    return f.name
            except Exception as exc:
                gr.Warning(f"Voice preview failed: {exc}")
                return None

        voice_preview.test_btn.click(
            fn=handle_voice_preview,
            inputs=[
                voice_preview.preview_text,
                tts_settings.voice,
                tts_settings.style,
                tts_settings.api_keys,
                tts_settings.base_url,
            ],
            outputs=[voice_preview.preview_audio],
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
                    gr.update(value="", visible=False),
                )

            all_choices = []
            all_info = []
            errors = []
            checkpoint_messages = []

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

                    ckpt = state.checkpoint_summary
                    if ckpt:
                        checkpoint_messages.append(
                            f"**{fname}**: Found checkpoint — "
                            f"{ckpt['completed_count']}/{len(result.chapters)} chapters completed"
                        )
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
                    gr.update(value="", visible=False),
                )

            checkpoint_text = "\n".join(checkpoint_messages) if checkpoint_messages else ""
            show_checkpoint = bool(checkpoint_text)

            return (
                gr.update(choices=all_choices, value=list(all_choices)),
                gr.update(interactive=True),
                gr.update(interactive=True),
                " | ".join(all_info),
                gr.update(value=checkpoint_text, visible=show_checkpoint),
            )

        parse_btn.click(
            fn=handle_parse,
            inputs=[file_upload],
            outputs=[chapter_selector, convert_btn, dry_run_btn, book_info, checkpoint_status],
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
            summary_lines: list[str] = []
            total_result: Optional[str] = None

            for fpath in file_paths:
                fname = Path(fpath).name
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

                config = PipelineConfig(tts=TTSConfig(), output_dir=out_dir)
                pipeline = ConversionPipeline(config)

                for event in pipeline.dry_run(Path(fpath), file_indices):
                    if isinstance(event, ChapterDoneEvent):
                        summary_lines.append(f"**{fname}**: {event.title} → `{event.path}`")
                    elif isinstance(event, ErrorEvent):
                        summary_lines.append(f"**{fname}**: Error - {event.error}")
                        break

                total_result = fname

            info = (
                "### Dry Run Complete\n"
                + "\n".join(summary_lines)
            )
            status = f"Dry run done: {total_result or 'no files'}"
            return status, info

        dry_run_btn.click(
            fn=handle_dry_run,
            inputs=[file_upload, chapter_selector, output_dir_input],
            outputs=[progress_display.status_text, dry_run_info],
        )

        # ── Cost estimate handler ────────────────────────────────────

        def handle_cost_estimate(chapter_values, price_per_million):
            if not chapter_values:
                return "Select chapters to estimate cost"
            try:
                price = float(price_per_million)
            except ValueError:
                return "Invalid price"

            parse_result = state.parse_result
            if parse_result is None:
                return "No book parsed"

            total_chars = 0
            for val in chapter_values:
                try:
                    fname_end = val.index("]")
                    idx = int(val[fname_end + 2:].split(":")[0])
                    if idx < len(parse_result.chapters):
                        total_chars += parse_result.chapters[idx].word_count
                except (ValueError, IndexError):
                    continue

            total_tokens = int(total_chars * 1.5)
            cost = (total_tokens / 1_000_000) * price

            return (
                f"### Cost Estimation\n"
                f"- **Characters**: {total_chars:,}\n"
                f"- **Estimated tokens**: {total_tokens:,} (1.5 tokens/char)\n"
                f"- **Estimated cost**: ¥{cost:.2f}"
            )

        cost_estimator.estimate_btn.click(
            fn=handle_cost_estimate,
            inputs=[chapter_selector, cost_estimator.price_input],
            outputs=[cost_estimator.cost_display],
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
            if voice and voice.strip():
                history_record(
                    voice=voice.strip(),
                    style=(style or "").strip(),
                )

            if not file_paths:
                gr.Warning("No file uploaded.")
                yield {
                    progress_display.status_text: "Error: No file uploaded",
                    progress_display.progress_bar: 0,
                }
                return

            api_keys = [
                k.strip() for k in api_keys_str.strip().split("\n") if k.strip()
            ]
            if not api_keys:
                gr.Warning("Please enter at least one API key.")
                yield {
                    progress_display.status_text: "Error: No API keys provided",
                    progress_display.progress_bar: 0,
                }
                return

            history_record(api_keys=api_keys, base_url=(base_url or "").strip())

            # Group selected chapters by file
            file_chapters: dict[str, list[int]] = {}
            for val in chapter_values:
                try:
                    if val.startswith("["):
                        fname_end = val.index("]")
                        fname = val[1:fname_end]
                        idx = int(val[fname_end + 2:].split(":")[0])
                        file_chapters.setdefault(fname, []).append(idx)
                except (ValueError, IndexError):
                    continue

            if not file_chapters:
                gr.Warning("Please select at least one chapter.")
                yield {
                    progress_display.status_text: "Error: No chapters selected",
                    progress_display.progress_bar: 0,
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
                    progress_display.status_text: f"Processing file {completed}/{total_files}: {fname}",
                    progress_display.progress_bar: 0,
                }

                try:
                    state.parse_file(fpath)
                    state.set_selected_chapters(chapter_indices)
                    state.start_conversion(
                        voice=voice or DEFAULT_VOICE,
                        style=style or DEFAULT_STYLE,
                        api_keys=api_keys,
                        base_url=base_url,
                        input_path=Path(fpath),
                        output_dir=out_dir,
                        resume=True,
                    )
                except Exception as exc:
                    gr.Warning(f"Failed to process {fname}: {exc}")
                    continue

                yield from _stream_progress(state, progress_display)

            yield {
                progress_display.status_text: f"All {total_files} files completed",
                progress_display.progress_bar: 100,
            }

            failed = state.failed_chapters
            if failed:
                marked_choices = []
                for ch in chapter_selector.choices:
                    if _get_chapter_idx(ch) in failed:
                        marked_choices.append(f"❌ {ch}")
                    else:
                        marked_choices.append(ch)
                yield {
                    chapter_selector: gr.update(choices=marked_choices),
                    retry_btn: gr.update(interactive=True),
                }
            else:
                state.clear_failed_chapters()
                yield {
                    retry_btn: gr.update(interactive=False),
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
                tts_settings.voice: gr.update(choices=voices),
                tts_settings.style: gr.update(choices=styles),
            }

        def handle_completion_summary() -> dict:
            progress = state.get_progress()
            if progress.status != ConversionStatus.COMPLETED:
                return {completion_summary: gr.update(visible=False)}

            elapsed = progress.elapsed_seconds
            chapter_count = len(progress.chapter_files)

            total_size = 0
            for f in progress.chapter_files:
                p = Path(str(f))
                if p.exists():
                    total_size += p.stat().st_size

            size_mb = total_size / (1024 * 1024)

            summary = f"""### Conversion Complete
- **Time**: {elapsed:.0f}s
- **Chapters**: {chapter_count}
- **Total size**: {size_mb:.1f} MB"""

            return {completion_summary: gr.update(value=summary, visible=True)}

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
                tts_settings.voice,
                tts_settings.style,
                tts_settings.api_keys,
                tts_settings.base_url,
                output_dir_input,
            ],
            outputs=[
                progress_display.status_text,
                progress_display.progress_bar,
                chapter_selector,
                retry_btn,
            ],
        ).then(
            fn=re_enable_convert,
            inputs=[],
            outputs=[convert_btn, stop_btn],
            queue=False,
        ).then(
            fn=refresh_settings_choices,
            inputs=[],
            outputs=[tts_settings.voice, tts_settings.style],
            queue=False,
        ).then(
            fn=handle_completion_summary,
            inputs=[],
            outputs=[completion_summary],
            queue=False,
        )

        # ── Stop handler ──────────────────────────────────────────────

        def handle_stop() -> str:
            state.cancel()
            return "Cancelling..."

        stop_btn.click(
            fn=handle_stop,
            inputs=[],
            outputs=[progress_display.status_text],
        )

        # ── Retry handler ─────────────────────────────────────────────

        def handle_retry(
            file_paths: Optional[List[str]],
            chapter_values: List[str],
            voice: str,
            style: str,
            api_keys_str: str,
            base_url: str,
            output_dir: str,
        ) -> Generator:
            failed = state.failed_chapters
            if not failed:
                gr.Warning("No failed chapters to retry")
                yield {
                    progress_display.status_text: "No failed chapters to retry",
                    retry_btn: gr.update(interactive=False),
                }
                return

            failed_values = [v for v in chapter_values if _get_chapter_idx(v) in failed]
            if not failed_values:
                gr.Warning("No failed chapters found in selection")
                yield {
                    progress_display.status_text: "No failed chapters in selection",
                    retry_btn: gr.update(interactive=False),
                }
                return

            state.clear_failed_chapters()

            clean_choices = [ch.removeprefix("❌ ") for ch in chapter_selector.choices]
            yield {
                chapter_selector: gr.update(choices=clean_choices),
            }

            yield from handle_convert(
                file_paths, failed_values, voice, style,
                api_keys_str, base_url, output_dir,
            )

            still_failed = state.failed_chapters
            if still_failed:
                marked_choices = []
                for ch in clean_choices:
                    if _get_chapter_idx(ch) in still_failed:
                        marked_choices.append(f"❌ {ch}")
                    else:
                        marked_choices.append(ch)
                yield {
                    chapter_selector: gr.update(choices=marked_choices),
                    retry_btn: gr.update(interactive=True),
                }
            else:
                yield {
                    retry_btn: gr.update(interactive=False),
                }

        retry_btn.click(
            fn=update_stop_state,
            inputs=[],
            outputs=[convert_btn, stop_btn],
            queue=False,
        ).then(
            fn=handle_retry,
            inputs=[
                file_upload,
                chapter_selector,
                tts_settings.voice,
                tts_settings.style,
                tts_settings.api_keys,
                tts_settings.base_url,
                output_dir_input,
            ],
            outputs=[
                progress_display.status_text,
                progress_display.progress_bar,
                chapter_selector,
                retry_btn,
            ],
        ).then(
            fn=re_enable_convert,
            inputs=[],
            outputs=[convert_btn, stop_btn],
            queue=False,
        )

        # ── Chapter preview handler ──────────────────────────────────

        def handle_chapter_preview(chapter_values: list[str]) -> str:
            if not chapter_values:
                return "Select a chapter to preview"

            last = chapter_values[-1]
            try:
                fname_end = last.index("]")
                fname = last[1:fname_end]
                idx = int(last[fname_end + 2:].split(":")[0])
            except (ValueError, IndexError):
                return "Invalid selection"

            parse_result = state.parse_result
            if parse_result is None or idx >= len(parse_result.chapters):
                return "Chapter not found"

            chapter = parse_result.chapters[idx]
            text = "\n\n".join(chapter.paragraphs)
            if len(text) > 5000:
                text = text[:5000] + "\n\n... (truncated)"
            return f"### {chapter.title}\n\n{text}"

        chapter_selector.select(
            fn=handle_chapter_preview,
            inputs=[chapter_selector],
            outputs=[chapter_preview],
        )

    return app


def _stream_progress(
    state: ConversionState,
    progress_display: ProgressDisplay,
) -> Generator:
    while state.is_converting:
        progress = state.get_progress()
        pct = _compute_percent(progress)
        status = _format_status(progress)

        yield {
            progress_display.status_text: status,
            progress_display.progress_bar: pct,
        }
        time.sleep(POLL_INTERVAL)

    final = state.get_progress()
    pct = _compute_percent(final)
    status = _format_status(final)

    yield {
        progress_display.status_text: status,
        progress_display.progress_bar: pct,
    }


def _compute_percent(progress) -> float:
    total = progress.total_chapters
    if total <= 0:
        return 0.0
    return round((progress.current_chapter / total) * 100, 1)


def _get_chapter_idx(choice: str) -> int:
    """Extract chapter index from a selector choice string."""
    val = choice.removeprefix("❌ ")
    try:
        fname_end = val.index("]")
        return int(val[fname_end + 2:].split(":")[0])
    except (ValueError, IndexError):
        return -1


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


def launch() -> None:
    app = create_app()
    app.launch()
