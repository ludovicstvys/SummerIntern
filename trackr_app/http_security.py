"""Reject oversized form streams before any parser or endpoint runs."""
from starlette.responses import PlainTextResponse

MAX_FORM_BYTES = 64 * 1024


class HttpSecurity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message = dict(message)
                message['headers'] = list(message.get('headers', [])) + [
                    (b'x-content-type-options', b'nosniff'),
                    (b'x-frame-options', b'DENY'),
                    (b'content-security-policy', b"default-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"),
                ]
            await send(message)

        headers = dict(scope.get('headers', []))
        content_type = headers.get(b'content-type', b'').split(b';', 1)[0].strip().lower()
        if content_type in (b'application/x-www-form-urlencoded', b'multipart/form-data'):
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                size += len(chunk)
                if size > MAX_FORM_BYTES:
                    return await PlainTextResponse('Form too large', status_code=413)(scope, receive, secure_send)
                chunks.append(chunk)
                if not message.get('more_body', False):
                    break
            consumed = False
            original_receive = receive
            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
                return await original_receive()
            receive = replay
        await self.app(scope, receive, secure_send)
