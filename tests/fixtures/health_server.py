"""Loopback health endpoints for the deploy-wait and qa-local tests.

    python3 health_server.py <port|0> [port-file]

/health → 200 "ok" · /version → 200 with $HEALTH_VERSION · /down → 503.
Port 0 picks a free port and writes it to port-file.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runtime_http_server import LoopbackHTTPServer  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/down':
            code, body = 503, b'down'
        elif path == '/version':
            code, body = 200, ('{"version":"%s"}' % os.environ.get('HEALTH_VERSION', '')).encode()
        else:
            code, body = 200, b'ok'
        self.send_response(code)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = LoopbackHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler)
if len(sys.argv) > 2:
    with open(sys.argv[2], 'w') as f:
        f.write(str(server.server_address[1]))
server.serve_forever()
