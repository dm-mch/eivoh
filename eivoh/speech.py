"""Speech to text through the Soniox async API (stdlib only).

Measured on 2026-11-13 with Telegram voice notes of 15-93 s: the async API
returned in 3-6 s, while streaming the same file over the real-time WebSocket
took about as long as the audio itself. So recorded voice goes through async.
"""
import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger(__name__)
BASE = "https://api.soniox.com"


class TranscriptionError(Exception):
    pass


class SonioxTranscriber:
    def __init__(self, api_key: str, model="stt-async-v6", language_hints=("ru", "en"),
                 poll_interval=0.2, timeout=60):
        self.api_key, self.model, self.language_hints = api_key, model, list(language_hints)
        self.poll_interval, self.timeout = poll_interval, timeout
        self.cleanups = set()

    async def transcribe(self, audio: bytes, filename="voice.ogg") -> str:
        file_id, tid, text = await asyncio.to_thread(self._transcribe, audio, filename)
        # Deleting server-side copies is off the critical path.
        job = asyncio.create_task(asyncio.to_thread(self._cleanup, file_id, tid))
        self.cleanups.add(job)
        job.add_done_callback(self.cleanups.discard)
        return text

    def _request(self, method, path, data=None, content_type=None):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise TranscriptionError(f"Soniox {method} {path.split('/')[2]} failed: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TranscriptionError(f"Soniox network error: {exc}") from None
        return json.loads(body) if body else {}

    def _transcribe(self, audio, filename):
        deadline = time.monotonic() + self.timeout
        boundary = "eivoh" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
        file_id = self._request("POST", "/v1/files", body, f"multipart/form-data; boundary={boundary}")["id"]
        tid = None
        try:
            payload = {"model": self.model, "file_id": file_id, "language_hints": self.language_hints,
                       "enable_language_identification": True}
            tid = self._request("POST", "/v1/transcriptions", json.dumps(payload).encode(), "application/json")["id"]
            while True:
                status = self._request("GET", f"/v1/transcriptions/{tid}")
                if status.get("status") == "completed":
                    break
                if status.get("status") == "error":
                    raise TranscriptionError(f"Soniox transcription failed: {status.get('error_message', 'unknown error')}")
                if time.monotonic() > deadline:
                    raise TranscriptionError(f"Soniox transcription timed out after {self.timeout} s")
                time.sleep(self.poll_interval)
            result = self._request("GET", f"/v1/transcriptions/{tid}/transcript")
            text = (result.get("text") or "".join(t.get("text", "") for t in result.get("tokens") or [])).strip()
        except BaseException:
            self._cleanup(file_id, tid)
            raise
        return file_id, tid, text

    def _cleanup(self, file_id, tid):
        for path in ([f"/v1/transcriptions/{tid}"] if tid else []) + [f"/v1/files/{file_id}"]:
            try:
                self._request("DELETE", path)
            except TranscriptionError as exc:
                log.warning("Soniox cleanup failed: %s", exc)
