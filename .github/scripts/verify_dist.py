"""Check release metadata, archive inventory and source-to-wheel equivalence."""
import email
import hashlib
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile

dist = Path("dist").resolve()
wheel = dist / "quillric-0.1.0-py3-none-any.whl"
sdist = dist / "quillric-0.1.0.tar.gz"
forbidden = {".env", ".venv", "fixtures", "out", "transcripts", "comparisons", "analysis", "__pycache__", ".git"}
with zipfile.ZipFile(wheel) as z:
    names = z.namelist()
    assert len(names) == len(set(names)), "duplicate wheel members"
    for n in names:
        path = Path(n)
        assert not path.is_absolute() and ".." not in path.parts
        assert path.parts[0] in {"transcribe", "quillric-0.1.0.dist-info"}
    wheel_files = {n: z.read(n) for n in names}
    metadata = email.message_from_bytes(z.read("quillric-0.1.0.dist-info/METADATA"))
    assert metadata["Name"] == "quillric" and metadata["Version"] == "0.1.0"
    assert metadata["Requires-Python"] == ">=3.9"
    assert b"transcribe = transcribe.cli:main" in z.read("quillric-0.1.0.dist-info/entry_points.txt")
    assert "transcribe/leveling-only.json" in wheel_files
    assert any(n.endswith("LICENSE") for n in wheel_files)
    assert not any(forbidden.intersection(Path(n).parts) for n in wheel_files)
with tarfile.open(sdist) as t, tempfile.TemporaryDirectory() as tmp:
    for member in t.getmembers():
        assert member.name.startswith("quillric-0.1.0/")
        assert not forbidden.intersection(Path(member.name).parts)
        assert member.isfile() or member.isdir(), "special file or link in sdist"
        assert ".." not in Path(member.name).parts
    allowed = set(subprocess.check_output(["git", "ls-files"], text=True).splitlines()) if Path(".git").exists() else None
    if allowed is not None:
        for member in t.getmembers():
            if member.isfile():
                relative = str(Path(member.name).relative_to("quillric-0.1.0"))
                assert relative in allowed or relative == "PKG-INFO", relative
    t.extractall(tmp, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    source = Path(tmp) / "quillric-0.1.0"
    assert (source / "LICENSE").is_file()
    metadata = email.message_from_bytes((source / "PKG-INFO").read_bytes())
    assert metadata["Name"] == "quillric" and metadata["Version"] == "0.1.0"
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(Path(tmp) / "rebuilt"), str(source)], check=True)
    with zipfile.ZipFile(Path(tmp) / "rebuilt" / wheel.name) as z:
        assert wheel_files == {n: z.read(n) for n in z.namelist()}, "sdist and checkout wheels differ"
for artifact in (wheel, sdist):
    print(hashlib.sha256(artifact.read_bytes()).hexdigest(), artifact.name)
