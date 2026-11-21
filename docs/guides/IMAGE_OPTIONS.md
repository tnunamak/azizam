# Image workflow options

## List images and send a request

Members use the OpenAI-compatible base URL ending in `/openai/v1`. Set
`AZIZAM_API_KEY` to your member app key and replace the example gateway host.

```bash
GATEWAY='https://ai.example.com'
BASE_URL="$GATEWAY/openai/v1"
curl --fail-with-body --request GET "$GATEWAY/gateway/v1/workflows" \
  --header "Authorization: Bearer $AZIZAM_API_KEY"
```

Use the workflow catalog, not just the OpenAI model list, to find the
image workflow and check its inputs and allowed
adapter names. The Krea 2 Turbo example below sets refinement steps, applies
a style adapter, and asks for two images. Replace the example adapter name
and weight with values from your listing. Put workflow-specific inputs in
`extensions["com.azizam.workflow"]`.

```bash
curl --fail-with-body --request POST "$BASE_URL/images/generations" \
  --header "Authorization: Bearer $AZIZAM_API_KEY" \
  --header 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "model": "image.generate.krea2-turbo",
  "prompt": "A small sailboat on a calm lake at sunrise",
  "extensions": {
    "com.azizam.workflow": {
      "steps": 10,
      "loras": [{"name": "example-style.safetensors", "weight": 0.8}],
      "batch_size": 2
    }
  }
}
JSON
```

Use the model identifier shown by your gateway's workflow listing. If the model exposes them, put `sampler` and `scheduler` next to `steps` in the same object.

## Discover accepted values and defaults

Find the workflow in the workflow listing (`GET /gateway/v1/workflows`) and read its inputs. Use one of the listed choices, and check the minimum and maximum for steps.
Leave out an optional input to use the built-in value; always send a value for required inputs. This still applies to an optional input with no default shown in the listing.
Ranges and choices can differ by workflow for guidance, width, height, and text encoder too. Don't copy them from another model.

## Workflows without sampler choices

- `image.generate.moss-image`: its few-step adapter supplies the sampler and noise schedule.
- `image.edit.seedvr2-upscale`: it is a one-step upscaler, not a generation sampler.
- `image.edit.image-identity`: This workflow chooses its own steps and does not offer sampler settings.

## Request more than one image

On a workflow that exposes `batch_size`, set it in `extensions["com.azizam.workflow"]`, as shown above. Check the workflow's input bounds. The reply has one item per image.

See the input declarations for [Krea 2 Turbo](../../manifests/workflows/krea2-turbo.yml) and [FLUX.2 Klein](../../manifests/workflows/flux2-klein.yml).
