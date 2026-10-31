# Adapted from LabAI/backend/app/services/folder_picker.py
"""One native folder dialog at a time, isolated from the web server event loop."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from .errors import AppError
from .workspace_tools import validate_workspace_root


PICKER_TIMEOUT_SECONDS = 180
_picker_lock = threading.Lock()


def _initial_directory(initial_path: str | None) -> Path:
    if initial_path:
        try:
            return validate_workspace_root(initial_path)
        except (AppError, OSError, ValueError):
            pass
    return Path.home()


def _picker_command(initial_path: Path) -> list[str]:
    # The Windows venv python.exe is a redirector which starts another python
    # process. This helper uses only the stdlib; launch the base interpreter so
    # cancellation owns the actual GUI process instead of just its redirector.
    python = getattr(sys, "_base_executable", None) or sys.executable
    return [python, "-I", "-B", str(Path(__file__).with_name("folder_picker_helper.py")), str(initial_path)]


async def pick_folder(initial_path: str | None = None) -> str | None:
    if not _picker_lock.acquire(blocking=False):
        raise AppError("FOLDER_PICKER_BUSY", "A folder selection window is already open. Finish or cancel it first.", status_code=409)
    process: subprocess.Popen | None = None
    files = ExitStack()
    try:
        stdout_file = files.enter_context(tempfile.TemporaryFile())
        stderr_file = files.enter_context(tempfile.TemporaryFile())
        try:
            startupinfo = None
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.lpDesktop = "winsta0\\default"
            # No shell: a path containing quotes, spaces or non-ASCII text stays
            # one argument. Launching is synchronous so cancellation cannot lose
            # the process handle between creation and assignment.
            process = subprocess.Popen(
                _picker_command(_initial_directory(initial_path)),
                stdin=subprocess.DEVNULL, stdout=stdout_file, stderr=stderr_file,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                startupinfo=startupinfo,
            )
        except OSError as exc:
            raise AppError("FOLDER_PICKER_UNAVAILABLE", "Cannot open the folder selection window. Enter the path manually.", status_code=503) from exc
        deadline = time.monotonic() + PICKER_TIMEOUT_SECONDS
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise AppError("FOLDER_PICKER_TIMEOUT", "Folder selection timed out. Try browsing again or enter the path manually.", status_code=408)
            await asyncio.sleep(0.1)
        stdout_file.seek(0)
        stdout = stdout_file.read(65536)
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise AppError("FOLDER_PICKER_UNAVAILABLE", "The folder selection window could not return a path. Enter the path manually.", status_code=503) from exc
        if process.returncode != 0 or not isinstance(result, dict) or "path" not in result:
            raise AppError("FOLDER_PICKER_UNAVAILABLE", "Cannot open the folder selection window on this desktop. Enter the path manually.", status_code=503)
        selected = result["path"]
        if selected is None or selected == "":
            return None
        if not isinstance(selected, str):
            raise AppError("FOLDER_PICKER_UNAVAILABLE", "The folder selector returned an invalid path. Enter the path manually.", status_code=503)
        return str(validate_workspace_root(selected))
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                # The helper never starts descendants. Killing it also closes its
                # Tk dialog; reaping before unlocking prevents old windows leaking.
                process.wait(timeout=5)
        finally:
            files.close()
            _picker_lock.release()
