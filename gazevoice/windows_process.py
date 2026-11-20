"""Own a Windows process tree with a kill-on-close Job Object.

The root starts suspended so no helper can escape before it joins the job.
GUI applications opened for the user deliberately do not use this owner.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes as w


class _Limits(ctypes.Structure):
    _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                ("Flags", w.DWORD), ("Minimum", ctypes.c_size_t), ("Maximum", ctypes.c_size_t),
                ("Active", w.DWORD), ("Affinity", ctypes.c_size_t), ("Priority", w.DWORD),
                ("Scheduling", w.DWORD)]


class _Io(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]


class _Extended(ctypes.Structure):
    _fields_ = [("Basic", _Limits), ("Io", _Io), ("ProcessMemory", ctypes.c_size_t),
                ("JobMemory", ctypes.c_size_t), ("PeakProcess", ctypes.c_size_t),
                ("PeakJob", ctypes.c_size_t)]


class _Thread(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("id", w.DWORD), ("pid", w.DWORD),
                ("priority", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]


def _api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
        ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
        ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ("CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE),
        ("Thread32First", [w.HANDLE, ctypes.POINTER(_Thread)], w.BOOL),
        ("Thread32Next", [w.HANDLE, ctypes.POINTER(_Thread)], w.BOOL),
        ("OpenThread", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ("ResumeThread", [w.HANDLE], w.DWORD),
        ("CloseHandle", [w.HANDLE], w.BOOL),
    ):
        fn = getattr(api, name)
        fn.argtypes, fn.restype = args, result
    return api


def own_and_resume(proc):
    api = _api()
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = _Extended()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        process = api.OpenProcess(0x0101, False, proc.pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not api.AssignProcessToJobObject(job, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            api.CloseHandle(process)
        snapshot = api.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        resumed = False
        try:
            entry = _Thread(size=ctypes.sizeof(_Thread))
            more = api.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.pid == proc.pid:
                    thread = api.OpenThread(2, False, entry.id)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        resumed = True
                    finally:
                        api.CloseHandle(thread)
                more = api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            api.CloseHandle(snapshot)
        if not resumed:
            raise OSError("Could not resume the Windows worker's primary thread.")
    except BaseException:
        api.CloseHandle(job)
        raise

    lock = threading.Lock()
    def close_tree(force=True):
        nonlocal job
        with lock:
            if job:
                if force:
                    api.TerminateJobObject(job, 1)
                api.CloseHandle(job)
                job = None
    proc.close_tree = close_tree
    def reap():
        try:
            proc.wait()
        finally:
            # Give helpers receiving the same CTRL_BREAK a moment to finish
            # their own handlers before reclaiming any that remain.
            time.sleep(0.25)
            close_tree(force=False)
    threading.Thread(target=reap, daemon=True, name=f"worker-tree-{proc.pid}").start()
    return proc
