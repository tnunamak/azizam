"""The one-line preview a History row shows: what the request asked (REQUEST_RETENTION.md section 17, slice 5b1 r2).

The preview is cut when the record is kept, from the kept body, so the list never loads a body. It is the first user
message, the input or the prompt; a removed value never shows in it.
"""
from __future__ import annotations

from gateway import request_history


def test_chat_shows_the_first_user_message_not_the_system_prompt():
    body = {"model": "chat-a", "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "And Spain?"},
    ]}
    assert request_history.preview_of(body) == "What is the capital of France?"


def test_chat_content_parts_read_their_text_and_skip_pictures():
    body = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "[url removed]"}},
        {"type": "text", "text": "Describe the picture"},
    ]}]}
    assert request_history.preview_of(body) == "Describe the picture"


def test_responses_input_is_a_string_or_a_list_of_messages():
    assert request_history.preview_of({"model": "m", "input": "Hello there"}) == "Hello there"
    body = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "Plan my week"}]}]}
    assert request_history.preview_of(body) == "Plan my week"


def test_messages_api_reads_the_first_user_message_with_block_content():
    body = {"model": "m", "max_tokens": 64, "system": "Be brief.",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Summarise this"}]}]}
    assert request_history.preview_of(body) == "Summarise this"


def test_a_picture_or_a_video_shows_its_prompt_and_speech_shows_its_input():
    assert request_history.preview_of({"model": "img", "prompt": "a lighthouse at dusk", "size": "1024x1024"}) == "a lighthouse at dusk"
    assert request_history.preview_of({"model": "tts", "input": "Read this aloud", "voice": "alloy"}) == "Read this aloud"


def test_an_image_edit_form_shows_the_prompt_field_of_its_form():
    body = {"fields": {"model": "edit", "prompt": "make the sky green"},
            "files": [{"field": "image", "media_type": "image/png", "bytes": 512}]}
    assert request_history.preview_of(body) == "make the sky green"


def test_embeddings_show_the_first_input():
    assert request_history.preview_of({"model": "emb", "input": ["first text", "second text"]}) == "first text"


def test_a_transcription_with_no_prompt_has_no_preview():
    body = {"fields": {"model": "whisper-1"}, "files": [{"field": "file", "media_type": "audio/mpeg", "bytes": 9000}]}
    assert request_history.preview_of(body) is None


def test_nothing_asked_in_words_or_an_unreadable_body_has_no_preview():
    assert request_history.preview_of(None) is None
    assert request_history.preview_of({"omitted": "unsupported_container", "media_type": "text/plain", "bytes": 4}) is None
    assert request_history.preview_of({"messages": [{"role": "assistant", "content": "Hi"}]}) is None
    assert request_history.preview_of({"prompt": "   "}) is None


def test_the_preview_is_one_line_and_cut_with_an_ellipsis():
    preview = request_history.preview_of({"prompt": "first line\n\n   second   line " + "word " * 80})
    assert "\n" not in preview
    assert "  " not in preview
    assert preview.startswith("first line second line word")
    assert preview.endswith("…")
    assert len(preview) == request_history.PREVIEW_CHARACTERS


def test_a_short_preview_is_not_cut():
    assert request_history.preview_of({"prompt": "A short one"}) == "A short one"


def test_a_removed_value_never_shows_in_a_preview():
    body = {"messages": [{"role": "user", "content": "Use key [removed] to call [url removed] now"}]}
    preview = request_history.preview_of(body)
    assert preview == "Use key to call now"
    assert "removed" not in preview


def test_the_summary_a_record_keeps_carries_the_preview():
    summary = request_history.summarize(
        "/openai/v1/chat/completions",
        {"model": "chat-a", "messages": [{"role": "user", "content": "Hello"}]},
        200,
        40,
    )
    assert summary == {"route": "/openai/v1/chat/completions", "model": "chat-a", "status": 200,
                       "latency_ms": 40, "preview": "Hello"}


def test_a_multipart_request_names_its_model_from_its_form():
    body = {"fields": {"model": "whisper-1"}, "files": [{"field": "file", "media_type": "audio/mpeg", "bytes": 9000}]}
    summary = request_history.summarize("/v1/audio/transcriptions", body, 200, 9)
    assert summary["model"] == "whisper-1"
