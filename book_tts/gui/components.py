"""Reusable Gradio UI components for the book_tts interface."""

from __future__ import annotations

from typing import Dict

import gradio as gr

from book_tts.config import DEFAULT_BASE_URL, DEFAULT_VOICE, DEFAULT_STYLE


def create_file_upload() -> gr.File:
    return gr.File(
        label="Upload Ebook(s)",
        file_types=[".epub", ".mobi", ".azw", ".azw3", ".md", ".markdown"],
        type="filepath",
        file_count="multiple",
    )


def create_tts_settings() -> Dict[str, gr.Component]:
    from book_tts.utils.history import load_history
    voices, styles = load_history()
    if DEFAULT_VOICE not in voices:
        voices.insert(0, DEFAULT_VOICE)
    if styles:
        # Keep existing styles, move DEFAULT_STYLE to front if present.
        if DEFAULT_STYLE in styles:
            styles.remove(DEFAULT_STYLE)
        styles.insert(0, DEFAULT_STYLE)
    else:
        styles.insert(0, DEFAULT_STYLE)

    with gr.Group():
        gr.Markdown("### TTS Settings")
        voice = gr.Dropdown(
            label="Voice",
            choices=voices,
            value=DEFAULT_VOICE,
            allow_custom_value=True,
        )
        style = gr.Dropdown(
            label="Style",
            choices=styles,
            value=DEFAULT_STYLE,
            allow_custom_value=True,
        )
        api_keys = gr.Textbox(
            label="API Keys (one per line)",
            lines=3,
            placeholder="Enter your API keys, one per line",
        )
        base_url = gr.Textbox(
            label="API Base URL",
            value=DEFAULT_BASE_URL,
            placeholder="https://api.example.com",
        )
    return {
        "voice": voice,
        "style": style,
        "api_keys": api_keys,
        "base_url": base_url,
    }


def create_chapter_selector() -> gr.CheckboxGroup:
    return gr.CheckboxGroup(
        label="Chapters",
        choices=[],
        value=[],
    )


def create_progress_display() -> Dict[str, gr.Component]:
    status_text = gr.Textbox(
        label="Status",
        value="Ready",
        interactive=False,
    )
    progress_bar = gr.Slider(
        label="Progress",
        minimum=0,
        maximum=100,
        value=0,
        interactive=False,
    )
    return {
        "status_text": status_text,
        "progress_bar": progress_bar,
    }


def create_audio_preview() -> gr.Audio:
    return gr.Audio(
        label="Audiobook Preview",
        type="filepath",
        interactive=False,
    )
