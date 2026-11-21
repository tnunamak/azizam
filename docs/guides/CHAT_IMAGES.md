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

## If the upload is refused

Look at `error.code` in the reply from **POST /v1/assets**.

| Code | What to change |
| --- | --- |
| `missing_file` | No file was sent. Send the image in the multipart `file` field. |
| `invalid_image` | The file is empty or unreadable. Choose a non-empty image file you can open, then upload it again. |
| `invalid_image_format` | This image format is not supported. Save the image in a supported format, such as PNG, then upload it again. |
| `file_too_large` | The image is over the upload size limit (HTTP 413). Make the image smaller, then upload it again. |

## If the chat request is refused

A refused image reference stops the whole chat request.

For a chat error, look at `error.code` and `error.param`. The parameter identifies the image part to fix.

| Code | What to change |
| --- | --- |
| `invalid_image_url` | This value is not a valid stored image ID. Copy the entire ID from the upload reply. |
| `asset_missing` | The asset is not available to your API key. Check the ID and use a key allowed to read the asset, or upload the image again under the key you will use for chat. |
| `invalid_image_format` | The stored file is not an image. Export it as a supported image, such as PNG, then upload the new file and use its new ID. |

## Who can use an asset ID?

Only a caller allowed to read the asset can use its ID. An unknown ID and an ID the caller may not read get the same refusal: `asset_missing`.

See the [request and refusal examples in the gateway contract tests](../../tests/test_chat_image_asset_refs.py).
