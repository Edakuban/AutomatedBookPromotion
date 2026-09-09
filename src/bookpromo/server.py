"""Start the browser only after the local server has successfully bound its port."""

import logging
import webbrowser

import uvicorn

from .config import Settings
from .web import create_app


def run_server(settings: Settings, *, open_browser: bool = True) -> None:
    host = f"[{settings.app_host}]" if ":" in settings.app_host else settings.app_host
    url = f"http://{host}:{settings.app_port}"

    class LocalServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                print(f"\nBook Promotion: {url}\nBeenden mit Strg+C.\n", flush=True)
                if open_browser:
                    try:
                        opened = webbrowser.open(url)
                    except (OSError, webbrowser.Error):
                        opened = False
                    if not opened:
                        logging.getLogger("bookpromo").warning(
                            "Browser konnte nicht geöffnet werden. Bitte die lokale URL öffnen."
                        )

    config = uvicorn.Config(
        create_app(settings), host=settings.app_host, port=settings.app_port,
        access_log=False, proxy_headers=False,
    )
    LocalServer(config).run()
