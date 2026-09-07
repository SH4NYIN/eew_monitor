import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from eew_display import ConnectionStatus, ConsoleDisplay
from eew_intensity import meets_notification_threshold
from eew_notifications import NotificationQueue, notify


HEARTBEAT_TIMEOUT = 3 * 60
RECONNECT_INITIAL_DELAY = 5
RECONNECT_MAX_DELAY = 60
WEBSOCKET_URL = "wss://ws-api.wolfx.jp/jma_eew"

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

JSON_DIR = BASE_DIR / "json"
LOG_DIR = BASE_DIR / "logs"
JSON_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
EVENT_FILES = {}

LOGGER = logging.getLogger("eew")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False

def initialize_logging():
    """Called by the singleton receiver only; viewers must not hold log files."""
    if any(isinstance(handler, RotatingFileHandler) for handler in LOGGER.handlers):
        return
    log_handler = RotatingFileHandler(
        LOG_DIR / "eew.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    log_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    LOGGER.addHandler(log_handler)


def report(message, level=logging.INFO, display=None):
    """Write durable messages to the log and the selected presentation."""
    LOGGER.log(level, message)
    (display or ConsoleDisplay()).message(message, level)


def is_heartbeat(message):
    """
    Detect common application-level heartbeat message formats:
      heartbeat
      "heartbeat"
      {"type": "heartbeat"}
      {"event": "heartbeat"}
      {"action": "heartbeat"}
    """
    if isinstance(message, str):
        return message.lower() == "heartbeat"

    if isinstance(message, dict):
        heartbeat_fields = (
            message.get("type"),
            message.get("event"),
            message.get("action"),
        )

        return any(
            isinstance(value, str) and value.lower() == "heartbeat"
            for value in heartbeat_fields
        )

    return False


def parse_origin_time(value):
    """Parse a Wolfx earthquake origin time into a datetime."""
    if not isinstance(value, str):
        return None

    value = value.strip()
    for time_format in (
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(value, time_format)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_magnitude(value):
    """Format magnitude with one decimal place."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "Unknown"


def format_intensity(value):
    """Return the JMA intensity code as display text."""
    if value is None:
        return "Unknown"

    intensity = str(value).strip()
    if not intensity:
        return "Unknown"
    return intensity


def format_depth(value):
    """Format hypocenter depth in kilometres."""
    if value is None:
        return "? km"

    if isinstance(value, bool):
        return "? km"

    try:
        depth = float(value)
    except (TypeError, ValueError):
        depth_text = str(value).strip()
        if not depth_text or depth_text.lower() in {"unknown", "none", "null"}:
            return "? km"
        if depth_text.lower().endswith("km"):
            return depth_text
        return f"{depth_text} km"

    if depth.is_integer():
        depth_text = str(int(depth))
    else:
        depth_text = f"{depth:g}"

    return f"{depth_text} km"


def format_report_type(message):
    """Format the report serial and final-report state."""
    serial = get_serial(message)
    is_final = message.get("isFinal") is True

    if is_final:
        return "Final"

    return f"Rep {serial}" if serial is not None else "Rep ?"


def sanitize_filename_part(value, fallback):
    """Replace characters that aren't permitted in Windows filenames."""
    text = str(value).strip() if value is not None else ""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).rstrip(". ")
    return text[:60] or fallback


def build_eew_summary(message, include_report_type=True):
    """Build a compact summary from the important EEW fields."""
    hypocenter = str(message.get("Hypocenter") or "Unknown hypocenter").strip()
    magnitude = format_magnitude(message.get("Magunitude"))
    intensity = format_intensity(message.get("MaxIntensity"))
    depth = format_depth(message.get("Depth"))

    origin_time = parse_origin_time(message.get("OriginTime"))
    if origin_time is None:
        origin_text = str(
            message.get("OriginTime") or "Unknown origin time"
        ).strip()
    else:
        origin_text = origin_time.strftime("%Y-%m-%d %H:%M")

    status = "[CANCELLED] " if message.get("isCancel") is True else ""
    summary_parts = [
        f"{status}{hypocenter}",
        f"M{magnitude} ({intensity})",
        depth,
        origin_text,
    ]

    if include_report_type:
        summary_parts.append(format_report_type(message))

    summary_parts.append("JMA")
    return " | ".join(summary_parts)


def show_eew_notification(message):
    """Submit a qualifying report to the desktop, returning a nonfatal error if any."""
    if not meets_notification_threshold(message):
        return ""

    title = str(message.get("Title") or "EEW").strip()
    title_prefix = (
        "EEW Cancelled"
        if message.get("isCancel") is True
        else "EEW Alert"
    )
    notification_title = f"{title_prefix}「{title}」"

    try:
        notify(
            notification_title,
            build_eew_summary(message),
        )
    except Exception as error:
        return " ".join(f"{type(error).__name__}: {error}".split())[:300]
    return ""


def update_notification_status(warning, display, status):
    """Publish and log notification outage/recovery transitions, not every failure."""
    if warning == status.notification_warning:
        return
    status.notification_warning = warning
    if warning:
        LOGGER.warning("Desktop notifications unavailable: %s", warning)
    else:
        LOGGER.info("Desktop notification submission restored.")
    display.status(status)


def get_storage_time(message):
    """Use earthquake origin time for storage, falling back to current time."""
    if isinstance(message, dict):
        origin_time = parse_origin_time(message.get("OriginTime"))
        if origin_time is not None:
            return origin_time

    return datetime.now()


def build_json_directory(message):
    """Build json/year/month/day from the earthquake origin time."""
    storage_time = get_storage_time(message)
    return (
        JSON_DIR
        / storage_time.strftime("%Y")
        / storage_time.strftime("%m")
        / storage_time.strftime("%d")
    )


def build_json_filename(message):
    """Build a filename from origin time, hypocenter, magnitude, and intensity."""
    if not isinstance(message, dict):
        timestamp = get_storage_time(message).strftime("%Y%m%d_%H%M%S")
        return f"{timestamp}_JSON.json"

    time_part = get_storage_time(message).strftime("%Y%m%d_%H%M")
    hypocenter = sanitize_filename_part(
        message.get("Hypocenter"),
        "Unknown",
    )
    magnitude = sanitize_filename_part(
        format_magnitude(message.get("Magunitude")),
        "Unknown",
    )
    intensity = sanitize_filename_part(
        message.get("MaxIntensity"),
        "Unknown",
    )

    return f"{time_part}_{hypocenter}_M{magnitude}({intensity}).json"


def get_event_id(message):
    """Return the EventID used to group revisions of the same earthquake."""
    if not isinstance(message, dict):
        return None

    event_id = str(message.get("EventID") or "").strip()
    return event_id or None


def get_serial(message):
    """Return Serial as an integer, or None when it isn't usable."""
    if not isinstance(message, dict):
        return None

    value = message.get("Serial")
    if isinstance(value, bool):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_saved_messages(file_path):
    """Read single-object JSON, JSON Lines, or concatenated JSON objects."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []

    messages = []
    decoder = json.JSONDecoder()
    position = 0

    while position < len(content):
        while position < len(content) and content[position].isspace():
            position += 1

        if position >= len(content):
            break

        try:
            message, position = decoder.raw_decode(content, position)
        except json.JSONDecodeError:
            LOGGER.warning("Unable to parse saved EEW file: %s", file_path)
            return []

        if isinstance(message, dict):
            messages.append(message)

    return messages


def get_latest_saved_message(file_path):
    """Return the highest-Serial report stored for an event."""
    latest_message = None
    latest_serial = None

    for message in read_saved_messages(file_path):
        serial = get_serial(message)

        if latest_message is None:
            latest_message = message
            latest_serial = serial
            continue

        if serial is None:
            if latest_serial is None:
                latest_message = message
            continue

        if latest_serial is None or serial > latest_serial:
            latest_message = message
            latest_serial = serial

    return latest_message


def read_event_id_from_file(file_path):
    """Read an EventID from a saved EEW file."""
    messages = read_saved_messages(file_path)
    if not messages:
        return None
    return get_event_id(messages[0])


def find_event_file(event_id):
    """Find the file created for an EventID in this or an earlier run."""
    known_path = EVENT_FILES.get(event_id)
    if known_path is not None and known_path.exists():
        return known_path

    for file_path in JSON_DIR.rglob("*.json"):
        # Only inspect generated files; leave test.json and other samples alone.
        if re.match(r"^\d{8}_\d{4,6}_", file_path.name) is None:
            continue

        if read_event_id_from_file(file_path) == event_id:
            EVENT_FILES[event_id] = file_path
            return file_path

    return None


def make_unique_path(desired_path, current_path=None):
    """Prevent different events with the same filename from overwriting."""
    if desired_path == current_path or not desired_path.exists():
        return desired_path

    sequence = 1
    while True:
        candidate = desired_path.with_name(
            f"{desired_path.stem}_{sequence}{desired_path.suffix}"
        )
        if candidate == current_path or not candidate.exists():
            return candidate
        sequence += 1


def save_json_message(message):
    """
    Append increasing-Serial revisions of one EventID as JSON Lines.
    Return the current path and one of: created, updated, duplicate, stale.
    """
    event_id = get_event_id(message)
    output_path = find_event_file(event_id) if event_id else None
    save_status = "created"

    if output_path is None:
        desired_path = (
            build_json_directory(message)
            / build_json_filename(message)
        )
        desired_path.parent.mkdir(parents=True, exist_ok=True)
        output_path = make_unique_path(desired_path)
        file_mode = "x"
    else:
        latest_message = get_latest_saved_message(output_path)
        latest_serial = get_serial(latest_message)
        incoming_serial = get_serial(message)

        if latest_message == message:
            return output_path, "duplicate"

        if latest_serial is not None and incoming_serial is not None:
            if incoming_serial == latest_serial:
                return output_path, "duplicate"
            if incoming_serial < latest_serial:
                return output_path, "stale"

        save_status = "updated"
        file_mode = "a"

    with output_path.open(
        mode=file_mode,
        encoding="utf-8",
    ) as file:
        json.dump(
            message,
            file,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        file.write("\n")

    latest_path = make_unique_path(
        build_json_directory(message) / build_json_filename(message),
        current_path=output_path,
    )

    if latest_path != output_path:
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.rename(latest_path)
        output_path = latest_path

    if event_id:
        EVENT_FILES[event_id] = output_path

    return output_path, save_status


def load_recent_history(limit=100):
    """Load the latest revision of recent saved events without sending toasts."""
    files = sorted(
        (
            path for path in JSON_DIR.rglob("*.json")
            if re.match(r"^\d{8}_\d{4,6}_", path.name)
        ),
        key=lambda path: (path.name, str(path)),
        reverse=True,
    )
    history = []
    for path in files:
        message = get_latest_saved_message(path)
        if message is None or message.get("isTraining") is True:
            continue
        history.append((build_eew_summary(message), message))
        if len(history) >= limit:
            break
    return list(reversed(history))


class HeartbeatTimeout(TimeoutError):
    """The server stopped sending application-level heartbeats."""


async def receive_messages(websocket_url, display=None, status=None, notifications=None):
    display = display or ConsoleDisplay()
    status = status or ConnectionStatus()
    async with connect(websocket_url) as websocket:
        LOGGER.info("Connected to server. Waiting for messages...")
        status.phase = "connected"
        status.detail = ""
        status.retry_at = 0
        status.connected_at = time.monotonic()
        status.last_heartbeat = None
        display.status(status)

        last_heartbeat = time.monotonic()

        while True:
            remaining_time = (
                last_heartbeat
                + HEARTBEAT_TIMEOUT
                - time.monotonic()
            )

            if remaining_time <= 0:
                raise HeartbeatTimeout(
                    f"No heartbeat received for {HEARTBEAT_TIMEOUT} seconds."
                )

            try:
                raw_message = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=remaining_time,
                )
            except asyncio.TimeoutError as error:
                raise HeartbeatTimeout(
                    f"No heartbeat received for {HEARTBEAT_TIMEOUT} seconds."
                ) from error

            # Decode text carried in a binary WebSocket message.
            if isinstance(raw_message, bytes):
                try:
                    text_message = raw_message.decode("utf-8")
                except UnicodeDecodeError:
                    report(
                        f"Received non-UTF-8 binary message: {raw_message!r}",
                        logging.WARNING,
                        display,
                    )
                    continue
            else:
                text_message = raw_message

            # Parse JSON when possible.
            try:
                parsed_message = json.loads(text_message)
                is_json = True
            except (json.JSONDecodeError, TypeError):
                parsed_message = text_message
                is_json = False

            # Only a heartbeat resets the heartbeat deadline.
            if is_heartbeat(parsed_message):
                last_heartbeat = time.monotonic()
                status.last_heartbeat = last_heartbeat
                display.status(status)
                continue

            # Ignore training reports; they aren't real earthquake alerts.
            if (
                isinstance(parsed_message, dict)
                and parsed_message.get("isTraining") is True
            ):
                LOGGER.info(
                    "Ignored training report: EventID=%s Serial=%s",
                    get_event_id(parsed_message),
                    get_serial(parsed_message),
                )
                continue

            # Save ordinary JSON and report only accepted, current revisions.
            if is_json:
                _, save_status = save_json_message(parsed_message)

                if save_status == "duplicate":
                    LOGGER.info(
                        "Ignored duplicate report: EventID=%s Serial=%s",
                        get_event_id(parsed_message),
                        get_serial(parsed_message),
                    )
                    continue

                if save_status == "stale":
                    LOGGER.warning(
                        "Ignored stale report: EventID=%s Serial=%s",
                        get_event_id(parsed_message),
                        get_serial(parsed_message),
                    )
                    continue

                if isinstance(parsed_message, dict):
                    summary = build_eew_summary(parsed_message)

                    LOGGER.info(summary)
                    display.eew(summary, parsed_message)
                    if meets_notification_threshold(parsed_message):
                        if notifications is not None:
                            notifications.submit(parsed_message)
                        else:
                            warning = await asyncio.to_thread(show_eew_notification, parsed_message)
                            update_notification_status(warning, display, status)
                else:
                    report(
                        "Received non-object JSON message and saved it locally.",
                        display=display,
                    )
                continue

            # Print ordinary non-JSON messages.
            report(f"Received message: {parsed_message}", display=display)


async def run_with_reconnect(display=None, retry_requested=None):
    """Keep the receiver alive with bounded exponential reconnect delays."""
    display = display or ConsoleDisplay()
    retry_requested = retry_requested or asyncio.Event()
    status = ConnectionStatus()

    def notification_status(warning):
        update_notification_status(warning, display, status)

    async with NotificationQueue(show_eew_notification, notification_status, LOGGER) as notifications:
        await reconnect_loop(display, retry_requested, status, notifications)


async def reconnect_loop(display, retry_requested, status, notifications):
    """Reconnect independently of desktop notification delivery."""
    reconnect_delay = RECONNECT_INITIAL_DELAY

    while True:
        status.phase = "connecting"
        status.connected_at = None
        status.last_heartbeat = None
        display.status(status)

        try:
            await receive_messages(WEBSOCKET_URL, display, status, notifications)
        except Exception as error:
            # Expected network failures don't need a traceback on every retry.
            if isinstance(error, (OSError, WebSocketException, asyncio.TimeoutError)):
                status.detail = f"{type(error).__name__}: {error}"
            else:
                LOGGER.exception("WebSocket receiver stopped unexpectedly.")
                status.detail = f"Receiver error {type(error).__name__}: {error}"
        else:
            status.detail = "Server closed the connection"

        # Measure the actual established session, not time spent connecting.
        if (
            status.connected_at is not None
            and time.monotonic() - status.connected_at >= HEARTBEAT_TIMEOUT
        ):
            reconnect_delay = RECONNECT_INITIAL_DELAY
            status.attempt = 0

        status.attempt += 1
        status.phase = "retrying"
        status.detail = " ".join(status.detail.split())
        status.retry_at = time.monotonic() + reconnect_delay
        LOGGER.warning("%s. Reconnecting in %s seconds (failure %s).",
                       status.detail, reconnect_delay, status.attempt)
        retry_requested.clear()
        display.status(status)

        try:
            await asyncio.wait_for(retry_requested.wait(), timeout=reconnect_delay)
        except asyncio.TimeoutError:
            pass
        reconnect_delay = min(
            reconnect_delay * 2,
            RECONNECT_MAX_DELAY,
        )


async def main(argv=None):
    # Internal entrypoint used when a console build is packaged as one executable.
    if "--service-run" in (sys.argv[1:] if argv is None else argv):
        from eew_service import main as service_main

        arguments = list(sys.argv[1:] if argv is None else argv)
        arguments.remove("--service-run")
        return await service_main(["run", *arguments])
    parser = argparse.ArgumentParser(description="Wolfx / JMA EEW real-time monitor")
    parser.add_argument("--plain", action="store_true", help="plain-text mode (automatic when output is redirected)")
    parser.add_argument("--tui", action="store_true", help="force the interactive terminal interface")
    args = parser.parse_args(argv)
    if args.plain and args.tui:
        parser.error("--plain and --tui cannot be used together")
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    interactive = all(stream is not None and stream.isatty()
                      for stream in (sys.stdin, sys.stdout))
    if args.tui or (interactive and not args.plain):
        from eew_tui import EEWApp
        from eew_runtime import watch_service

        await EEWApp(watch_service).run_async()
    else:
        from eew_runtime import watch_service

        await watch_service(ConsoleDisplay(), asyncio.Event())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        LOGGER.info("Terminated.")
