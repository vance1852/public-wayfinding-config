"""控制端独立启动入口。

可直接载入一份资料 JSON 启动本地控制端（不依赖合作方资料端）::

    python -m dispatch.runserver fixtures/two-directions-and-closure.json \\
        --port 8080 --screen-url http://127.0.0.1:9001
"""

import argparse
import json
from pathlib import Path

from .service import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path, help="资料文件（取其中 facts）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--screen-url", default=None,
                        help="屏端网关基址；缺省则只登记下发意图")
    parser.add_argument("--partner-url", default=None)
    args = parser.parse_args()
    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    server = run(scenario["facts"], args.host, args.port,
                 args.screen_url, args.partner_url)
    print(f"配置分发控制端 http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
