"""Enforce a request bound before multipart uploads can fill the spool directory."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class UploadLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not (
            scope["path"] == "/uploads" or scope["path"].startswith(("/books/local/", "/settings/openwebui/"))
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        limit = self.max_bytes if scope["path"] == "/uploads" else 256 * 1024
        try:
            too_large = int(headers.get(b"content-length", b"0")) > limit
        except ValueError:
            too_large = False
        if too_large:
            await JSONResponse({"error": "Die hochgeladene Datei ist zu groß."}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "Die hochgeladene Datei ist zu groß.")
            return message

        await self.app(scope, limited_receive, send)
