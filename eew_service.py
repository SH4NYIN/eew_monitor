"""Independent background receiver; the TUI is a client of this process."""

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

from eew_runtime import (
    RUNTIME_DIR, InstanceLock, ServiceHub, ServiceStopped, ServiceUnavailable,
    ensure_service, request, write_endpoint,
)
from eew_display import ConnectionStatus


async def serve(directory=RUNTIME_DIR, *, receiver=None, history_loader=None):
    lock = InstanceLock(directory)
    if not lock.acquire():
        return False
    hub = ServiceHub(directory)
    server = None
    tasks = []
    logger = None
    try:
        # Only the lock owner may open rotating logs, write reports or notify.
        if receiver is None:
            import eew as core

            core.initialize_logging()
            logger = core.LOGGER
            receiver = core.run_with_reconnect
            history_loader = core.load_recent_history
        server = await hub.open()
        if history_loader is not None:
            for summary, message in await asyncio.to_thread(history_loader):
                hub.eew(summary, message, historical=True)
        if hub.stop_requested.is_set():
            return True
        monitor = asyncio.create_task(receiver(hub, hub.retry_requested))
        stopped = asyncio.create_task(hub.stop_requested.wait())
        tasks = [monitor, stopped]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if monitor in done and not hub.stopping:
            await monitor
            raise RuntimeError("The background receiver exited unexpectedly.")
        return True
    except Exception:
        if logger is not None:
            logger.exception("Background service stopped unexpectedly.")
        raise
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            server.close()
            await server.wait_closed()
        clients = list(hub.clients)
        for task in clients:
            task.cancel()
        if clients:
            await asyncio.gather(*clients, return_exceptions=True)
        if hub.endpoint is not None:
            hub.endpoint["state"] = "stopped" if hub.stopping else "failed"
            with contextlib.suppress(OSError):
                write_endpoint(directory, hub.endpoint)
        if logger is not None:
            logger.info("Background receiver terminated.")
            # Release file handles before giving another process ownership.
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
        lock.release()


async def stop_service(directory=RUNTIME_DIR):
    probe = InstanceLock(directory)
    if probe.acquire():
        try:
            from eew_runtime import read_endpoint

            with contextlib.suppress(ServiceUnavailable):
                endpoint = read_endpoint(directory)
                endpoint["state"] = "stopped"
                write_endpoint(directory, endpoint)
        finally:
            probe.release()
        return
    try:
        await request("stop", directory=directory)
    except ServiceStopped:
        pass
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        probe = InstanceLock(directory)
        if probe.acquire():
            probe.release()
            return
        await asyncio.sleep(0.1)
    raise ServiceUnavailable("Stop requested, but the receiver has not exited. Check the logs.")


async def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--service-run" in arguments:
        arguments.remove("--service-run")
        arguments.insert(0, "run")
    parser = argparse.ArgumentParser(description="EEW background service manager")
    parser.add_argument("action", choices=("start", "status", "stop", "retry", "run"), nargs="?", default="start")
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    try:
        if args.action == "run":
            await serve(args.runtime_dir)
            return 0
        if args.action == "start":
            reply = await ensure_service(args.runtime_dir)
            message = f"Service ready, PID {reply['pid']}. Monitoring continues after this terminal closes."
        elif args.action == "stop":
            await stop_service(args.runtime_dir)
            message = "Service stopped. Press R in the TUI to start it again."
        else:
            reply = await request(args.action, directory=args.runtime_dir)
            status = ConnectionStatus(**reply["connection"])
            message = f"Service PID {reply['pid']} · {status.text()}"
        if sys.stdout is not None:
            print(message)
        return 0
    except ServiceUnavailable as error:
        if sys.stderr is not None:
            print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main()))
