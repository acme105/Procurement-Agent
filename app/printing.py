"""HTML to PDF with headless Chrome. If Chrome is missing, print_pdf raises PdfError (nothing is faked)."""
from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from . import config


class PdfError(RuntimeError):
    pass


def chrome_available() -> bool:
    return Path(config.CHROME).exists()


def _chrome(args: list[str], html: str, out_path: Path, timeout: int = 120) -> None:
    """Run headless Chrome on `html` and wait for `out_path`. Headless Chrome (new mode) uses its own temporary
    profile; a --user-data-dir makes it linger after writing, so none is passed. If the process lingers after the
    file is complete, it is stopped."""
    if not chrome_available():
        raise PdfError(f"Headless Chrome not found at {config.CHROME}")
    with tempfile.TemporaryDirectory(prefix="aerchain-chrome-") as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(html, encoding="utf-8")
        extra = ["--no-sandbox"] if config.CHROME_NO_SANDBOX else []      # needed when Chrome runs as root or in a container
        cmd = [config.CHROME, *extra, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--disable-extensions", "--hide-scrollbars", "--allow-file-access-from-files", *args, page.as_uri()]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        t0, size, stable_since = time.time(), -1, None
        while True:
            if proc.poll() is not None:
                break
            if out_path.exists():
                s = out_path.stat().st_size
                if s > 0 and s == size:
                    stable_since = stable_since or time.time()
                    if time.time() - stable_since > 3:      # complete and unchanged: stop a lingering process
                        proc.kill()
                        proc.wait(5)
                        break
                else:
                    size, stable_since = s, None
            if time.time() - t0 > timeout:
                proc.kill()
                proc.wait(5)
                raise PdfError(f"Chrome timed out after {timeout}s")
            time.sleep(0.15)
        if proc.returncode not in (0, None, -9) and not (out_path.exists() and out_path.stat().st_size):
            err = proc.stderr.read().decode(errors="ignore")[-400:] if proc.stderr else ""
            raise PdfError(f"Chrome failed ({proc.returncode}): {err}")


def print_pdf(html: str, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    _chrome(["--no-pdf-header-footer", "--run-all-compositor-stages-before-draw", "--virtual-time-budget=8000",
             f"--print-to-pdf={out_path}"], html, out_path)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise PdfError(f"Chrome produced no PDF at {out_path}")
    return out_path
