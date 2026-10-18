"""Transcribe one audio file with a local MLX speech engine.

Runs inside GazeVoice's own speech venv, not the control center's Python, and
prints one JSON line. Engines are imported lazily so `--help` works anywhere.

    python speech_worker.py --engine parakeet --model mlx-community/parakeet-tdt-0.6b-v2 --audio cmd.wav
    python speech_worker.py --engine whisper --model mlx-community/whisper-large-v3-turbo --download-only
"""

from __future__ import annotations

import argparse
import json
import sys
import time


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", choices=["parakeet", "whisper"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--audio")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.download_only and not args.audio:
        parser.error("--audio is required unless --download-only")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.download_only:
        from huggingface_hub import snapshot_download

        print(json.dumps({"downloaded": snapshot_download(args.model)}))
        return 0

    started = time.monotonic()
    if args.engine == "parakeet":
        from parakeet_mlx import from_pretrained

        model = from_pretrained(args.model)
        loaded = time.monotonic()
        text = model.transcribe(args.audio).text
    else:
        import mlx_whisper

        loaded = time.monotonic()
        text = mlx_whisper.transcribe(args.audio, path_or_hf_repo=args.model)["text"]
    done = time.monotonic()
    print(json.dumps({
        "text": text.strip(),
        "seconds": round(done - loaded, 2),
        "load_seconds": round(loaded - started, 2),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
