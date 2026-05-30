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
        title="Book TTS - 电子书转有声书",
    ) as app:
        gr.Markdown("# Book TTS\n将电子书转换为有声书")

        with gr.Row():
            with gr.Column(scale=1):
                file_upload = create_file_upload()
                tts_settings = create_tts_settings()
                voice_preview = create_voice_preview()
                parser_choice = gr.Radio(
                    label="解析器",
                    choices=["标准", "纯 HTML"],
                    value="标准",
                    info="标准：使用 EPUB 导航；纯 HTML：按文件顺序，用标题标签作章节名",
                )
                parse_btn = gr.Button("解析电子书", variant="secondary")
                chapter_selector = create_chapter_selector()
                book_info = gr.Markdown("")

            with gr.Column(scale=1):
                with gr.Row():
                    convert_btn = gr.Button(
                        "开始转换", variant="primary", interactive=False
                    )
                    stop_btn = gr.Button(
                        "停止", variant="stop", interactive=False
                    )
                    dry_run_btn = gr.Button(
                        "Dry Run", variant="secondary", interactive=False
                    )
                    retry_btn = create_retry_button()
                progress_display = create_progress_display()
                completion_summary = create_completion_summary()
                cost_estimator = create_cost_estimator()
                dry_run_info = gr.Markdown("")
                checkpoint_status = create_checkpoint_status()
                output_format = gr.Radio(
                    label="输出格式",
                    choices=["mp3", "m4b"],
                    value="mp3",
                )
                output_dir_input = gr.Textbox(
                    label="输出目录",
                    value=str(DEFAULT_OUTPUT_DIR),
                )
                gr.Markdown("---")
                chapter_preview_dropdown = gr.Dropdown(
                    label="查看章节",
                    choices=[],
                    value=None,
                    interactive=True,
                )
                chapter_preview_text = gr.Textbox(
                    label="章节预览",
                    value="请先勾选章节",
                    lines=10,
                    interactive=False,
                )

        # ── Voice preview handler ────────────────────────────────────

        def handle_voice_preview(preview_text, voice, style, api_keys_str, base_url):
            if not preview_text.strip():
                gr.Warning("请输入试听文本")
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
                gr.Warning(f"语音试听失败: {exc}")
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

        def handle_parse(file_paths: Optional[List[str]], parser_choice: str) -> tuple:
            if not file_paths:
                gr.Warning("请先上传文件")
                return (
                    gr.update(choices=[], value=[]),
                    gr.update(interactive=False),
                    gr.update(interactive=False),
                    "未上传文件",
                    gr.update(value="", visible=False),
                )

            use_html_parser = parser_choice == "纯 HTML"
            all_choices = []
            all_info = []
            errors = []
            checkpoint_messages = []

            for fpath in file_paths:
                try:
                    result = state.parse_file(fpath, use_html_parser=use_html_parser)
                    fname = Path(fpath).name
                    for ch in result.chapters:
                        label = f"[{fname}] {ch.index}: {ch.title} ({ch.word_count} 字)"
                        all_choices.append(label)
                    meta = result.metadata
                    info = f"**{fname}**: {len(result.chapters)} 章"
                    if meta.author:
                        info += f" - {meta.author}"
                    all_info.append(info)

                    ckpt = state.checkpoint_summary
                    if ckpt:
                        checkpoint_messages.append(
                            f"**{fname}**: 发现断点 — "
                            f"已完成 {ckpt['completed_count']}/{len(result.chapters)} 章"
                        )
                except Exception as exc:
                    errors.append(f"{Path(fpath).name}: {exc}")

            if errors:
                gr.Warning(f"部分文件解析失败: {'; '.join(errors)}")

            if not all_choices:
                return (
                    gr.update(choices=[], value=[]),
                    gr.update(interactive=False),
                    gr.update(interactive=False),
                    "未找到章节",
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
            inputs=[file_upload, parser_choice],
            outputs=[chapter_selector, convert_btn, dry_run_btn, book_info, checkpoint_status],
        )

        # ── Dry-run handler ───────────────────────────────────────────

        def handle_dry_run(
            file_paths: Optional[List[str]],
            chapter_values: List[str],
            output_dir: str,
        ) -> tuple[str, str]:
            if not file_paths:
                return "", "**错误：** 未上传文件"
            if not chapter_values:
                return "", "**错误：** 未选择章节"

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
                        summary_lines.append(f"**{fname}**: 错误 - {event.error}")
                        break

                total_result = fname

            info = (
                "### 预览完成\n"
                + "\n".join(summary_lines)
            )
            status = f"预览完成: {total_result or '无文件'}"
            return status, info

        dry_run_btn.click(
            fn=handle_dry_run,
            inputs=[file_upload, chapter_selector, output_dir_input],
            outputs=[progress_display.status_text, dry_run_info],
        )

        # ── Cost estimate handler ────────────────────────────────────

        def handle_cost_estimate(chapter_values, price_per_million):
            if not chapter_values:
                return "选择章节以估算费用"
            try:
                price = float(price_per_million)
            except ValueError:
                return "价格无效"

            parse_result = state.parse_result
            if parse_result is None:
                return "未解析书籍"

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
                f"### 费用估算\n"
                f"- **字符数**: {total_chars:,}\n"
                f"- **预估 token 数**: {total_tokens:,}（1.5 token/字）\n"
                f"- **预估费用**: ¥{cost:.2f}"
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
            output_format: str,
            output_dir: str,
        ) -> Generator:
            if voice and voice.strip():
                history_record(
                    voice=voice.strip(),
                    style=(style or "").strip(),
                )

            if not file_paths:
                gr.Warning("未上传文件")
                yield {
                    progress_display.status_text: "错误: 未上传文件",
                    progress_display.progress_bar: 0,
                }
                return

            api_keys = [
                k.strip() for k in api_keys_str.strip().split("\n") if k.strip()
            ]
            if not api_keys:
                gr.Warning("请输入至少一个 API Key")
                yield {
                    progress_display.status_text: "错误: 未提供 API Key",
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
                gr.Warning("请至少选择一个章节")
                yield {
                    progress_display.status_text: "错误: 未选择章节",
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
                    progress_display.status_text: f"处理文件 {completed}/{total_files}: {fname}",
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
                        output_format=output_format or "mp3",
                    )
                except Exception as exc:
                    gr.Warning(f"处理 {fname} 失败: {exc}")
                    continue

                yield from _stream_progress(state, progress_display)

            yield {
                progress_display.status_text: f"全部 {total_files} 个文件处理完成",
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
            voices, styles, api_keys_list, base_url_hist = load_history()
            if DEFAULT_VOICE not in voices:
                voices.insert(0, DEFAULT_VOICE)
            if DEFAULT_STYLE in styles:
                styles.remove(DEFAULT_STYLE)
            styles.insert(0, DEFAULT_STYLE)
            return {
                tts_settings.voice: gr.update(choices=voices),
                tts_settings.style: gr.update(choices=styles),
                tts_settings.api_keys: gr.update(value="\n".join(api_keys_list) if api_keys_list else ""),
                tts_settings.base_url: gr.update(value=base_url_hist if base_url_hist else DEFAULT_BASE_URL),
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

            summary = f"""### 转换完成
- **耗时**: {elapsed:.0f} 秒
- **章节数**: {chapter_count}
- **文件大小**: {size_mb:.1f} MB"""

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
                output_format,
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
            outputs=[tts_settings.voice, tts_settings.style, tts_settings.api_keys, tts_settings.base_url],
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
            return "正在取消..."

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
            output_format: str,
            output_dir: str,
        ) -> Generator:
            failed = state.failed_chapters
            if not failed:
                gr.Warning("没有失败章节需要重试")
                yield {
                    progress_display.status_text: "没有失败章节",
                    retry_btn: gr.update(interactive=False),
                }
                return

            failed_values = [v for v in chapter_values if _get_chapter_idx(v) in failed]
            if not failed_values:
                gr.Warning("选中章节中无失败章节")
                yield {
                    progress_display.status_text: "选中章节中无失败章节",
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
                api_keys_str, base_url, output_format, output_dir,
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
                output_format,
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

        # ── Chapter preview handlers ─────────────────────────────────

        def update_preview_dropdown(chapter_values: list[str]) -> dict:
            if not chapter_values:
                return {
                    chapter_preview_dropdown: gr.update(choices=[], value=None),
                    chapter_preview_text: gr.update(value="请先勾选章节"),
                }
            return {
                chapter_preview_dropdown: gr.update(choices=chapter_values, value=chapter_values[0]),
            }

        def handle_chapter_preview(selected_chapter: str) -> str:
            if not selected_chapter:
                return "请先勾选章节"

            try:
                fname_end = selected_chapter.index("]")
                idx = int(selected_chapter[fname_end + 2:].split(":")[0])
            except (ValueError, IndexError):
                return "选择无效"

            parse_result = state.parse_result
            if parse_result is None or idx >= len(parse_result.chapters):
                return "章节未找到"

            chapter = parse_result.chapters[idx]
            return "\n\n".join(chapter.paragraphs)

        chapter_selector.change(
            fn=update_preview_dropdown,
            inputs=[chapter_selector],
            outputs=[chapter_preview_dropdown, chapter_preview_text],
        )

        chapter_preview_dropdown.change(
            fn=handle_chapter_preview,
            inputs=[chapter_preview_dropdown],
            outputs=[chapter_preview_text],
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
        return "就绪"
    if status == ConversionStatus.PARSING:
        return "正在解析电子书..."
    if status == ConversionStatus.CONVERTING:
        elapsed = progress.elapsed_seconds
        remaining = progress.estimated_remaining
        parts = [msg] if msg else []
        parts.append(f"已用时: {elapsed:.0f}s")
        if remaining > 0:
            parts.append(f"预计剩余: {remaining:.0f}s")
        return " | ".join(parts)
    if status == ConversionStatus.COMPLETED:
        return f"完成，耗时 {progress.elapsed_seconds:.0f}s"
    if status == ConversionStatus.CANCELLED:
        return "已取消"
    if status == ConversionStatus.ERROR:
        return f"错误: {msg}"
    return str(status)


def launch() -> None:
    app = create_app()
    app.launch(theme=gr.themes.Soft())
