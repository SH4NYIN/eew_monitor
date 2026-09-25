"""Interactive presentation; receiving, persistence and notifications stay in the core."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Static

from eew_display import ConnectionStatus
from eew_themes import DEFAULT_THEME, INTENSITY_CODES, get_theme


INTENSITY_COLORS = get_theme().colors
UNKNOWN_INTENSITY_COLOR = get_theme().unknown


def intensity_color(value, theme=DEFAULT_THEME):
    """Accept JMA Japanese/Chinese labels, +/- codes and full-width variants."""
    return get_theme(theme).color(value)


def intensity_style(value, theme=DEFAULT_THEME):
    palette = get_theme(theme)
    if palette.badges:
        return Style(color=palette.foreground(value), bgcolor=palette.color(value), bold=True)
    return Style(color=palette.color(value))


# key, heading, proportional weight, minimum terminal-cell width.
COLUMNS = (
    ("source", "Src", 5, 4),
    ("origin", "Origin time", 22, 19),
    ("place", "Location", 30, 24),
    ("magnitude", "M", 7, 5),
    ("intensity", "Int.", 7, 5),
    ("depth", "Depth", 10, 7),
    ("serial", "Rep.", 8, 5),
    ("state", "Status", 11, 6),
)
MAX_EVENTS = 1000


def column_widths(viewport):
    """Distribute space by fixed ratios, respecting CJK-aware minimum widths."""
    available = max(sum(column[3] for column in COLUMNS), viewport - 2 * len(COLUMNS))
    widths = {}
    remaining = list(COLUMNS)
    while remaining:
        weight = sum(column[2] for column in remaining)
        constrained = [column for column in remaining
                       if available * column[2] / weight < column[3]]
        if constrained:
            for column in constrained:
                widths[column[0]] = column[3]
                available -= column[3]
                remaining.remove(column)
            continue
        shares = {column[0]: available * column[2] / weight for column in remaining}
        rounded = {key: int(value) for key, value in shares.items()}
        for key in sorted(shares, key=lambda key: shares[key] - rounded[key], reverse=True)[
            :available - sum(rounded.values())
        ]:
            rounded[key] += 1
        widths.update(rounded)
        break
    return widths


def field_text(value, fallback="—"):
    return str(value).strip() if value is not None and str(value).strip() else fallback


@dataclass
class EventRecord:
    message: dict
    historical: bool
    received_at: str
    theme: str = DEFAULT_THEME

    def fields(self):
        message = self.message
        origin = field_text(message.get("OriginTime"))
        try:
            origin = datetime.fromisoformat(origin.replace("/", "-")).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
        magnitude = message.get("Magunitude")
        try:
            magnitude = f"{float(magnitude):.1f}"
        except (TypeError, ValueError):
            magnitude = "—"
        depth = field_text(message.get("Depth"))
        if depth != "—" and not depth.lower().endswith("km"):
            depth += " km"
        state = "CANCEL" if message.get("isCancel") is True else (
            "FINAL" if message.get("isFinal") is True else "UPDATE"
        )
        return {
            "source": "HIS" if self.historical else "LIVE",
            "origin": origin,
            "place": field_text(message.get("Hypocenter"), "Unknown"),
            "magnitude": magnitude,
            "intensity": field_text(message.get("MaxIntensity")),
            "depth": depth,
            "serial": field_text(message.get("Serial")),
            "state": state,
        }

    @property
    def style(self):
        palette = get_theme(self.theme)
        return Style(color=palette.color(self.message.get("MaxIntensity")) if palette.colored_rows else palette.row_foreground,
                     bold=self.message.get("isFinal") is True or self.message.get("isCancel") is True)

    @property
    def intensity_style(self):
        return self.style + intensity_style(self.message.get("MaxIntensity"), self.theme)

    def cells(self):
        fields = self.fields()
        cells = []
        for key, _, _, _ in COLUMNS:
            text = Text(fields[key], style=self.style, no_wrap=True, overflow="ellipsis")
            if key == "intensity":
                text.style = self.intensity_style
                if get_theme(self.theme).badges:
                    text = Text(f" {fields[key]} ", style=self.intensity_style,
                                no_wrap=True, overflow="ellipsis")
            if key == "state" and self.message.get("isCancel") is True:
                text.stylize("bold reverse")
            cells.append(text)
        return cells

    def overview(self):
        """A compact three-column overview, with wrapping for the full place name."""
        fields = self.fields()
        table = Table.grid(expand=True, padding=(0, 1))
        for ratio in (5, 3, 2):
            table.add_column(ratio=ratio, overflow="fold")
        intensity = Text(f"M {fields['magnitude']}    Int. ", style=self.style)
        if get_theme(self.theme).badges:
            intensity.append(f" {fields['intensity']} ", style=self.intensity_style)
        else:
            intensity.append(fields["intensity"])
        table.add_row(Text(fields["place"], style=self.style),
                      intensity,
                      Text(f"{fields['state']}  #{fields['serial']}", style=self.style))
        table.add_row(Text(fields["origin"]), Text(f"Depth {fields['depth']}"), Text(fields["source"]))
        return table


class EventDetails(ModalScreen):
    BINDINGS = [("escape,enter", "dismiss", "Back")]
    DEFAULT_CSS = """
    EventDetails { align: center middle; }
    EventDetails > VerticalScroll {
        width: 90%; height: 80%; border: round $primary; background: $surface; padding: 1 2;
    }
    """

    def __init__(self, record):
        super().__init__()
        self.record = record

    def compose(self):
        with VerticalScroll():
            yield Static(self.details_table(), id="event-details-content")

    def details_table(self):
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=12)
        table.add_column(ratio=1, overflow="fold")
        fields = self.record.fields()
        for key, heading, _, _ in COLUMNS:
            style = self.record.intensity_style if key == "intensity" else self.record.style
            value = fields[key]
            if key == "intensity" and get_theme(self.record.theme).badges:
                value = f" {value} "
            table.add_row(Text(heading), Text(value, style=style))
        table.add_row("EventID", Text(field_text(self.record.message.get("EventID"))))
        table.add_row("Received", Text(self.record.received_at or "HIS · No receipt time in this session"))
        table.add_row("", "Esc / Enter to return")
        return table

    def set_intensity_theme(self, theme):
        self.record.theme = theme
        self.query_one("#event-details-content", Static).update(self.details_table())


def intensity_legend(theme=DEFAULT_THEME):
    palette = get_theme(theme)
    text = Text("Intensity  " if palette.colored_rows else f"Intensity · {palette.label}  ", no_wrap=True)
    for code in (*INTENSITY_CODES, "?"):
        if palette.badges:
            text.append(f" {code} ", style=intensity_style(code, theme))
            text.append(" ")
        else:
            text.append(code + ("  " if code != "?" else ""), style=intensity_style(code, theme))
    return text


class EventTable(DataTable):
    """Stable event rows with independently scrollable history."""

    def watch_scroll_y(self, old_value, new_value):
        super().watch_scroll_y(old_value, new_value)
        if new_value < old_value and not self.app.syncing_view:
            self.app.set_follow(False)

    def on_resize(self):
        if self.app.ready:
            self.call_after_refresh(self.app.resize_columns)

    def on_mouse_scroll_up(self):
        self.app.set_follow(False)

    def on_click(self):
        self.app.set_follow(False)


class EEWApp(App):
    TITLE = "EEW Monitor · Wolfx / JMA"
    CSS = """
    Screen { layout: vertical; }
    #connection { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #connection.online { color: $success; }
    #connection.warning { color: $warning; }
    #latest { height: auto; min-height: 4; max-height: 8; border: round $primary; }
    #events { height: 1fr; border: round $secondary; }
    #notice { height: 1; padding: 0 1; display: none; color: $warning; }
    #intensity-legend { height: 1; padding: 0 1; }
    #view-status { height: 1; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding("q,ctrl+c", "quit", "Quit view", priority=True),
        Binding("r", "retry", "Retry"),
        Binding("f", "follow", "Follow"),
        Binding("enter", "details", "Details"),
        Binding("up", "history('up')", "Up", show=False, priority=True),
        Binding("down", "history('down')", "Down", show=False, priority=True),
        Binding("pageup", "history('page_up')", "Page up", priority=True),
        Binding("pagedown", "history('page_down')", "Page down", priority=True),
        Binding("home", "history('home')", "First", show=False, priority=True),
        Binding("end", "follow", "Latest", show=False, priority=True),
    ]

    def __init__(self, monitor, history_loader=None, *, intensity_theme=DEFAULT_THEME):
        super().__init__()
        self.intensity_theme = get_theme(intensity_theme).key
        self.monitor = monitor
        self.history_loader = history_loader
        self.connection = ConnectionStatus()
        self.retry_requested = asyncio.Event()
        self.following = True
        self.unseen = 0
        self.records = {}
        self.latest_key = None
        self.anonymous_sequence = 0
        self.widths = {}
        self.ready = False
        self.syncing_view = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="connection", markup=False)
        yield Static("No reports yet. Waiting for earthquake information.", id="latest", markup=False)
        yield EventTable(id="events", show_row_labels=False, cursor_type="row",
                         cursor_foreground_priority="renderable", cursor_background_priority="renderable",
                         zebra_stripes=True)
        yield Static(id="notice", markup=False)
        yield Static(intensity_legend(self.intensity_theme), id="intensity-legend")
        yield Static(id="view-status", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one("#latest").border_title = "Latest update"
        self.query_one("#events").border_title = "Events · One row per earthquake · Enter for details"
        self.query_one(EventTable).focus()
        self.ready = True
        self.resize_columns()
        self.render_status()
        self.set_follow(True)
        self.set_interval(1, self.render_status)
        self.run_worker(self.start_monitor(), name="EEW receiver")

    async def start_monitor(self):
        if self.history_loader is not None:
            history = await asyncio.to_thread(self.history_loader)
            for summary, message in history:
                self.eew(summary, message, historical=True)
        await self.monitor(self, self.retry_requested)

    def status(self, status):
        self.connection = status
        if status.phase == "service_stopped":
            self.sub_title = "Service stopped · R to start"
        elif status.phase == "service_unavailable":
            self.sub_title = "Service unavailable"
        self.render_status()

    def backend(self, pid):
        self.sub_title = f"Service PID {pid} · Q closes viewer only"

    def render_status(self):
        bar = self.query_one("#connection", Static)
        bar.update(self.connection.text())
        bar.set_class(self.connection.phase == "connected", "online")
        bar.set_class(bool(self.connection.detail or self.connection.notification_warning)
                      or self.connection.phase == "service_stopped", "warning")
        # The full error remains accessible even when the terminal is narrow.
        bar.tooltip = self.connection.text()

    def message(self, message, level=logging.INFO):
        # Service notices must never be mistaken for an earthquake row.
        notice = self.query_one("#notice", Static)
        notice.update(str(message))
        notice.tooltip = str(message)
        notice.display = True

    def eew(self, summary, message, historical=False, received_at=None, source_key=None):
        event_id = str(message.get("EventID") or "").strip()
        if event_id:
            key = f"event:{event_id}"
        elif source_key:
            key = source_key
        else:
            # Without an EventID, don't accidentally merge unrelated earthquakes.
            self.anonymous_sequence += 1
            key = f"anonymous:{self.anonymous_sequence}"
        record = EventRecord(dict(message), historical,
                             received_at if received_at is not None else (
                                 "" if historical else datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                             theme=self.intensity_theme)
        table = self.query_one(EventTable)
        exists = key in self.records
        self.records[key] = record
        if exists:
            for column, cell in zip(COLUMNS, record.cells()):
                table.update_cell(key, column[0], cell)
        else:
            table.add_row(*record.cells(), key=key)
        self.latest_key = key
        self.query_one("#latest", Static).update(record.overview())
        # Keep a bounded in-memory view; all revisions remain stored on disk.
        if self.following:
            self.trim_events()
            self.follow_latest()
        if not historical and not self.following:
            self.unseen += 1
        self.update_view_status()

    def set_intensity_theme(self, key):
        """Repaint existing content without replacing rows, moving the cursor or notifying."""
        self.intensity_theme = get_theme(key).key
        if not self.ready:
            return
        screen = self.screen_stack[0]
        table = screen.query_one(EventTable)
        for event_key, record in self.records.items():
            record.theme = key
            for column, cell in zip(COLUMNS, record.cells()):
                table.update_cell(event_key, column[0], cell)
        screen.query_one("#intensity-legend", Static).update(intensity_legend(key))
        if self.latest_key in self.records:
            screen.query_one("#latest", Static).update(self.records[self.latest_key].overview())
        for detail in self.screen_stack:
            if isinstance(detail, EventDetails):
                detail.set_intensity_theme(key)

    def trim_events(self):
        table = self.query_one(EventTable)
        while len(self.records) > MAX_EVENTS:
            key = next(key for key in self.records if key != self.latest_key)
            del self.records[key]
            table.remove_row(key)

    def resize_columns(self):
        table = self.query_one(EventTable)
        widths = column_widths(max(0, table.content_size.width - table.scrollbar_size_vertical))
        if self.widths == widths:
            return
        selected = table.ordered_rows[table.cursor_row].key.value if table.row_count else None
        scroll_x, scroll_y = table.scroll_x, table.scroll_y
        self.widths = widths
        self.syncing_view = True
        table.clear(columns=True)
        for key, heading, _, _ in COLUMNS:
            table.add_column(heading, key=key, width=widths[key])
        for key, record in self.records.items():
            table.add_row(*record.cells(), key=key)
        if self.following and self.latest_key:
            self.follow_latest()
        else:
            if selected in self.records:
                table.move_cursor(row=table.get_row_index(selected), scroll=False)
            self.call_after_refresh(table.scroll_to, x=scroll_x, y=scroll_y, animate=False, force=True)
        self.call_after_refresh(self.finish_view_sync)

    def finish_view_sync(self):
        self.syncing_view = False

    def follow_latest(self):
        if self.latest_key in self.records:
            self.syncing_view = True
            table = self.query_one(EventTable)
            table.move_cursor(row=table.get_row_index(self.latest_key), animate=False)
            self.call_after_refresh(self.finish_view_sync)

    def set_follow(self, enabled):
        self.following = enabled
        if enabled:
            self.unseen = 0
        self.update_view_status()

    def update_view_status(self):
        text = "Following updates" if self.following else f"Browsing · {self.unseen} updates · F / End to follow"
        self.query_one("#view-status", Static).update(text + f" · {len(self.records)} events · ← → scroll horizontally")

    def action_history(self, direction):
        self.set_follow(False)
        actions = {"up": "action_cursor_up", "down": "action_cursor_down",
                   "home": "action_scroll_top", "page_up": "action_page_up",
                   "page_down": "action_page_down"}
        getattr(self.query_one(EventTable), actions[direction])()

    def action_follow(self):
        self.set_follow(True)
        self.trim_events()
        self.follow_latest()

    def action_details(self):
        table = self.query_one(EventTable)
        if table.row_count:
            key = table.ordered_rows[table.cursor_row].key.value
            self.set_follow(False)
            self.push_screen(EventDetails(self.records[key]))

    def on_data_table_row_selected(self):
        self.action_details()

    def action_retry(self):
        if self.connection.phase in {"retrying", "service_stopped", "service_unavailable"}:
            self.retry_requested.set()
