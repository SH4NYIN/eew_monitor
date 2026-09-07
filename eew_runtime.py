"""Single-instance ownership and authenticated, loopback-only service IPC."""

import asyncio
import contextlib
import errno
import json
import os
import secrets
import subprocess
import sys
import time
from collections import OrderedDict, deque
from dataclasses import asdict
from pathlib import Path

from eew_display import ConnectionStatus


BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
RUNTIME_DIR = BASE_DIR / ".runtime"
PROTOCOL_VERSION = 1
MAX_FRAME = 8 * 1024 * 1024


class ServiceUnavailable(Exception):
    pass


class ServiceStopped(ServiceUnavailable):
    pass


class InstanceLock:
    """An OS-held file lock, automatically released even after a process crash."""

    def __init__(self, directory):
        self.path = Path(directory) / "receiver.lock"
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        self.file = stream
        return True

    def release(self):
        if self.file is not None:
            self.file.close()
            self.file = None


def write_endpoint(directory, endpoint):
    """Publish metadata atomically; a stale PID alone never proves liveness."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "endpoint.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(endpoint, stream)
    os.replace(temporary, directory / "endpoint.json")


def read_endpoint(directory):
    try:
        endpoint = json.loads((Path(directory) / "endpoint.json").read_text(encoding="utf-8"))
        if endpoint.get("protocol") != PROTOCOL_VERSION:
            raise ServiceUnavailable("Incompatible service protocol. Stop the old receiver first.")
        if not isinstance(endpoint.get("port"), int) or not 0 < endpoint["port"] < 65536:
            raise ValueError("invalid port")
        if not isinstance(endpoint.get("token"), str) or not endpoint["token"]:
            raise ValueError("missing token")
        return endpoint
    except (OSError, ValueError, AttributeError) as error:
        raise ServiceUnavailable("No valid service endpoint found.") from error


async def request(command="status", *, directory=RUNTIME_DIR, endpoint=None, **parameters):
    endpoint = endpoint or read_endpoint(directory)
    if endpoint.get("state") == "stopped":
        raise ServiceStopped("Service stopped.")

    async def exchange():
        reader, writer = await asyncio.open_connection("127.0.0.1", endpoint["port"], limit=MAX_FRAME)
        try:
            payload = dict(parameters, command=command, token=endpoint["token"], protocol=PROTOCOL_VERSION)
            writer.write(json.dumps(payload).encode("utf-8") + b"\n")
            await writer.drain()
            response = json.loads(await reader.readline())
            if not response.get("ok") or response.get("instance") != endpoint["token"]:
                raise ServiceUnavailable(response.get("error", "Service authentication failed."))
            return response
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    try:
        return await asyncio.wait_for(exchange(), timeout=3)
    except (OSError, asyncio.TimeoutError, ValueError, AttributeError) as error:
        raise ServiceUnavailable("Service is temporarily unreachable.") from error


class ServiceHub:
    """Only the receiver publishes reports. Clients never notify or save JSON."""

    def __init__(self, directory=RUNTIME_DIR):
        self.directory = Path(directory)
        self.token = secrets.token_hex(32)
        self.connection = ConnectionStatus()
        self.retry_requested = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.stopping = False
        self.sequence = 0
        self.events = OrderedDict()
        self.updates = deque(maxlen=2000)
        self.notice = ""
        self.endpoint = None
        self.clients = set()

    def status(self, status):
        self.connection = status

    def message(self, message, level=None):
        self.notice = str(message)

    def eew(self, summary, message, historical=False):
        self.sequence += 1
        event_id = str(message.get("EventID") or "").strip()
        key = f"event:{event_id}" if event_id else f"anonymous:{self.sequence}"
        entry = {
            "key": key, "sequence": self.sequence, "summary": summary,
            "message": dict(message), "historical": historical,
            "received_at": "" if historical else time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.events[key] = entry
        self.updates.append(entry)
        while len(self.events) > 1000:
            oldest = next(key for key in self.events if key != entry["key"])
            del self.events[oldest]

    def snapshot(self, instance=None, after=None):
        reset = (instance != self.token or not isinstance(after, int)
                 or after > self.sequence
                 or (self.updates and after < self.updates[0]["sequence"] - 1))
        entries = sorted(self.events.values(), key=lambda entry: entry["sequence"]) if reset else [
            entry for entry in self.updates if entry["sequence"] > after
        ]
        return dict(self.info(), events=entries, sequence=self.sequence, reset=bool(reset), notice=self.notice)

    def info(self):
        return {
            "ok": True, "instance": self.token, "pid": os.getpid(),
            "connection": asdict(self.connection), "stopping": self.stopping,
        }

    async def handle_client(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        stop_after_reply = False
        try:
            payload = json.loads(await asyncio.wait_for(reader.readline(), 3))
            token = payload.get("token")
            if (not isinstance(token, str) or not secrets.compare_digest(token, self.token)
                    or payload.get("protocol") != PROTOCOL_VERSION):
                response = {"ok": False, "error": "Service authentication failed."}
            else:
                command = payload.get("command")
                if command == "snapshot":
                    response = self.snapshot(payload.get("instance"), payload.get("after"))
                elif command == "retry":
                    if self.connection.phase == "retrying":
                        self.retry_requested.set()
                    response = self.info()
                elif command == "stop":
                    self.stopping = True
                    self.endpoint["state"] = "stopped"
                    write_endpoint(self.directory, self.endpoint)
                    response = self.info()
                    stop_after_reply = True
                elif command == "status":
                    response = self.info()
                else:
                    response = {"ok": False, "error": "Unknown service command."}
            writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
        except (OSError, ValueError, AttributeError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.clients.discard(task)
            if stop_after_reply:
                self.stop_requested.set()

    async def open(self):
        server = await asyncio.start_server(self.handle_client, "127.0.0.1", 0, limit=65536)
        self.endpoint = {
            "protocol": PROTOCOL_VERSION, "pid": os.getpid(), "token": self.token,
            "port": server.sockets[0].getsockname()[1], "state": "running",
        }
        try:
            write_endpoint(self.directory, self.endpoint)
        except OSError:
            server.close()
            await server.wait_closed()
            raise
        return server


def spawn_service(directory=RUNTIME_DIR):
    """Detach the same Python environment; never invoke a shell."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    interpreter = Path(sys.executable)
    if getattr(sys, "frozen", False):
        command = [str(interpreter), "--service-run"]
    else:
        command = [str(interpreter), str(BASE_DIR / "eew_service.py"), "run"]
    command += ["--runtime-dir", str(directory.resolve())]
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    with (directory / "startup.log").open("ab") as errors:
        return subprocess.Popen(command, stderr=errors, **options)


async def ensure_service(directory=RUNTIME_DIR, *, launcher=spawn_service, timeout=15):
    """Concurrent starters may spawn, but only the OS-lock owner can receive."""
    try:
        return await request(directory=directory)
    except ServiceUnavailable:
        pass
    try:
        previous_instance = read_endpoint(directory)["token"]
    except ServiceUnavailable:
        previous_instance = None
    child = launcher(directory)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = await request(directory=directory)
            if response["stopping"]:
                raise ServiceStopped("Service is stopping.")
            return response
        except ServiceStopped:
            # An explicit stop of the instance we just launched wins over startup.
            if read_endpoint(directory)["token"] != previous_instance:
                raise
        except ServiceUnavailable:
            if child.poll() not in (None, 0):
                raise ServiceUnavailable(f"Service failed to start. See {Path(directory) / 'startup.log'}")
        if child.poll() == 0:
            probe = InstanceLock(directory)
            if probe.acquire():
                probe.release()
                child = launcher(directory)
        await asyncio.sleep(0.1)
    raise ServiceUnavailable(f"Service startup timed out. See {Path(directory) / 'startup.log'}")


async def watch_service(display, retry_requested, directory=RUNTIME_DIR):
    """Attach without owning the receiver; intentional stops aren't auto-restarted."""
    instance, after, notice = None, None, ""
    try:
        await ensure_service(directory)
    except ServiceUnavailable as error:
        display.status(ConnectionStatus(phase="service_unavailable", detail=str(error)))
    while True:
        try:
            if retry_requested.is_set():
                retry_requested.clear()
                await request("retry", directory=directory)
            reply = await request("snapshot", directory=directory, instance=instance, after=after)
            if reply["stopping"]:
                raise ServiceStopped("Service stopped.")
            if hasattr(display, "backend"):
                display.backend(reply["pid"])
            display.status(ConnectionStatus(**reply["connection"]))
            for entry in reply["events"]:
                display.eew(entry["summary"], entry["message"], historical=entry["historical"],
                            received_at=entry["received_at"], source_key=entry["key"])
            if reply["notice"] != notice:
                notice = reply["notice"]
                if notice:
                    display.message(notice)
            instance, after = reply["instance"], reply["sequence"]
        except ServiceStopped:
            display.status(ConnectionStatus(phase="service_stopped"))
            retry_requested.clear()
            while not retry_requested.is_set():
                # Reattach if another viewer or the launcher explicitly starts it.
                try:
                    if read_endpoint(directory).get("state") == "running":
                        break
                except ServiceUnavailable:
                    pass
                try:
                    await asyncio.wait_for(retry_requested.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
            if retry_requested.is_set():
                retry_requested.clear()
                try:
                    await ensure_service(directory)
                except ServiceUnavailable as error:
                    display.status(ConnectionStatus(phase="service_unavailable", detail=str(error)))
        except ServiceUnavailable:
            display.status(ConnectionStatus(phase="service_unavailable", detail="Lost service connection; attempting recovery"))
            await asyncio.sleep(1)
            try:
                try:
                    endpoint = read_endpoint(directory)
                except ServiceUnavailable:
                    endpoint = {}
                if endpoint.get("state") == "stopped":
                    continue
                await ensure_service(directory)
            except ServiceUnavailable:
                pass
        try:
            await asyncio.wait_for(retry_requested.wait(), 0.5)
        except asyncio.TimeoutError:
            pass
