"""Stands in for `mlagents-learn`. It prints the lines the real one printed on this Mac, and exits the same ways.

FAKE_ML_SCRIPT picks what it acts out:
  timeout         waits for `--timeout-wait` seconds, then dies with the Unity time-out traceback (exit 1);
                  with FAKE_ML_CONNECT=1 it connects and reports progress first, so Unity stops answering mid-training
  previous_run    the run name was already used (exit 1, quickly)
  port_busy       another run already holds the connection (exit 1)
  crash           dies with an error the service has no special sentence for (exit 2)
  wait_interrupt  waits for Play; on SIGINT saves a model and exits 0
  progress        connects and reports progress, then behaves like wait_interrupt
                  (FAKE_ML_PROGRESS = normal | none | no_episode)
  finish          connects, reports progress, saves a model, exits 0
  deaf            waits for Play and ignores SIGINT, so only SIGKILL ends it
  restart         connects and reports progress, then (each step waits for a file in FAKE_ML_DIR) "restart" prints the
                  worker-restart warning Unity stopping answering leaves, "listen" prints the Listening line again, and
                  "reconnect" connects again with more progress; then it behaves like wait_interrupt
  no_listening    never prints the Listening line: it connects (FAKE_ML_SHOW = connected) or reports progress
                  (FAKE_ML_SHOW = progress) at once, then behaves like wait_interrupt

FAKE_ML_DIR is a folder it writes into: argv.json (argv, cwd, and whether SIGINT was ignored when it started), main.pid, "armed" once its
signal handlers are in place, and "ready" once it is listening as well. FAKE_ML_HELPER = sigint | deaf starts a second process in the same process group, as the
real tool does: "sigint" notes the SIGINT it gets in helper.sigint and exits, "deaf" ignores it, and "quiet" ignores
it too and lets go of the output pipe, so only the process group still ties it to the run. Its pid goes to helper.pid. FAKE_ML_SAVE=0 makes the graceful exit save no model. FAKE_ML_LISTEN_DELAY=<seconds> is the slow start of the real tool
(about 3 s warm, 25-30 s the first time after an install): that long passes before it prints the Listening line. A SIGINT
cuts the wait short. Nothing here runs for longer than half a minute.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

LIFETIME = 30.0

BANNER = [
    "",
    "            ┐  ╖",
    "        ╓╖╬│╡  ││╬╖╖",
    " ╖╬│││││╬╜        ╙╬│││││╖╖                               ╗╗╗",
    " ╬╬╬╬╖││╦╖        ╖╬││╗╣╣╣╬      ╟╣╣╬    ╟╣╣╣             ╜╜╜  ╟╣╣",
    " ╬╬╬╜       ╬╬╣╣      ╙╝╣╣╬      ╙╣╣╣╗╖╓╗╣╣╣╜ ╟╣╣╬   ╣╣╣  ╣╣╣  ╟╣╣╦╓    ╣╣╣╣╣",
    "          ╙╬╬╬╣╣╣╜",
    "             ╙",
    "        ",
    " Version information:",
    "  ml-agents: 1.2.0.dev0,",
    "  ml-agents-envs: 1.2.0.dev0,",
    "  Communicator API: 1.5.0,",
    "  PyTorch: 2.8.0",
]
LISTENING = "[INFO] Listening on port 5004. Start training by pressing the Play button in the Unity Editor."
CONNECTED = [
    "[INFO] Connected to Unity environment with package version 4.2.0 and communication version 1.5.0",
    "[INFO] Connected new brain: Mover?team=0",
]
INTERRUPTED = "[INFO] Learning was interrupted. Please wait while the graph is generated."
MODEL_NAME = "Mover"

SITE = "/x/venv/lib/python3.10/site-packages"
TRACEBACK_HEAD = [
    "Traceback (most recent call last):",
    '  File "/x/venv/bin/mlagents-learn", line 12, in <module>',
    "    sys.exit(main())",
    f'  File "{SITE}/mlagents/trainers/learn.py", line 266, in run_cli',
    "    run_training(run_seed, options, num_areas)",
]
TIMEOUT_TAIL = [
    f'  File "{SITE}/mlagents/trainers/subprocess_env_manager.py", line 101, in recv',
    "    raise env_exception",
    "mlagents_envs.exception.UnityTimeOutException: The Unity environment took too long to respond. Make sure that :",
    "\t The environment does not need user interaction to launch",
    '\t The Agents\' Behavior Parameters > Behavior Type is set to "Default"',
    "\t The environment and the Python interface have compatible versions.",
    "\t If you're running on a headless server without graphics support, turn off display by either passing "
    "--no-graphics option or build your Unity executable as server build.",
]
PREVIOUS_TAIL = [
    f'  File "{SITE}/mlagents/trainers/directory_utils.py", line 26, in validate_existing_directories',
    "    raise UnityTrainerException(",
    "mlagents.trainers.exception.UnityTrainerException: Previous data from this run ID was found. "
    "Either specify a new run ID, use --resume to resume this run, or use the --force parameter to overwrite "
    "existing data.",
]
PORT_BUSY_TAIL = [
    "[ERROR] UnityEnvironment worker 0: environment raised an unexpected exception.",
    "Traceback (most recent call last):",
    f'  File "{SITE}/mlagents_envs/rpc_communicator.py", line 87, in check_port',
    '    s.bind(("localhost", port))',
    "OSError: [Errno 48] Address already in use",
    "",
    "During handling of the above exception, another exception occurred:",
    "",
    "Traceback (most recent call last):",
    f'  File "{SITE}/mlagents_envs/rpc_communicator.py", line 89, in check_port',
    "    raise UnityWorkerInUseException(self.worker_id)",
    "mlagents_envs.exception.UnityWorkerInUseException: Couldn't start socket communication because worker number 0 "
    "is still in use. You may need to manually close a previously opened environment or use a different worker number.",
]
CRASH_TAIL = [
    f'  File "{SITE}/mlagents/trainers/trainer_controller.py", line 172, in start_learning',
    "ValueError: something unexpected went wrong",
]

RESTART_LINES = [  # what Unity stopping answering mid-run leaves, as printed on this Mac
    "[WARNING] Restarting worker[0] after 'The Unity environment took too long to respond. Make sure that :'",
    "\t The environment does not need user interaction to launch",
    '\t The Agents\' Behavior Parameters > Behavior Type is set to "Default"',
    "\t The environment and the Python interface have compatible versions.",
]

HELPER_CODE = r"""
import os, signal, sys, time
mode, marker, pid_file = sys.argv[1:4]
if mode in ("deaf", "quiet"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if mode == "quiet":
        quiet = os.open(os.devnull, os.O_WRONLY)
        os.dup2(quiet, 1)
        os.dup2(quiet, 2)
else:
    def on_interrupt(*_):
        open(marker, "w").write("sigint")
        os._exit(0)
    signal.signal(signal.SIGINT, on_interrupt)
open(pid_file, "w").write(str(os.getpid()))
time.sleep(30)
"""


def say(line, stream=sys.stdout):
    print(line, file=stream, flush=True)


def flag_value(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def start_helper(folder):
    """A second process in this process group. Returns once it has set up its signal handling."""
    mode = os.environ.get("FAKE_ML_HELPER")
    if not mode or folder is None:
        return
    pid_file = folder / "helper.pid"
    subprocess.Popen([sys.executable, "-c", HELPER_CODE, mode, str(folder / "helper.sigint"), str(pid_file)])
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)


def save_model(results, run_id):
    if os.environ.get("FAKE_ML_SAVE") == "0":
        return
    folder = Path(results) / run_id
    (folder / MODEL_NAME).mkdir(parents=True, exist_ok=True)
    exported = folder / MODEL_NAME / f"{MODEL_NAME}-1000.onnx"
    exported.write_bytes(f"onnx for {MODEL_NAME}".encode())
    final = folder / f"{MODEL_NAME}.onnx"
    final.write_bytes(exported.read_bytes())
    say(f"[INFO] Exported {exported}")
    say(f"[INFO] Copied {exported} to {final}.")


def progress_line(step, seconds, reward):
    rewards = "No episode was completed since last summary." if reward is None else (
        f"Mean Reward: {reward:.3f}. Std of Reward: 0.500.")
    return f"[INFO] {MODEL_NAME}. Step: {step}. Time Elapsed: {seconds:.3f} s. {rewards} Training."


def wait_until(stop_flag):
    deadline = time.monotonic() + LIFETIME
    while not stop_flag["hit"] and time.monotonic() < deadline:
        time.sleep(0.02)
    return stop_flag["hit"]


def wait_for_file(path, stop_flag):
    """Wait until a test creates `path`. False if SIGINT came first, or it took half a minute."""
    deadline = time.monotonic() + LIFETIME
    while not stop_flag["hit"] and time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def main():
    args = sys.argv[1:]
    script = os.environ.get("FAKE_ML_SCRIPT", "wait_interrupt")
    folder = Path(os.environ["FAKE_ML_DIR"]) if os.environ.get("FAKE_ML_DIR") else None
    run_id = flag_value(args, "--run-id") or "run"
    results = flag_value(args, "--results-dir") or "results"
    if folder is not None:
        sigint = "ignored" if signal.getsignal(signal.SIGINT) == signal.SIG_IGN else "handled"
        (folder / "argv.json").write_text(json.dumps({"argv": args, "cwd": os.getcwd(), "sigint_at_start": sigint}))
        (folder / "main.pid").write_text(str(os.getpid()))
    for line in BANNER:
        say(line)

    if script == "previous_run":
        for line in TRACEBACK_HEAD + PREVIOUS_TAIL:
            say(line, sys.stderr)
        return 1
    if script == "port_busy":
        for line in PORT_BUSY_TAIL + TRACEBACK_HEAD:
            say(line, sys.stderr)
        return 1
    if script == "crash":
        for line in TRACEBACK_HEAD + CRASH_TAIL:
            say(line, sys.stderr)
        return 2

    hit = {"hit": False}
    if script == "deaf":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGINT, lambda *_: hit.update(hit=True))
    start_helper(folder)
    if folder is not None:
        (folder / "armed").write_text("armed")  # the signal handlers are in place, though it may not be listening yet
    delay = time.monotonic() + float(os.environ.get("FAKE_ML_LISTEN_DELAY", "0"))
    while not hit["hit"] and time.monotonic() < delay:
        time.sleep(0.02)
    if script == "no_listening":
        if os.environ.get("FAKE_ML_SHOW", "connected") == "progress":
            say(progress_line(10000, 20.1, 0.5))
        else:
            for line in CONNECTED:
                say(line)
    else:
        say(LISTENING)
    if folder is not None:
        (folder / "ready").write_text("ready")

    if script == "timeout":
        if os.environ.get("FAKE_ML_CONNECT") == "1":
            for line in CONNECTED:
                say(line)
            say(progress_line(10000, 20.1, 0.5))
        time.sleep(int(flag_value(args, "--timeout-wait") or 120))
        for line in TRACEBACK_HEAD + TIMEOUT_TAIL:
            say(line, sys.stderr)
        return 1
    if script == "deaf":
        time.sleep(LIFETIME)
        return 0

    if script == "restart":
        for line in CONNECTED:
            say(line)
        say(progress_line(10000, 20.1, 0.5))
        for gate, lines in (("restart", RESTART_LINES), ("listen", [LISTENING]), ("reconnect", CONNECTED)):
            if not wait_for_file(folder / gate, hit):
                break
            for line in lines:
                say(line)
        else:
            say(progress_line(30000, 61.5, 2.5))

    if script in ("progress", "finish"):
        for line in CONNECTED:
            say(line)
        shape = os.environ.get("FAKE_ML_PROGRESS", "normal")
        if shape == "no_episode":
            say(progress_line(10000, 20.1, None))
        elif shape == "normal":
            say(progress_line(10000, 20.1, 0.5))
            say(progress_line(20000, 41.25, 1.234))
        if script == "finish":
            say(progress_line(50000, 120.543, 2.0))
            save_model(results, run_id)
            return 0
    if wait_until(hit):
        say(INTERRUPTED)
        save_model(results, run_id)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
