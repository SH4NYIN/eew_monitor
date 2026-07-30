import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from win11toast import notify
from websockets.asyncio.client import connect


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

LOGGER = logging.getLogger("eew_test")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False

if not LOGGER.handlers:
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


def report(message, level=logging.INFO):
    """Write a message to the rotating log and to the console when available."""
    LOGGER.log(level, message)

    if sys.stdout is not None:
        print(message)


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


def sanitize_filename_part(value, fallback):
    """Replace characters that aren't permitted in Windows filenames."""
    text = str(value).strip() if value is not None else ""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).rstrip(". ")
    return text[:60] or fallback


def build_eew_summary(message):
    """Build a compact summary from the important EEW fields."""
    hypocenter = str(message.get("Hypocenter") or "Unknown hypocenter").strip()
    magnitude = format_magnitude(message.get("Magunitude"))
    intensity = format_intensity(message.get("MaxIntensity"))

    origin_time = parse_origin_time(message.get("OriginTime"))
    if origin_time is None:
        origin_text = str(
            message.get("OriginTime") or "Unknown origin time"
        ).strip()
    else:
        origin_text = origin_time.strftime("%Y-%m-%d %H:%M")

    status = "CANCELLED | " if message.get("isCancel") is True else ""
    return (
        f"{status} | {hypocenter} | {origin_text} | "
        f"M{magnitude} ({intensity}) | JMA"
    )


def show_eew_notification(message):
    """Show a non-blocking Windows toast for an accepted EEW report."""
    title = str(message.get("Title") or "EEW").strip()
    notification_title = (
        f"EEW CANCELLED | {title}"
        if message.get("isCancel") is True
        else f"EEW ALERT | {title}"
    )

    try:
        notify(
            notification_title,
            build_eew_summary(message),
        )
    except Exception:
        LOGGER.exception("Unable to display the Windows toast notification.")


def build_json_filename(message):
    """Build a filename from origin time, hypocenter, magnitude, and intensity."""
    if not isinstance(message, dict):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{timestamp}_JSON.json"

    origin_time = parse_origin_time(message.get("OriginTime"))
    if origin_time is None:
        origin_time = datetime.now()

    time_part = origin_time.strftime("%Y%m%d_%H%M")
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
    except OSError:
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

    for file_path in JSON_DIR.glob("*.json"):
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
        desired_path = JSON_DIR / build_json_filename(message)
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
        JSON_DIR / build_json_filename(message),
        current_path=output_path,
    )

    if latest_path != output_path:
        output_path.rename(latest_path)
        output_path = latest_path

    if event_id:
        EVENT_FILES[event_id] = output_path

    return output_path, save_status


async def receive_messages(websocket_url):
    async with connect(websocket_url) as websocket:
        report("Connected to server. Waiting for messages...")

        last_heartbeat = time.monotonic()

        while True:
            remaining_time = (
                last_heartbeat
                + HEARTBEAT_TIMEOUT
                - time.monotonic()
            )

            if remaining_time <= 0:
                raise RuntimeError(
                    f"No heartbeat received for {HEARTBEAT_TIMEOUT} seconds."
                )

            try:
                raw_message = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=remaining_time,
                )
            except TimeoutError as error:
                raise RuntimeError(
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
                    report(summary)
                    show_eew_notification(parsed_message)
                else:
                    report(
                        "Received non-object JSON message and saved it locally."
                    )
                continue

            # Print ordinary non-JSON messages.
            report(f"Received message: {parsed_message}")


async def run_with_reconnect():
    """Keep the receiver alive with bounded exponential reconnect delays."""
    reconnect_delay = RECONNECT_INITIAL_DELAY

    while True:
        attempt_started = time.monotonic()

        try:
            await receive_messages(WEBSOCKET_URL)
        except Exception as error:
            connected_duration = time.monotonic() - attempt_started
            if connected_duration >= HEARTBEAT_TIMEOUT:
                reconnect_delay = RECONNECT_INITIAL_DELAY

            LOGGER.exception("WebSocket receiver stopped unexpectedly.")
            report(
                f"Connection lost: {error}. "
                f"Reconnecting in {reconnect_delay} seconds...",
                logging.WARNING,
            )
        else:
            report(
                f"Connection closed. "
                f"Reconnecting in {reconnect_delay} seconds...",
                logging.WARNING,
            )

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(
            reconnect_delay * 2,
            RECONNECT_MAX_DELAY,
        )


async def main():
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    await run_with_reconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        report("Terminated.")
