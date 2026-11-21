# Image adapters and text encoders

## Callers: choose from the workflow listing

1. [List image workflows with your member key](IMAGE_OPTIONS.md#list-images-and-send-a-request).
   Select the model you want and check its input
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
   workflow's architecture. The current rows are `krea2`, `qwen_image_2` and `flux2_klein_9b`;
   all match the `ss_base_model_version` header field. Files without a usable
   matching header are hidden by default. An explicit owner mark for the
   architecture is the exception.
3. To recognize a new base model, add a row to the registry using the
   header field and value from its trainer, then restart the gateway so it
   reads the registry again. Refresh the workflow listing after the restart.
   A new adapter in the engine’s folder usually appears within about five
   minutes. If the engine cannot be reached, the list may stay as it was.
   For a file replaced under the same name, the gateway may keep using the
   old file information for up to six hours. Use a new filename to see the
   replacement sooner.
   Encoder choices come from the workflow manifest, not the adapter folder.

## Your own adapters

Member uploads are offered only on workflows that run on this gateway's own
machine, not on remote workflows.

1. The owner must turn on member uploads in the admin settings. They are off by
   default. The owner can change the size limit for each file. The default is
   2 GiB, and the highest allowed limit is 8 GiB. The owner sets a
   per-person total. The default total is 10 GiB. If your upload would put you
   over the total, it is refused and the reply tells you how much you have used
   and what your total is. Storing the same file again does not count twice.
2. In the Library, choose **Upload your own style adapter** and pick a
   `.safetensors` file, or send it to `POST /me/api/adapters`.
   The Library tells you whether the adapter is ready to pick, was added but
   isn't offered by any workflow yet, or was refused. If the gateway refuses
   the upload, no new adapter is added.
   Only safetensors files are accepted. Pickle formats such as `.pt`, `.ckpt`,
   and `.bin` are refused.
   If the gateway cannot read the file's built-in information, it refuses the upload.
   If storage fails, the gateway keeps no new adapter in your Library, and it’s safe to retry when you see “The adapter could not be stored. Try again, and ask the owner if it continues.”
3. The gateway reads the file header to find its base model. Your adapter is
   offered only on workflows that use that base model. If the header names no
   known base model, the file is still stored, but the upload reply says it
   won't be offered.
4. Uploads are private to the person who uploaded them. The workflow's listing
   shows each ID with the label you gave it. An upload without a label is
   listed by its ID. The upload reply's `id` field is your upload ID and starts
   with `ast_`. Use the ID, not your label, in `loras`. For example, if you called an upload “my style” and its ID is
   `ast_example123`, put `{"loras":[{"name":"ast_example123","weight":1.0}]}`
   in your request.
5. The first use on a local workflow copies the file. Deleting your adapter
   frees room and removes it from the choice list.
