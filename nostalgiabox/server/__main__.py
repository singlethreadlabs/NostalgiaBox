"""Run the headless server."""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "nostalgiabox.server.app:app",
        host="0.0.0.0",
        port=8080,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()

