"""Native desktop notification backends and a receiver-owned delivery queue."""

import asyncio
import contextlib
from html import escape
import shutil
import subprocess
import sys


LINUX_TIMEOUT = 3
QUEUE_LIMIT = 64


class NotificationError(RuntimeError):
    """A desktop notification could not be submitted to the operating system."""


def notify(title, body):
    """Submit plain text to the native desktop; successful submission is not delivery."""
    if sys.platform == "win32":
        # Import lazily so missing desktop dependencies never prevent monitoring.
        from win11toast import notify as windows_notify

        windows_notify(title, body)
        return
    if sys.platform.startswith("linux"):
        executable = shutil.which("notify-send")
        if executable is None:
            raise NotificationError("notify-send is missing; install libnotify-bin on Ubuntu/Debian")
        try:
            result = subprocess.run(
                [executable, "--app-name=EEW Monitor", "--urgency=critical",
                 "--", title, escape(body, quote=False)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=LINUX_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise NotificationError("Linux desktop notification service timed out") from error
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace")
            detail = " ".join(detail.split())[:240]
            raise NotificationError(
                f"Linux desktop notification failed: {detail or 'check the user desktop / D-Bus session'}"
            )
        return
    raise NotificationError(f"Desktop notifications are not implemented for {sys.platform}")


class NotificationQueue:
    """Serialize blocking OS calls outside the receiver event loop, with bounded memory."""

    def __init__(self, sender, on_status, logger):
        self.sender = sender
        self.on_status = on_status
        self.logger = logger
        self.queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.run(), name="desktop-notifications")
        return self

    def submit(self, message):
        if self.queue.full():
            # Prioritize current alerts over stale queued revisions during a burst.
            self.queue.get_nowait()
            self.queue.task_done()
            self.logger.warning("Notification queue full; dropped the oldest pending alert. Its archive is retained.")
        self.queue.put_nowait(dict(message))

    async def run(self):
        while True:
            message = await self.queue.get()
            try:
                warning = await asyncio.to_thread(self.sender, message)
                self.on_status(warning)
            finally:
                self.queue.task_done()

    async def __aexit__(self, *args):
        # Do not replay a backlog of alerts after an explicit stop or process failure.
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
