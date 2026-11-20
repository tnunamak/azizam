# Image workflow options

## Put controls in the request

In a request to an image route, put workflow-specific inputs in `extensions["com.azizam.workflow"]`. The keys inside that object are the workflow's input names. For example, this request for Krea 2 Turbo sets the number of refinement steps and asks for two images:

```json
{
  "model": "image.generate.krea2-turbo",
  "prompt": "A small sailboat on a calm lake at sunrise",
  "extensions": {
    "com.azizam.workflow": {
      "steps": 10,
      "batch_size": 2
    }
  }
}
```

Use the model identifier shown by your gateway's workflow listing. If the model exposes them, put `sampler` and `scheduler` next to `steps` in the same object.

## Discover accepted values and defaults

Find the workflow in the listing and read its `inputs`. For an input with choices, use a value in its `enum`. For `steps`, check its integer minimum and maximum. The input's `default` is the workflow's own setting: omit that input to keep it. Don't copy choices or ranges from a different model.

## Workflows without sampler choices

- `image.generate.moss-image`: its few-step adapter supplies the sampler and noise schedule.
- `image.edit.seedvr2-upscale`: it is a one-step upscaler, not a generation sampler.
- `image.edit.image-identity`: it is a composed gateway route without a graph input map; the route code sets its own steps.

## Request more than one image

On a workflow that exposes `batch_size`, set it in `extensions["com.azizam.workflow"]`, as shown above. Check the workflow's input bounds. The reply has one item per image.

See the [Krea 2 Turbo input declarations](../../manifests/workflows/krea2-turbo.yml).
