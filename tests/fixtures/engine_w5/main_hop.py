"""Request construction copied from main cff9fe18d1a85315e2905ee9e02616439a41de17, ending before transport dispatch."""
from __future__ import annotations
async def _forward_internal_litellm_local_upstream(request: Request, *, backend: str, upstream_base: str, body: dict, activation: 'LocalModelActivation', target_model: str, lease: Optional[ServingLease]) -> Response | StreamingResponse:
    """Forward one ready, leased request to the engine. A stream takes the lease."""
    admission = _dispatch_admission_response('chat.generate', auth_context=None, service_id=backend, service_available=None, resource_class='chat')
    if admission is not None:
        return admission
    body['model'] = activation.backend_model or target_model
    execution_request = LlmExecutionRequest(method='POST', url=f'{upstream_base}/v1/chat/completions', headers={'content-type': 'application/json'}, body=json.dumps(body).encode('utf-8'), read_timeout=UPSTREAM_READ_TIMEOUT)
    return execution_request
