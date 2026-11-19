"""Offline fake command provider used by the integration tests."""

import fcntl
import json
import os
import sys
import time
from pathlib import Path


request = json.load(sys.stdin)
body = request["body"]
stage = request["schema"].split("-")[1]
assert request["schema"] == f"quillric-{stage}-request/1"
assert Path(body["audio_path"]).is_absolute()
assert Path(body["audio_path"]).is_file()
assert Path(os.environ["HOME"]).samefile(Path.cwd())
assert os.environ.get("QUILLRIC_UNLISTED_TEST") is None
schema = f"quillric-{stage}-response/1"
mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode == "timeout":
    time.sleep(60)
if mode == "grandchild":
    ready_read, ready_write = os.pipe()
    if os.fork() == 0:
        os.close(ready_read)
        with open("grandchild.lock", "w") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
            os.write(ready_write, b"1")
            os.close(ready_write)
            time.sleep(15)
        os._exit(0)
    os.close(ready_write)
    assert os.read(ready_read, 1) == b"1"
    os.close(ready_read)
if mode == "nonzero":
    sys.exit(9)
if mode == "check-env":
    assert os.environ.get("QUILLRIC_PASSED_TEST") == "listed"
    assert "PATH" not in os.environ
if mode == "require-flac":
    with Path(body["audio_path"]).open("rb") as source:
        assert source.read(4) == b"fLaC"
if mode == "wrong-schema":
    schema = "quillric-unknown-response/1"
if mode == "overrun":
    sys.stdout.write("0" * 65536)
    sys.exit()
if mode == "bad":
    print('{"schema":"duplicate","schema":"duplicate"}')
    sys.exit()
if mode == "error":
    print(json.dumps({"schema": schema, "status": "error", "category": "refused"}))
    sys.exit()
if stage == "transcription":
    text = "你好" if body["model"] == "first" else "再见"
    result = {"words": [{"text": text, "type": "word", "start": 0, "end": 0.4,
                         "speaker_id": "raw", "logprob": -0.25}],
              "language_code": "zh", "audio_duration_secs": 1}
    if mode == "unordered":
        word = result["words"][0]
        result["words"] = [dict(word, start=0.3), dict(word, start=0.1)]
    if mode == "positive-logprob":
        result["words"][0]["logprob"] = 0.25
else:
    result = {"assignments": {str(word["ordinal"]): "speaker_0" for word in body["words"]}}
    if mode == "wrong-assignment":
        result = {"assignments": {"999": "speaker_0"}}
print(json.dumps({"schema": schema, "status": "ok", "body": result}))
