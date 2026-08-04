from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.web_app import DrawingApi, start_local_server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the local Drawing Assist UI for browser checks."
    )
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args()

    api = DrawingApi()
    server, thread, token = start_local_server(api)
    url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    if args.ready_file is not None:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.write_text(url, encoding="utf-8")
    print(url, flush=True)
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        api.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
