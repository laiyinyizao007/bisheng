"""PaddleOCR AI Studio shim for BISHENG.

BISHENG's `paddle_ocr` loader makes ONE synchronous request:
    POST <url>   {"file": <base64>, "fileType": 0|1, ...}
    -> {"errorCode": 0, "result": {"layoutParsingResults": [...]}}
The AI Studio cloud service now only offers an asynchronous job API
(submit -> poll -> download a JSONL result file). This shim exposes the synchronous
contract that BISHENG expects and translates it to the job API.

Pure standard library. The token is read from the PADDLEOCR_TOKEN env var and never logged.
"""
import base64
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

JOB_URL = os.environ.get("PADDLEOCR_JOB_URL", "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs")
MODEL = os.environ.get("PADDLEOCR_MODEL", "PaddleOCR-VL-1.6")
TOKEN = os.environ.get("PADDLEOCR_TOKEN", "").strip()
POLL_INTERVAL = float(os.environ.get("PADDLEOCR_POLL_INTERVAL", "3"))
JOB_TIMEOUT = float(os.environ.get("PADDLEOCR_JOB_TIMEOUT", "900"))
# Only these switches are forwarded to the cloud service; everything else in the request is ignored.
FORWARDED_OPTIONS = ("useDocOrientationClassify", "useDocUnwarping", "useChartRecognition")


class ShimError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def log(msg):
    print(f"[shim] {msg}", flush=True)


def _http(method, url, headers=None, data=None, timeout=120):
    req = urlrequest.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()
    except URLError as e:
        raise ShimError(f"cannot reach cloud service: {e.reason}")


def _multipart(fields, file_field, filename, content):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(parts)


def parse_document(file_b64, file_type, options):
    if not TOKEN:
        raise ShimError("PADDLEOCR_TOKEN is not set in the shim container", status=500)
    try:
        content = base64.b64decode(file_b64, validate=False)
    except Exception:
        raise ShimError("field 'file' is not valid base64", status=400)
    if not content:
        raise ShimError("field 'file' is empty", status=400)

    filename = "document.pdf" if file_type == 0 else "image.png"
    optional = {k: bool(options[k]) for k in FORWARDED_OPTIONS if k in options}
    boundary, body = _multipart(
        {"model": MODEL, "optionalPayload": json.dumps(optional)}, "file", filename, content
    )
    auth = {"Authorization": f"bearer {TOKEN}"}

    status, raw = _http("POST", JOB_URL, {**auth, "Content-Type": f"multipart/form-data; boundary={boundary}"}, body, 300)
    if status != 200:
        raise ShimError(f"submit failed: HTTP {status}: {raw[:300].decode(errors='replace')}")
    try:
        job_id = json.loads(raw)["data"]["jobId"]
    except Exception:
        raise ShimError(f"unexpected submit response: {raw[:300].decode(errors='replace')}")
    log(f"job {job_id} submitted ({len(content)} bytes, type={file_type})")

    deadline = time.time() + JOB_TIMEOUT
    while True:
        status, raw = _http("GET", f"{JOB_URL}/{job_id}", auth, None, 60)
        if status != 200:
            raise ShimError(f"poll failed: HTTP {status}: {raw[:300].decode(errors='replace')}")
        data = json.loads(raw)["data"]
        state = data.get("state")
        if state == "done":
            result_url = data["resultUrl"]["jsonUrl"]
            break
        if state == "failed":
            raise ShimError(f"cloud job failed: {data.get('errorMsg')}")
        if time.time() > deadline:
            raise ShimError(f"cloud job timed out after {JOB_TIMEOUT:.0f}s (state={state})", status=504)
        time.sleep(POLL_INTERVAL)

    status, raw = _http("GET", result_url, None, None, 120)
    if status != 200:
        raise ShimError(f"download result failed: HTTP {status}")
    results = []
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            results.extend(json.loads(line)["result"]["layoutParsingResults"])
    log(f"job {job_id} done, {len(results)} page(s)")
    return results


class Handler(BaseHTTPRequestHandler):
    server_version = "paddleocr-shim/1.0"

    def log_message(self, fmt, *args):  # keep the default access log quiet; we log what matters
        pass

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "token_configured": bool(TOKEN), "model": MODEL})
        self._send(404, {"errorCode": 404, "errorMsg": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/layout-parsing":
            return self._send(404, {"errorCode": 404, "errorMsg": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            file_type = int(payload.get("fileType", 0))
            results = parse_document(payload.get("file", ""), file_type, payload)
            self._send(200, {"errorCode": 0, "errorMsg": "Success", "result": {"layoutParsingResults": results}})
        except ShimError as e:
            log(f"error: {e}")
            self._send(e.status, {"errorCode": e.status, "errorMsg": str(e)})
        except Exception as e:  # unexpected: report it instead of hiding it
            log(f"unexpected error: {type(e).__name__}: {e}")
            self._send(500, {"errorCode": 500, "errorMsg": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    log(f"listening on :{port}, model={MODEL}, token_configured={bool(TOKEN)}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
