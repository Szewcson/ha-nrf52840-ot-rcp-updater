"""Nordic DFU invocation with fixed argument ownership and bounded logs."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile


class DfuError(RuntimeError):
    """The Nordic USB DFU tool could not install the verified package."""


class NordicDfuFlasher:
    """Program a known PCA10059 serial number with one verified ZIP package."""

    def __init__(self, executable: str = "nrfutil") -> None:
        self._executable = executable

    def flash(self, package: Path, serial_number: str) -> None:
        if not package.is_file() or package.suffix != ".zip":
            raise DfuError("Nordic DFU package is missing or is not a ZIP file")
        if shutil.which(self._executable) is None:
            raise DfuError("nrfutil is unavailable; install the nrfutil device command in this app image")

        command = [
            self._executable,
            "device",
            "program",
            "--firmware",
            str(package),
            "--serial-number",
            serial_number,
        ]
        with tempfile.NamedTemporaryFile(prefix="nrfutil-", suffix=".log", delete=True) as log_file:
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=300,
                )
            except OSError as err:
                raise DfuError(f"unable to execute nrfutil: {err}") from err
            except subprocess.TimeoutExpired as err:
                raise DfuError("nrfutil DFU exceeded the 300 second timeout") from err
            log_file.flush()
            log_file.seek(0, 2)
            size = log_file.tell()
            log_file.seek(max(size - 8192, 0))
            tail = log_file.read().decode("utf-8", "replace").strip()
        if completed.returncode != 0:
            raise DfuError(f"nrfutil device program failed ({completed.returncode}): {tail}")

