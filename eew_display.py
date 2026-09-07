"""Presentation contracts shared by the terminal UI and plain output."""

import math
import sys
import time
from dataclasses import dataclass


@dataclass
class ConnectionStatus:
    phase: str = "connecting"
    detail: str = ""
    attempt: int = 0
    retry_at: float = 0
    connected_at: float | None = None
    last_heartbeat: float | None = None
    notification_warning: str = ""

    def text(self):
        text = self.connection_text()
        if self.notification_warning:
            text += f" · Notifications unavailable: {self.notification_warning}"
        return text

    def connection_text(self):
        now = time.monotonic()
        if self.phase == "service_stopped":
            return "Service stopped · Not monitoring · Press R to start"
        if self.phase == "service_unavailable":
            return "Service unavailable · Reconnecting"
        if self.phase == "connected":
            heartbeat = (
                "Awaiting first heartbeat"
                if self.last_heartbeat is None
                else f"Heartbeat {int(max(0, now - self.last_heartbeat))}s ago"
            )
            return f"Connected · Monitoring · {heartbeat}"
        if self.phase == "retrying":
            seconds = math.ceil(max(0, self.retry_at - now))
            return (
                f"Disconnected · Retry in {seconds}s · Failures: {self.attempt}"
                f" · {self.detail}"
            )
        if self.phase == "connecting" and self.attempt:
            return f"Disconnected · Reconnecting (attempt {self.attempt}) · {self.detail}"
        return "Connecting to Wolfx / JMA EEW…"


class ConsoleDisplay:
    """For redirected output: emit outage/recovery transitions, not retries."""

    def __init__(self):
        self.in_outage = False
        self.was_connected = False
        self.service_phase = None
        self.notification_warning = ""

    def message(self, message, level=None):
        if sys.stdout is not None:
            print(message, flush=True)

    def eew(self, summary, message, historical=False, received_at=None, source_key=None):
        if not historical and message.get("isFinal") is True:
            self.message(summary)

    def status(self, status):
        if status.notification_warning != self.notification_warning:
            if status.notification_warning:
                self.message(f"Desktop notifications unavailable: {status.notification_warning}")
            elif self.notification_warning:
                self.message("Desktop notification warning cleared.")
            self.notification_warning = status.notification_warning
        if status.phase in {"service_stopped", "service_unavailable"}:
            if self.service_phase != status.phase:
                self.message(status.text())
            self.service_phase = status.phase
            return
        self.service_phase = None
        if status.phase == "retrying" and not self.in_outage:
            self.message(f"Connection lost; retrying automatically: {status.detail} (see logs/eew.log)")
            self.in_outage = True
        elif status.phase == "connected":
            if self.in_outage or not self.was_connected:
                self.message("Connection restored. Monitoring resumed." if self.in_outage else "Connected. Monitoring.")
            self.in_outage = False
            self.was_connected = True
