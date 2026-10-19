"""Transcribe speech with a local MLX engine. Runs inside GazeVoice's own speech venv.

One-shot (the dashboard's Test key):
    python speech_worker.py --engine parakeet --model mlx-community/parakeet-tdt-0.6b-v2 --audio cmd.wav
Download only (the Install key):
    python speech_worker.py --engine whisper --model mlx-community/whisper-large-v3-turbo --download-only
Serve (the voice loop; the model stays loaded):
    python speech_worker.py --serve --engine parakeet --model …
    → prints {"ready": true, "load_seconds": …}
    ← reads {"id": 1, "audio": "/path/cmd.wav", "prompt": "optional vocabulary"} per line
    → prints {"id": 1, "text": "…", "words": [{"text", "start", "end"}], "seconds": …} or {"id": 1, "error": "…"}

Engines are imported lazily, so `--help` and the tests work anywhere.
GAZEVOICE_FAKE_ENGINE=1 swaps in a canned engine (tests); =fail makes loading fail.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", choices=["parakeet", "whisper"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--audio")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)
    if not (args.download_only or args.serve or args.audio):
        parser.error("--audio is required unless --download-only or --serve")
    return args


def tokens_to_words(tokens: list[tuple[str, float, float]]) -> list[dict]:
    """Merge subword tokens into words. A token that starts with a space or "▁" starts a word."""
    words: list[dict] = []
    for text, start, end in tokens:
        starts_word = not words or text[:1].isspace() or text.startswith("▁")
        clean = text.replace("▁", " ").strip()
        if not clean:
            continue
        if starts_word:
            words.append({"text": clean, "start": round(start, 3), "end": round(end, 3)})
        else:
            words[-1]["text"] += clean
            words[-1]["end"] = round(end, 3)
    return words


def _megabytes(count: int) -> str:
    return f"{count / 1e6:,.0f} MB"


def _blob_bytes(folder: Path) -> int:
    blobs = folder / "blobs"
    return sum(f.stat().st_size for f in blobs.iterdir() if f.is_file()) if blobs.is_dir() else 0


def download(
    model: str,
    fetch: Callable[[str], str] | None = None,
    folder: Path | None = None,
    total: int | None = None,
    attempts: int = 4,
    every: float = 5.0,
    pause: float = 5.0,
) -> str:
    """Fetch a model into the Hugging Face cache, printing progress, and resume if it breaks off.

    Plain HTTPS only: Xet transfers have been seen to stall forever without timing out,
    while HTTPS times out on a stall and picks up where it left off.
    """
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
    # The Hub's "set a token for higher rate limits" notice would read like GazeVoice asking for a key.
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    if fetch is None:
        from huggingface_hub import constants, snapshot_download

        fetch = snapshot_download
        folder = Path(constants.HF_HUB_CACHE) / f"models--{model.replace('/', '--')}"
        try:
            total = sum(f.file_size or 0 for f in snapshot_download(model, dry_run=True))
        except Exception:  # noqa: BLE001 - without a total the progress lines just say how much so far
            total = None

    def progress() -> str:
        done = _blob_bytes(folder) if folder else 0
        return f"Downloaded {_megabytes(done)}" + (f" of {_megabytes(total)}" if total else "")

    print(f"Downloading {model}" + (f" ({_megabytes(total)})…" if total else "…"), flush=True)
    finished = threading.Event()

    def report() -> None:
        while not finished.wait(every):
            print(progress(), flush=True)

    threading.Thread(target=report, daemon=True).start()
    try:
        for attempt in range(1, attempts + 1):
            try:
                path = fetch(model)
                break
            except Exception as exc:  # noqa: BLE001 - network errors come in many types
                if attempt == attempts:
                    raise
                print(f"The download was interrupted ({type(exc).__name__}: {exc}). "
                      f"Resuming ({attempt + 1} of {attempts})…", flush=True)
                time.sleep(pause * attempt)
    finally:
        finished.set()
    print(progress(), flush=True)
    return path


class ParakeetEngine:
    def __init__(self, model: str):
        from parakeet_mlx import from_pretrained

        self.model = from_pretrained(model)

    def transcribe(self, audio: str, prompt: str | None = None) -> dict:
        result = self.model.transcribe(audio)
        tokens = [(t.text, t.start, t.end) for sentence in result.sentences for t in sentence.tokens]
        return {"text": result.text.strip(), "words": tokens_to_words(tokens)}


class WhisperEngine:
    def __init__(self, model: str):
        import mlx_whisper
        import numpy as np

        self.mlx_whisper = mlx_whisper
        self.model = model
        # mlx_whisper caches the loaded model per repo; one silent second loads it now.
        mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo=model)

    def transcribe(self, audio: str, prompt: str | None = None) -> dict:
        result = self.mlx_whisper.transcribe(
            audio, path_or_hf_repo=self.model, word_timestamps=True, initial_prompt=prompt or None
        )
        words = [
            {"text": w["word"].strip(), "start": round(w["start"], 3), "end": round(w["end"], 3)}
            for segment in result.get("segments", [])
            for w in segment.get("words", [])
            if w.get("word", "").strip()
        ]
        return {"text": result["text"].strip(), "words": words}


class FakeEngine:
    """Canned results so tests can drive the real protocol without MLX."""

    def __init__(self, model: str):
        if os.environ.get("GAZEVOICE_FAKE_ENGINE") == "fail":
            raise RuntimeError("could not load the model (fake failure)")

    def transcribe(self, audio: str, prompt: str | None = None) -> dict:
        if not Path(audio).exists():
            raise FileNotFoundError(f"No such audio file: {audio}")
        words = [("make", 0.1, 0.3), ("this", 0.4, 0.6), ("twice", 0.7, 0.9), ("as", 0.95, 1.0), ("tall", 1.05, 1.3)]
        return {"text": "make this twice as tall", "words": [{"text": w, "start": s, "end": e} for w, s, e in words]}


def load_engine(engine: str, model: str):
    if os.environ.get("GAZEVOICE_FAKE_ENGINE"):
        return FakeEngine(model)
    return ParakeetEngine(model) if engine == "parakeet" else WhisperEngine(model)


def serve(args: argparse.Namespace) -> int:
    # Our replies own the real stdout; anything a library prints goes to stderr instead.
    replies = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    sys.stdout = sys.stderr
    started = time.monotonic()
    engine = load_engine(args.engine, args.model)
    replies.write(json.dumps({"ready": True, "load_seconds": round(time.monotonic() - started, 2)}) + "\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            rid = request.get("id")
        except ValueError:
            replies.write(json.dumps({"id": None, "error": "bad request"}) + "\n")
            continue
        began = time.monotonic()
        try:
            result = engine.transcribe(request["audio"], request.get("prompt"))
            reply = {"id": rid, **result, "seconds": round(time.monotonic() - began, 2)}
        except Exception as exc:  # noqa: BLE001 - report, keep serving
            reply = {"id": rid, "error": f"{type(exc).__name__}: {exc}"}
        replies.write(json.dumps(reply) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.download_only:
        print(json.dumps({"downloaded": download(args.model)}))
        return 0
    if args.serve:
        return serve(args)

    started = time.monotonic()
    engine = load_engine(args.engine, args.model)
    loaded = time.monotonic()
    result = engine.transcribe(args.audio)
    done = time.monotonic()
    print(json.dumps({
        "text": result["text"],
        "words": result["words"],
        "seconds": round(done - loaded, 2),
        "load_seconds": round(loaded - started, 2),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
