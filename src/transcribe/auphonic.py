#!/usr/bin/env python3
"""Process one audio file with configurable Auphonic algorithms."""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import mimetypes
import os
import re
import shlex
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable


API_BASE = "https://auphonic.com"
TERMINAL_STATUS_DONE = 3
TERMINAL_STATUS_ERROR = 2

DEFAULT_ALGORITHMS: dict[str, Any] = {
    "denoise": True,
    "denoisemethod": "dynamic",
    "denoiseamount": 12,
    "deverbamount": 6,
    "debreathamount": -1,
    "leveler": True,
    "levelerstrength_speech": 110,
    "levelerstrength_music": -1,
    "compressor_speech": "medium",
    "compressor_music": "same",
    "msclassifier": "on",
    "filtering": True,
    "filtermethod": "bwe",
    "normloudness": True,
    "loudnesstarget": -16,
    "maxpeak": -1,
    "loudnessmethod": "dialog",
    "silence_cutter": False,
    "filler_cutter": False,
    "cough_cutter": False,
    "music_cutter": False,
}

ALL_PROCESSING_OFF: dict[str, Any] = {
    "denoise": False,
    "leveler": False,
    "filtering": False,
    "normloudness": False,
    "silence_cutter": False,
    "filler_cutter": False,
    "cough_cutter": False,
    "music_cutter": False,
}


class AuphonicError(RuntimeError):
    """A safe-to-display Auphonic workflow error."""


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urllib.parse.urlsplit(url)
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), parsed.hostname, parsed.port if parsed.port is not None else default_port


class CrossOriginAuthStripper(urllib.request.HTTPRedirectHandler):
    """Keep Auphonic auth on same-origin redirects, never send it elsewhere."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        new_url = _normalized_request_url(new_url)
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )
        if redirected is None:
            return None
        old_origin = _origin(request.full_url)
        new_origin = _origin(new_url)
        if old_origin != new_origin:
            redirected.remove_header("Authorization")
        return redirected


def _normalized_request_url(url: str) -> str:
    """Return a credential-free HTTPS URL with request-target characters encoded."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("expected an absolute HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("download URLs must not contain credentials")
        _ = parsed.port  # Reject malformed or out-of-range ports before making a request.
        path = urllib.parse.quote(
            parsed.path,
            safe="/%:@!$&'()*+,;=-._~",
        )
        query = urllib.parse.quote(
            parsed.query,
            safe="=&%:@!$'()*+,;/?-._~",
        )
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, path, query, parsed.fragment)
        )
    except (UnicodeError, ValueError) as error:
        raise AuphonicError(f"Auphonic returned an invalid download URL: {error}") from error


def production_payload(
    source: Path,
    output_basename: str | None = None,
    algorithms: dict[str, Any] | None = None,
    output_file: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the complete, explicit processing configuration."""
    return {
        "metadata": {"title": f"Processed: {source.name}"},
        "output_basename": output_basename or f"{source.stem}-auphonic",
        "output_files": [dict(output_file or {"format": "wav"})],
        "algorithms": dict(DEFAULT_ALGORITHMS if algorithms is None else algorithms),
    }


def _parse_override(override: str) -> tuple[str, Any]:
    key, separator, raw_value = override.partition("=")
    if not separator or not key.strip():
        raise AuphonicError(f"Invalid --set value {override!r}; expected KEY=VALUE")
    key = key.strip()
    raw_value = raw_value.strip()
    if not raw_value:
        raise AuphonicError(f"Invalid --set value {override!r}; VALUE cannot be empty")
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        value = raw_value
    return key, value


def resolve_algorithms(
    config_path: Path | None,
    no_default_algorithms: bool,
    overrides: list[str],
) -> dict[str, Any]:
    """Resolve defaults, an all-off config baseline, and CLI overrides."""
    if config_path is not None:
        try:
            document = json.loads(config_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise AuphonicError(f"Could not read algorithm config {config_path}: {error}") from error
        except json.JSONDecodeError as error:
            raise AuphonicError(
                f"Algorithm config {config_path} is invalid JSON: {error}"
            ) from error
        if not isinstance(document, dict) or not all(
            isinstance(key, str) for key in document
        ):
            raise AuphonicError("Algorithm config must be a JSON object")
        algorithms = {**ALL_PROCESSING_OFF, **document}
    elif no_default_algorithms:
        algorithms = dict(ALL_PROCESSING_OFF)
    else:
        algorithms = dict(DEFAULT_ALGORITHMS)

    for override in overrides:
        key, value = _parse_override(override)
        algorithms[key] = value
    return algorithms


def allocate_numbered_basename(
    destination_dir: Path, source: Path, output_suffix: str
) -> str:
    """Atomically allocate the next monotonically increasing output number."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    counter_path = destination_dir / ".experiment-counter"
    with counter_path.open("a+", encoding="utf-8") as counter:
        fcntl.flock(counter.fileno(), fcntl.LOCK_EX)
        counter.seek(0)
        counter_text = counter.read().strip()
        if counter_text and not counter_text.isdecimal():
            raise AuphonicError(f"Invalid experiment counter in {counter_path}")
        recorded = int(counter_text) if counter_text else 0
        existing = [
            int(match.group(1))
            for path in destination_dir.iterdir()
            if (match := re.match(r"^(\d+)-", path.name))
        ]
        experiment_number = max([recorded, *existing], default=0) + 1
        counter.seek(0)
        counter.truncate()
        counter.write(f"{experiment_number}\n")
        counter.flush()
        os.fsync(counter.fileno())
    return f"{experiment_number:03d}-{source.stem}{output_suffix}"


def _parse_dotenv_value(raw_value: str) -> str:
    lexer = shlex.shlex(raw_value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    values = list(lexer)
    return values[0] if values else ""


def load_api_key() -> str:
    """Load the API key from the environment or a nearby .env without logging it."""
    for variable in ("AUPHONIC_API_KEY", "AUPHONIC_KEY"):
        if value := os.environ.get(variable):
            return value

    candidates = (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env")
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        for line in resolved.read_text(encoding="utf-8").splitlines():
            match = re.match(
                r"^\s*(?:export\s+)?(AUPHONIC_API_KEY|AUPHONIC_KEY)\s*=\s*(.*)$",
                line,
            )
            if match and (value := _parse_dotenv_value(match.group(2))):
                return value
    raise AuphonicError(
        "No Auphonic key found; set AUPHONIC_API_KEY or add it to .env"
    )


def _decode_json(body: bytes, context: str) -> dict[str, Any]:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuphonicError(f"{context} returned a non-JSON response") from error
    if not isinstance(document, dict):
        raise AuphonicError(f"{context} returned an unexpected JSON value")
    return document


class AuphonicClient:
    def __init__(self, api_key: str, api_base: str = API_BASE, timeout: float = 60.0):
        self.api_key = api_key
        self.api_base = _normalized_request_url(api_base).rstrip("/")
        parsed_base = urllib.parse.urlsplit(self.api_base)
        if parsed_base.query or parsed_base.fragment:
            raise AuphonicError("API base URL must not contain a query or fragment")
        self.timeout = timeout

    def _request_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            _normalized_request_url(f"{self.api_base}{path}"),
            data=body, headers=headers, method=method
        )
        opener = urllib.request.build_opener(CrossOriginAuthStripper())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                document = _decode_json(response.read(), path)
        except urllib.error.HTTPError as error:
            error_body = error.read()
            message = f"HTTP {error.code}"
            try:
                error_document = _decode_json(error_body, path)
                message = str(error_document.get("error_message") or message)
            except AuphonicError:
                pass
            raise AuphonicError(f"Auphonic rejected {path}: {message}") from error
        except urllib.error.URLError as error:
            raise AuphonicError(f"Could not reach Auphonic for {path}: {error.reason}") from error

        if document.get("status_code") != 200:
            message = document.get("error_message") or "unknown API error"
            form_errors = document.get("form_errors")
            if form_errors:
                message = f"{message}; fields: {json.dumps(form_errors, sort_keys=True)}"
            raise AuphonicError(f"Auphonic rejected {path}: {message}")
        data = document.get("data")
        if not isinstance(data, dict):
            raise AuphonicError(f"Auphonic returned no data for {path}")
        return data

    def create_production(
        self,
        source: Path,
        output_basename: str,
        algorithms: dict[str, Any],
        output_file: dict[str, Any] | None = None,
    ) -> str:
        data = self._request_json(
            "POST",
            "/api/productions.json",
            production_payload(source, output_basename, algorithms, output_file),
        )
        production_id = data.get("uuid")
        if not isinstance(production_id, str) or not production_id:
            raise AuphonicError("Auphonic did not return a production UUID")
        return production_id

    def update_production(
        self,
        production_id: str,
        source: Path,
        output_basename: str,
        algorithms: dict[str, Any],
    ) -> None:
        self._request_json(
            "POST",
            f"/api/production/{production_id}.json",
            production_payload(source, output_basename, algorithms),
        )

    def upload(
        self,
        production_id: str,
        source: Path,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(_normalized_request_url(self.api_base))
        if parsed.scheme != "https" or not parsed.hostname:
            raise AuphonicError(f"Unsupported API base URL: {self.api_base}")
        port = parsed.port
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            port=port,
            timeout=self.timeout,
            context=ssl.create_default_context(),
        )

        boundary = f"----auphonic-{uuid.uuid4().hex}"
        safe_name = source.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="input_file"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        source_size = source.stat().st_size
        content_length = len(prefix) + source_size + len(suffix)
        base_path = parsed.path.rstrip("/")
        upload_path = f"{base_path}/api/production/{production_id}/upload.json"

        try:
            connection.putrequest("POST", upload_path)
            connection.putheader("Authorization", f"Bearer {self.api_key}")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(content_length))
            connection.endheaders()
            connection.send(prefix)
            bytes_sent = 0
            if progress is not None:
                progress(bytes_sent, source_size)
            with source.open("rb") as audio:
                while chunk := audio.read(1024 * 1024):
                    connection.send(chunk)
                    bytes_sent += len(chunk)
                    if progress is not None:
                        progress(bytes_sent, source_size)
            connection.send(suffix)
            response = connection.getresponse()
            response_body = response.read()
        except OSError as error:
            raise AuphonicError(f"Audio upload failed: {error}") from error
        finally:
            connection.close()

        if response.status != 200:
            raise AuphonicError(f"Auphonic rejected the audio upload: HTTP {response.status}")
        document = _decode_json(response_body, "audio upload")
        if document.get("status_code") != 200:
            message = document.get("error_message") or "unknown API error"
            raise AuphonicError(f"Auphonic rejected the audio upload: {message}")

    def start(self, production_id: str) -> None:
        self._request_json("POST", f"/api/production/{production_id}/start.json")

    def details(self, production_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/api/production/{production_id}.json")

    def download(
        self,
        download_url: str,
        destination: Path,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> None:
        opener = urllib.request.build_opener(CrossOriginAuthStripper())
        partial = destination.with_name(f".{destination.name}.part")
        try:
            url = _normalized_request_url(download_url)
            headers = ({"Authorization": f"Bearer {self.api_key}"}
                       if _origin(url) == _origin(self.api_base) else {})
            request = urllib.request.Request(url, headers=headers)
            with opener.open(request, timeout=self.timeout) as response:
                raw_length = response.headers.get("Content-Length")
                total = int(raw_length) if raw_length and raw_length.isdecimal() else None
                bytes_received = 0
                if progress is not None:
                    progress(bytes_received, total)
                with partial.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        bytes_received += len(chunk)
                        if progress is not None:
                            progress(bytes_received, total)
            partial.replace(destination)
        except (
            OSError,
            ValueError,
            http.client.HTTPException,
            urllib.error.URLError,
        ) as error:
            partial.unlink(missing_ok=True)
            raise AuphonicError(f"Could not download the result: {error}") from error


def _result_download_url(details: dict[str, Any], output_format: str = "wav") -> str:
    output_files = details.get("output_files")
    if not isinstance(output_files, list):
        raise AuphonicError("Finished production has no output file list")
    for output in output_files:
        if isinstance(output, dict) and output.get("format") == output_format:
            download_url = output.get("download_url")
            if isinstance(download_url, str) and download_url:
                return download_url
    raise AuphonicError(
        f"Finished production has no downloadable {output_format.upper()} output"
    )


def wait_for_completion(
    client: AuphonicClient,
    production_id: str,
    poll_interval: float,
    wait_timeout: float,
    status_callback: Callable[[dict[str, Any], bool], None] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + wait_timeout
    previous_status: object = None
    while time.monotonic() < deadline:
        details = client.details(production_id)
        status = details.get("status")
        status_string = str(details.get("status_string") or f"status {status}")
        status_changed = status != previous_status
        if status_callback is not None:
            status_callback(details, status_changed)
        elif status_changed:
            print(f"Auphonic: {status_string}", flush=True)
        previous_status = status
        if status == TERMINAL_STATUS_DONE:
            return details
        if status == TERMINAL_STATUS_ERROR:
            message = details.get("error_message") or "production failed"
            raise AuphonicError(f"Auphonic production {production_id} failed: {message}")
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
    raise AuphonicError(
        f"Timed out waiting for production {production_id}; it may still be running remotely"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_file", type=Path, help="audio file to process")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("out"), help="result directory (default: out)"
    )
    production_mode = parser.add_mutually_exclusive_group()
    production_mode.add_argument(
        "--resume-production",
        metavar="UUID",
        help="resume and download an existing production instead of submitting a new one",
    )
    production_mode.add_argument(
        "--rerun-production",
        metavar="UUID",
        help="update and rerun an existing production using its uploaded input",
    )
    parser.add_argument(
        "--output-suffix",
        default="-auphonic",
        help="suffix added to the source stem (default: -auphonic)",
    )
    algorithm_source = parser.add_mutually_exclusive_group()
    algorithm_source.add_argument(
        "--config",
        type=Path,
        help="JSON algorithm settings merged over an explicit all-processing-off baseline",
    )
    algorithm_source.add_argument(
        "--no-default-algorithms",
        action="store_true",
        help="start with every processing module disabled",
    )
    parser.add_argument(
        "--set",
        dest="algorithm_overrides",
        metavar="KEY=VALUE",
        action="append",
        default=[],
        help="override one algorithm parameter; repeat as needed (values accept JSON)",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="print the resolved algorithm JSON and exit without contacting Auphonic",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=10.0, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--wait-timeout", type=float, default=7200.0, help=argparse.SUPPRESS
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.audio_file.expanduser().resolve()
    if not source.is_file():
        print(f"error: audio file does not exist: {source}", file=sys.stderr)
        return 2
    if args.poll_interval <= 0 or args.wait_timeout <= 0:
        print("error: polling interval and timeout must be positive", file=sys.stderr)
        return 2
    if not args.output_suffix or any(character in args.output_suffix for character in "/\\\0"):
        print("error: output suffix must be nonempty and cannot contain path separators", file=sys.stderr)
        return 2

    try:
        algorithms = resolve_algorithms(
            args.config.expanduser().resolve() if args.config else None,
            args.no_default_algorithms,
            args.algorithm_overrides,
        )
        if args.print_config:
            print(json.dumps(algorithms, indent=2, sort_keys=True))
            return 0
        destination_dir = args.output_dir.expanduser().resolve()
        output_basename = allocate_numbered_basename(
            destination_dir, source, args.output_suffix
        )
        destination = destination_dir / f"{output_basename}.wav"
        api_key = load_api_key()
        client = AuphonicClient(api_key)
        if args.resume_production:
            production_id = args.resume_production
            print(f"Resuming production: {production_id}", flush=True)
        elif args.rerun_production:
            production_id = args.rerun_production
            print(f"Updating production: {production_id}", flush=True)
            client.update_production(
                production_id, source, output_basename, algorithms
            )
            print("Restarting processing with the existing input…", flush=True)
            client.start(production_id)
        else:
            print(f"Creating Auphonic production for {source.name}…", flush=True)
            production_id = client.create_production(
                source, output_basename, algorithms
            )
            print(f"Production: {production_id}", flush=True)
            print("Uploading audio…", flush=True)
            client.upload(production_id, source)
            print("Starting processing…", flush=True)
            client.start(production_id)
        details = wait_for_completion(
            client, production_id, args.poll_interval, args.wait_timeout
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
        client.download(_result_download_url(details), destination)
    except (AuphonicError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Result: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
