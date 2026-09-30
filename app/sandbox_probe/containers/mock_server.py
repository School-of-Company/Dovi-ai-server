import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RECEIVED: list[dict[str, str]] = []


class Handler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode(errors="replace") if length else ""
        if self.command == "GET" and self.path == "/_received":
            payload = json.dumps(RECEIVED).encode()
        else:
            RECEIVED.append({"method": self.command, "path": self.path, "body": body[:4096]})
            payload = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def log_message(self, format: str, *args: object) -> None:
        pass


def create_server(port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    create_server(int(sys.argv[1])).serve_forever()
