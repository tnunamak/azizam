"""A fictional HTTP engine. Its protocol is loaded only from test data."""
import base64
import io
import httpx
from PIL import Image

from tests.test_engine_protocol_executor import Chunks


def png():
    out = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(out, format='PNG')
    return out.getvalue()


class ProtocolUpstream:
    def __init__(self):
        self.requests = []
        self.active = 'text-old'
        self.fail_selection = False
        self.status = 200
        self.image = png()

    def __call__(self, request):
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={'problem':'busy'}, headers={'retry-after':'7'})
        path = request.url.path.removeprefix('/prefix')
        if path == '/api/v3/converse':
            if b'"delivery":"live"' in request.content:
                return httpx.Response(200, stream=Chunks([b'event: word\ndata: {"delta":"Hi","done":false}\n\n', b'data: {"delta":"","done":true,"why":"eos","meter":{"in":3,"out":1}}\n\n']), headers={'content-type':'text/event-stream'})
            return httpx.Response(200, json={'result':{'id':'answer-1','issued':1700000000,'model':'text-v2','output':{'text':'Hi'},'why':'eos'},'meter':{'in':3,'out':1,'total':4}})
        if path == '/catalog':
            return httpx.Response(200, json={'available':[{'id':'prism-chat','object':'model','created':0,'owned_by':'test'}]})
        if path == '/vectors':
            return httpx.Response(200, json={'encoder':'embed-v3','vectors':[{'object':'embedding','index':0,'embedding':[0.25,0.75]}]})
        if path == '/picture':
            return httpx.Response(200, content=self.image, headers={'content-type':'image/png'})
        if path == '/api/v3/renders' and request.method == 'POST':
            return httpx.Response(200, json={'ticket':{'id':'job-1'}})
        if path == '/api/v3/renders/job-1':
            if request.method == 'DELETE':
                return httpx.Response(204)
            return httpx.Response(200, json={'phase':'FINISHED','outputs':[{'key':'output-1'}]})
        if path == '/api/v3/renders/job-1/outputs/output-1':
            return httpx.Response(200, content=self.image, headers={'content-type':'image/png'})
        if path == '/selected':
            if request.method == 'POST':
                if not self.fail_selection:
                    import json
                    self.active = json.loads(request.content)['model']
                return httpx.Response(204)
            return httpx.Response(200, json={'active':self.active})
        if path == '/speak':
            import wave
            out = io.BytesIO()
            with wave.open(out, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b'\0\0'*24)
            return httpx.Response(200, content=out.getvalue(), headers={'content-type':'audio/wav'})
        raise AssertionError((request.method, str(request.url), request.content))
