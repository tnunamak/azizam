# Image adapters and text encoders

## Callers: choose from the workflow listing

1. Open the image workflow listing on your gateway, for example at
   `https://ai.example.com`. Select the model you want and check its input
   choices. A workflow that supports style adapters (LoRAs) lists the installed
   adapter names it offers for that model; do not guess a name or send a
   local path. If the workflow offers an encoder choice, use its listed names.
2. In the workflow inputs, send `loras` as a list of objects with `name` and
   `weight`. Send `text_encoder` only if you want a listed alternative to the
   usual encoder. Omit either input when you do not need it. The item limit and
   weight range must be read from the listing. They are part of the workflow
   contract, not gateway-wide defaults.

Illustrative workflow inputs. Replace both example names and the weight with
values from your listing:

```json
{
  "loras": [{"name": "example-style.safetensors", "weight": 1.0}],
  "text_encoder": "example-encoder"
}
```

An unknown or unsuitable name is refused instead of silently substituted. The
validation error identifies the rejected choice and lists the offered values
when choices are available. Use an offered value or omit the input. If the
listing offers none, refresh it before retrying. The run receipt records the
chosen adapter names and their effective weights, and the encoder if selected.

## Operators: make an adapter available

1. Put the adapter file in the image engine's adapter folder for that workflow.
   The gateway lists files from the folder configured by the workflow binding.
2. For automatic discovery, the file's header must name a base model that the
   [architecture registry](../../registry/adapter_architectures.yml) maps to the
   workflow's architecture. The current rows are `krea2` and `qwen_image_2`;
   both match the `ss_base_model_version` header field. Files without a usable
   matching header are hidden by default. An explicit owner mark for the
   architecture is the exception.
3. To recognize a new base model, add a row to the registry using the actual
   header field and value emitted by its trainer. Refresh the workflow listing
   after the engine and gateway have picked up the change. Encoder choices
   come from the workflow manifest, not the adapter folder.

## Out of scope

This guide covers operator-installed files. It does not cover uploads by members.
