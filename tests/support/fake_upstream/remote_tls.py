"""Synthetic TLS peer for the real remote connector checks."""
import asyncio


async def real_tls_identity(monkeypatch, directory):
    """Run the connector and TLS library against a synthetic local TLS server."""
    from tests.support.remote_protocol import socket_case
    import datetime
    import ssl
    import httpcore
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from gateway.engine_protocol.errors import ProtocolError
    import pytest

    real_connect = httpcore.AnyIOBackend.connect_tcp
    original_context = ssl.create_default_context
    for identity in ('engine.example', 'wrong.example'):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity)])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                       .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                       .not_valid_after(now + datetime.timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName(identity)]), critical=False)
                       .sign(key, hashes.SHA256()))
        cert_path, key_path = directory / 'synthetic-cert.pem', directory / 'synthetic-key.pem'
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        received = []
        async def serve(reader, writer):
            try:
                data = await reader.readuntil(b'\r\n\r\n')
                received.append(data)
                payload = b'{"value":"remote-model"}'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: ' +
                             str(len(payload)).encode() + b'\r\n\r\n' + payload)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0, ssl=server_context)
        port = server.sockets[0].getsockname()[1]
        dialled = []
        async def redirect_socket(backend, host, requested_port, **kwargs):
            dialled.append((host, requested_port))
            assert host == '93.184.216.34' and requested_port == 443
            return await real_connect(backend, '127.0.0.1', port, **kwargs)
        def context(*args, **kwargs):
            result = original_context(*args, **kwargs)
            result.load_verify_locations(cafile=str(cert_path))
            return result
        try:
            _, _, _, runtime, _, _ = socket_case(monkeypatch)
            monkeypatch.setattr(httpcore.AnyIOBackend, 'connect_tcp', redirect_socket)
            monkeypatch.setattr(ssl, 'create_default_context', context)
            if identity == 'engine.example':
                await runtime.invoke('state', {})
                assert received and b'Host: engine.example' in received[0]
            else:
                with pytest.raises(ProtocolError):
                    await runtime.invoke('state', {})
                assert received == []
            assert dialled == [('93.184.216.34', 443)]
        finally:
            server.close()
            await server.wait_closed()
            monkeypatch.setattr(httpcore.AnyIOBackend, 'connect_tcp', real_connect)
            monkeypatch.setattr(ssl, 'create_default_context', original_context)
            key_path.unlink()
            cert_path.unlink()

