"""Reusable Gradio UI components for the book_tts interface."""

from __future__ import annotations

from dataclasses import dataclass

import gradio as gr

from book_tts.config import DEFAULT_BASE_URL, DEFAULT_VOICE, DEFAULT_STYLE


@dataclass
class VoicePreview:
    """Typed container for voice preview components."""

    preview_text: gr.Textbox
    test_btn: gr.Button
    preview_audio: gr.Audio


@dataclass
class TTSSettings:
    """Typed container for TTS settings components."""

    voice: gr.Dropdown
    style: gr.Dropdown
    api_keys: gr.Textbox
    base_url: gr.Textbox


@dataclass
class ProgressDisplay:
    """Typed container for progress display components."""

    status_text: gr.Textbox
    progress_bar: gr.HTML


def create_file_upload() -> gr.File:
    return gr.File(
        label="上传电子书",
        file_types=[".epub", ".mobi", ".azw", ".azw3", ".md", ".markdown"],
        type="filepath",
        file_count="multiple",
    )


def create_tts_settings() -> TTSSettings:
    from book_tts.utils.history import load_history
    voices, styles, api_keys_list, base_url_hist = load_history()
    if DEFAULT_VOICE not in voices:
        voices.insert(0, DEFAULT_VOICE)
    if styles:
        if DEFAULT_STYLE in styles:
            styles.remove(DEFAULT_STYLE)
        styles.insert(0, DEFAULT_STYLE)
    else:
        styles.insert(0, DEFAULT_STYLE)

    api_keys_value = "\n".join(api_keys_list) if api_keys_list else ""
    base_url_value = base_url_hist if base_url_hist else DEFAULT_BASE_URL

    with gr.Group():
        gr.Markdown("### TTS 设置")
        voice = gr.Dropdown(
            label="音色",
            choices=voices,
            value=DEFAULT_VOICE,
            allow_custom_value=True,
        )
        style = gr.Dropdown(
            label="风格",
            choices=styles,
            value=DEFAULT_STYLE,
            allow_custom_value=True,
        )
        api_keys = gr.Textbox(
            label="API Keys（每行一个）",
            value=api_keys_value,
            lines=3,
            placeholder="输入 API Key，每行一个",
        )
        base_url = gr.Textbox(
            label="API 地址",
            value=base_url_value,
            placeholder="https://api.example.com",
        )
    return TTSSettings(
        voice=voice,
        style=style,
        api_keys=api_keys,
        base_url=base_url,
    )


def create_chapter_selector() -> gr.CheckboxGroup:
    return gr.CheckboxGroup(
        label="章节",
        choices=[],
        value=[],
    )


def create_chapter_preview() -> gr.Markdown:
    return gr.Markdown(
        value="选择章节查看提取的文本",
        label="章节预览",
    )


def create_progress_display() -> ProgressDisplay:
    status_text = gr.Textbox(
        label="状态",
        value="就绪",
        interactive=False,
    )
    progress_bar = gr.HTML(
        value='<div style="background:#eee;border-radius:8px;height:24px;overflow:hidden">'
              '<div style="background:linear-gradient(90deg,#667eea,#764ba2);height:100%;width:0%;'
              'transition:width 0.3s;display:flex;align-items:center;justify-content:center;'
              'color:#fff;font-size:12px">0%</div></div>',
    )
    return ProgressDisplay(
        status_text=status_text,
        progress_bar=progress_bar,
    )


def create_checkpoint_status() -> gr.Markdown:
    return gr.Markdown(
        value="",
        visible=False,
    )


def create_retry_button() -> gr.Button:
    return gr.Button(
        "重试失败章节",
        variant="secondary",
        interactive=False,
    )


def create_completion_summary() -> gr.Markdown:
    return gr.Markdown(
        value="",
        visible=False,
    )


@dataclass
class CostEstimator:
    """Typed container for cost estimation components."""

    price_input: gr.Textbox
    estimate_btn: gr.Button
    cost_display: gr.Markdown


def create_cost_estimator() -> CostEstimator:
    with gr.Group():
        gr.Markdown("### 费用估算")
        price_input = gr.Textbox(
            label="每百万 token 单价（¥）",
            value="0.15",
        )
        estimate_btn = gr.Button("估算费用", variant="secondary")
        cost_display = gr.Markdown(value="")
    return CostEstimator(
        price_input=price_input,
        estimate_btn=estimate_btn,
        cost_display=cost_display,
    )


def create_voice_preview() -> VoicePreview:
    with gr.Group():
        gr.Markdown("### 语音试听")
        preview_text = gr.Textbox(
            label="试听文本",
            value="你好，这是一段语音测试。",
        )
        test_btn = gr.Button("试听", variant="secondary")
        preview_audio = gr.Audio(label="预览音频", interactive=False)
    return VoicePreview(
        preview_text=preview_text,
        test_btn=test_btn,
        preview_audio=preview_audio,
    )
