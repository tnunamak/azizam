Compatibility fixtures captured from `origin/main` `cff9fe18d1a85315e2905ee9e02616439a41de17`.

`live-shape.json` uses the supplied transport shape with invented model names, service units and key references. It contains no operator data. The two compiled fragments are JSON bytes from that revision’s `gateway/litellm_substrate.py`, run over the unchanged seed and this fixture. `main_hop.py` is that revision’s hop function through construction of its execution request, before network dispatch.

`main-probes.json` freezes resource and readiness URLs and headers from that same main revision, with environment overrides absent. No probe in this fixture sends an inference key.
