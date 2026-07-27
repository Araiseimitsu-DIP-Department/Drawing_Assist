from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.web_app import DrawingApi, start_local_server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("preview", type=Path)
    args = parser.parse_args()

    api = DrawingApi()
    server, thread, token = start_local_server(api)
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/app.js", timeout=10) as response:
            script = response.read().decode("utf-8")
        if "window.pywebview.api" in script:
            raise SystemExit("The obsolete pywebview js_api bridge is still present.")

        request = Request(
            f"{base_url}/upload?name={quote(args.source.name)}",
            data=args.source.read_bytes(),
            method="POST",
            headers={
                "Content-Type": "application/pdf",
                "X-Drawing-Assist-Token": token,
            },
        )
        with urlopen(request, timeout=60) as response:
            state = json.loads(response.read().decode("utf-8"))
        if not state.get("ok") or not state.get("loaded"):
            raise SystemExit(state.get("message") or "PDF load failed.")

        prefix = "data:image/png;base64,"
        image = str(state.get("image") or "")
        if not image.startswith(prefix):
            raise SystemExit("No rendered PNG was returned.")
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        args.preview.write_bytes(base64.b64decode(image[len(prefix):]))
        print(
            "PASS: local HTTP upload and render "
            f"({state['file_name']}, {state['page_count']} page(s))"
        )
    finally:
        api.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
