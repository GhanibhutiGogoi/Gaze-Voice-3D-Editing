"""Windows trainer entry point: translate CTRL_BREAK to Python's save-on-interrupt path.

Runs in the dedicated ML-Agents environment; no gazevoice install is required there.
"""
import signal
import sys
from pathlib import Path


def main():
    # Python 3.10 puts this script's folder first. Its mlagents.py is our
    # integration module, not the ML-Agents package in the training environment.
    own_folder = Path(__file__).resolve().parent
    sys.path[:] = [entry for entry in sys.path if Path(entry or '.').resolve() != own_folder]
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    from mlagents.trainers.learn import main as train
    train()


if __name__ == "__main__":
    main()
