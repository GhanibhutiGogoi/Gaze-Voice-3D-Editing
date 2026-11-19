"""Windows trainer entry point: translate CTRL_BREAK to Python's save-on-interrupt path.

Runs in the dedicated ML-Agents environment; no gazevoice install is required there.
"""
import signal


def main():
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    from mlagents.trainers.learn import main as train
    train()


if __name__ == "__main__":
    main()
