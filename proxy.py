#!/usr/bin/env python3

import gzip
import http.client
import json
import os
import sys
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://localhost:9000")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
UPSTREAM_TIMEOUT = 30

MODIFY_PATH = "/getSystemData"

# Hop-by-hop headers must not be forwarded by a proxy (RFC 7230 s6.1)
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

upstream = urlsplit(UPSTREAM_URL)
UPSTREAM_HOST = upstream.hostname
UPSTREAM_PORT = upstream.port or (443 if upstream.scheme == "https" else 80)
UPSTREAM_BASE_PATH = upstream.path.rstrip("/")


def decode_body(body: bytes, encoding: str) -> bytes:
    """
    Decode based on Content-Encoding to parse the JSON.
    """

    enc = (encoding or "").lower().strip()
    if enc in ("", "identity"):
        return body
    if enc in ("gzip", "x-gzip"):
        return gzip.decompress(body)
    if enc == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    raise ValueError(f"unsupported content-encoding: {enc}")


def modify_json_body(body: bytes, headers: list):
    """
    Adds the system.rid key to the body payload if it is already valid JSON
    """

    content_encoding = next(
        (v for k, v in headers if k.lower() == "content-encoding"), ""
    )
    try:
        data = json.loads(decode_body(body, content_encoding))
    except ValueError, OSError, zlib.error:
        return None
    if not isinstance(data, dict):
        return None

    data["system"]["rid"] = "abc"
    new_body = json.dumps(data).encode("utf-8")

    # Re-serialised uncompressed, drop headers describing the old bytes.
    stale = {"content-encoding", "content-length", "etag", "content-md5"}
    new_headers = [(k, v) for k, v in headers if k.lower() not in stale]
    return new_body, new_headers


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SimpleProxy/1.0"

    def _proxy(self):
        # Read incoming body
        length = int(self.headers.get("Content-Length") or 0)
        req_body = self.rfile.read(length) if length else None

        # Prepare headers to be sent upstream
        fwd_headers = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP or k.lower() == "host":
                continue
            fwd_headers[k] = v
        fwd_headers["Host"] = upstream.netloc
        fwd_headers["Connection"] = "close"

        target = UPSTREAM_BASE_PATH + self.path

        # Send request
        conn = http.client.HTTPConnection(
            UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT
        )
        try:
            conn.request(self.command, target, body=req_body, headers=fwd_headers)
            resp = conn.getresponse()
            resp_body = resp.read()  # http.client de-chunks
            status, reason = resp.status, resp.reason
            resp_headers = (
                resp.getheaders()
            )  # list of tuples; keeps duplicates (Set-Cookie)
        except Exception as exc:
            self.log_error("upstream error: %r", exc)
            msg = json.dumps({"error": "bad gateway", "detail": str(exc)}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        finally:
            conn.close()

        # Add system.rid key when it's /getSystemData
        request_path = urlsplit(self.path).path
        if request_path == MODIFY_PATH and status == 200 and self.command == "GET":
            result = modify_json_body(resp_body, resp_headers)
            if result:
                resp_body, resp_headers = result

        # Pass response back to client
        self.send_response(status, reason)
        passthrough_length = self.command == "HEAD" or status in (204, 304)
        for k, v in resp_headers:
            kl = k.lower()
            if kl in HOP_BY_HOP:
                continue
            if kl == "content-length" and not passthrough_length:
                continue  # recomputed below
            self.send_header(k, v)
        if not passthrough_length:
            self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if self.command != "HEAD" and status not in (204, 304):
            self.wfile.write(resp_body)

    # Route every method through the same handler
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _proxy


    # base send_response() adds Server/Date headers itself; suppress them so the
    # upstream's own values (if any) are the ones the client sees.
    def send_response(self, code, message=None):
        self.log_request(code)
        self.send_response_only(code, message)


def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    print(
        f"Proxy listening on {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_URL}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
