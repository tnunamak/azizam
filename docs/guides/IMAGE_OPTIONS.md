# Image options

## Set workflow inputs

On an image request, put model-specific inputs in `extensions` under
`com.azizam.workflow`. Use the input names listed for the model: `steps` sets
how many refinement steps to run, `sampler` selects the generation method,
and `scheduler` controls how those steps are spaced.

Example request body for Krea 2 Turbo:

```json
{
  "model": "image.generate.krea2-turbo",
  "prompt": "A ceramic cup on a windowsill in morning light",
  "extensions": {
    "com.azizam.workflow": {
      "steps": 10,
      "batch_size": 2
    }
  }
}
```

## Find valid values and defaults

Find the model in the workflow listing and look up its `inputs`. For an input
with choices, `enum` lists the accepted values. For `steps`, use the listed
integer range. If you leave an input out, the workflow uses its own value;
the listing shows that value as `default`. Do not assume a sampler or
scheduler accepted by one model is accepted by another.

## Models without caller-selectable sampler controls

- `image.generate.moss-image`: the few-step adapter supplies its own sampler and noise schedule.
- `image.edit.seedvr2-upscale`: this is a one-step upscaler, not a generation sampler.
- `image.edit.image-identity`: the gateway composes this workflow without a direct graph-input mapping. Its route code sets the step count.

## Request more than one image

If the model lists a `batch_size` input, set it in the same
`com.azizam.workflow` object, within the listed range. The example requests
two images. The response's `data` array has one item per image.
