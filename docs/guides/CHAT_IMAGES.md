# Send a large image in chat

## Why upload first?

Chat requests have a JSON body size cap. Inline base64 uses that budget. Upload the image once and send its short asset ID in the chat request instead.

## Upload, then reference the image

1. Send the image to **POST /v1/assets** on your gateway, for example `https://ai.example.com/v1/assets`. Use multipart form data with the image in the `file` field and your normal API authentication.
2. Copy the `id` field from the JSON reply. It starts with `img_` followed by 64 lowercase hexadecimal characters.
3. Send **POST /v1/chat/completions** with the ID as `image_url.url`. Replace both example values with the model you use and the ID returned by the upload.

```json
{
  "model": "your-vision-model",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "What is in this image?"},
      {"type": "image_url", "image_url": {"url": "img_0000000000000000000000000000000000000000000000000000000000000000"}}
    ]
  }]
}
```

## If the request is refused

For a chat error, look at `error.code` and `error.param`. The parameter identifies the image part to fix.

| Code | What to change |
| --- | --- |
| `invalid_image_url` | The reference looks like an asset ID (it has no `:` and no `/`) but is not a valid one. Real URLs and data URIs pass through unchanged. Copy the entire `id` from the upload reply, with no spaces or extra punctuation. |
| `asset_missing` | The asset is not available to your API key. Check the ID and use a key allowed to read the asset, or upload the image again under the key you will use for chat. |
| `invalid_image_format` | Returned both when the upload is not recognized as an image and when a stored asset is not an image at chat time. Export it as a supported image, such as PNG, then upload the new file and use its new ID. |
| `file_too_large` | The upload exceeds its own size limit. Resize or compress the image, then retry the upload. |

If the upload refuses a missing or empty file, send a non-empty image in the multipart `file` field before retrying the chat.

## Who can use an asset ID?

Only a caller allowed to read the asset can use its ID. An unknown ID and an ID the caller may not read get the same refusal: `asset_missing`.

See the [request and refusal examples in the gateway contract tests](../../tests/test_chat_image_asset_refs.py).
