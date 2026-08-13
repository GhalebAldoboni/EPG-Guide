"""Parse elCinema TV listings and serialize them as XMLTV."""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


DUBAI = ZoneInfo("Asia/Dubai")
ARABIC_MONTHS = {
    "يناير": 1,
    "فبراير": 2,
    "مارس": 3,
    "ابريل": 4,
    "أبريل": 4,
    "مايو": 5,
    "يونيو": 6,
    "يوليو": 7,
    "اغسطس": 8,
    "أغسطس": 8,
    "سبتمبر": 9,
    "اكتوبر": 10,
    "أكتوبر": 10,
    "نوفمبر": 11,
    "ديسمبر": 12,
}
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(صباح(?:اً|ا)?|مساء(?:ً|ا)?)")
DURATION_RE = re.compile(r"\[(\d+)\s*(?:دقيقة|دقائق|دقيقتين)\]")
CHANNEL_PATH_RE = re.compile(r"^/en/tvguide/(\d+)/$")
MAX_DISCOVERED_CHANNELS = 250
MAX_PROGRAMMES_PER_CHANNEL = 1_000


class SourceParseError(ValueError):
    """The source page did not contain a recognizable guide."""


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    url: str
    icon_url: str | None = None
    aliases: tuple[str, ...] = ()
    alias_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Programme:
    channel_id: str
    title: str
    start: datetime
    stop: datetime
    description: str | None = None
    category: str | None = None


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: "_Node | None" = None
    children: list["_Node"] | None = None
    chunks: list[str] | None = None

    def __post_init__(self) -> None:
        self.children = [] if self.children is None else self.children
        self.chunks = [] if self.chunks is None else self.chunks

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    def text(self) -> str:
        pieces = list(self.chunks or [])
        for child in self.children or []:
            pieces.append(child.text())
        return _clean(" ".join(pieces))

    def descendants(self, tag: str | None = None) -> Iterable["_Node"]:
        for child in self.children or []:
            if tag is None or child.tag == tag:
                yield child
            yield from child.descendants(tag)


class _TreeParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.current = self.root
        self.node_count = 1
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.node_count += 1
        if self.node_count > 100_000:
            raise SourceParseError("source HTML contains too many nodes")
        node = _Node(tag, {key: value or "" for key, value in attrs}, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node
            self.depth += 1
            if self.depth > 100:
                raise SourceParseError("source HTML nesting is too deep")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.current = self.current.parent or self.root

    def handle_endtag(self, tag: str) -> None:
        cursor = self.current
        while cursor is not self.root:
            if cursor.tag == tag:
                self.current = cursor.parent or self.root
                self.depth = max(0, self.depth - 1)
                return
            cursor = cursor.parent or self.root

    def handle_data(self, data: str) -> None:
        self.current.chunks.append(data)


def _tree(html: str) -> _Node:
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    return parser.root


def _clean(value: str) -> str:
    return " ".join(value.replace("\u200f", "").replace("\u200e", "").split())


def _bounded(value: str | None, maximum: int, field: str) -> str | None:
    if value is not None and len(value) > maximum:
        raise SourceParseError(f"{field} exceeds {maximum} characters")
    return value


def _safe_media_url(value: str | None, base_url: str = "https://elcinema.com") -> str | None:
    if not value:
        return None
    if len(value) > 4_096:
        return None
    resolved = urljoin(base_url, value)
    parsed = urlsplit(resolved)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
        or not (hostname == "elcinema.com" or hostname.endswith(".elcinema.com"))
    ):
        return None
    return resolved


def _first(root: _Node, *, tag: str | None = None, class_name: str | None = None) -> _Node | None:
    for node in root.descendants(tag):
        if class_name is None or class_name in node.classes:
            return node
    return None


def discover_channels(html: str, base_url: str = "https://elcinema.com") -> tuple[Channel, ...]:
    """Discover current channel pages from the English guide index."""
    root = _tree(html)
    found: dict[str, Channel] = {}
    for anchor in root.descendants("a"):
        href = anchor.attrs.get("href", "")
        if not CHANNEL_PATH_RE.fullmatch(href):
            continue
        name = _clean(anchor.attrs.get("title", ""))
        if len(name) > 300:
            raise SourceParseError("channel ID exceeds 300 characters")
        image = next(anchor.descendants("img"), None)
        if not name or image is None:
            continue
        icon = image.attrs.get("data-src") or image.attrs.get("src") or None
        found.setdefault(
            href,
            Channel(
                id=name,
                name=name,
                url=urljoin(base_url, href),
                icon_url=_safe_media_url(icon, base_url),
            ),
        )
        if len(found) > MAX_DISCOVERED_CHANNELS:
            raise SourceParseError("elCinema index advertised too many channels")
    if not found:
        raise SourceParseError("elCinema index contained no recognizable channels")
    return tuple(found.values())


def parse_arabic_datetime(date_text: str, time_text: str, year: int) -> datetime:
    """Parse an Arabic elCinema wall-clock time for Dubai IPTV clients."""
    date_match = re.search(r"(\d{1,2})\s+([أ-ي]+)", _clean(date_text))
    time_match = TIME_RE.search(_clean(time_text))
    if not date_match or not time_match:
        raise SourceParseError(f"unrecognized date/time: {date_text!r} {time_text!r}")
    day = int(date_match.group(1))
    month_name = date_match.group(2)
    month = ARABIC_MONTHS.get(month_name)
    if month is None:
        raise SourceParseError(f"unrecognized Arabic month: {month_name}")
    hour, minute = int(time_match.group(1)), int(time_match.group(2))
    meridiem = time_match.group(3)
    if hour < 1 or hour > 12 or minute > 59:
        raise SourceParseError(f"invalid time: {time_text!r}")
    if meridiem.startswith("مساء") and hour != 12:
        hour += 12
    elif meridiem.startswith("صباح") and hour == 12:
        hour = 0
    try:
        return datetime(year, month, day, hour, minute, tzinfo=DUBAI)
    except ValueError as error:
        raise SourceParseError(str(error)) from error


def _date_groups(tvgrid: _Node) -> list[tuple[str, list[_Node]]]:
    groups: list[tuple[str, list[_Node]]] = []
    current_cards: list[_Node] | None = None
    for node in tvgrid.descendants():
        if "dates" in node.classes:
            current_cards = []
            groups.append((node.text(), current_cards))
        elif current_cards is not None and any(name.startswith("boxed-category-") for name in node.classes):
            if not any(
                any(name.startswith("boxed-category-") for name in ancestor.classes)
                for ancestor in _ancestors_until(node, tvgrid)
            ):
                current_cards.append(node)
    return groups


def _ancestors_until(node: _Node, stop: _Node) -> Iterable[_Node]:
    cursor = node.parent
    while cursor is not None and cursor is not stop:
        yield cursor
        cursor = cursor.parent


def _card_fields(card: _Node) -> tuple[str, str, int | None, str | None, str | None]:
    text = card.text()
    time_match = TIME_RE.search(text)
    duration_match = DURATION_RE.search(text)
    if not time_match:
        raise SourceParseError("programme card is missing a valid time")
    work_link = next(
        (
            node
            for node in card.descendants("a")
            if re.fullmatch(r"/work/\d+/", node.attrs.get("href", "")) and node.text()
        ),
        None,
    )
    if work_link is not None:
        title = work_link.text()
        title_list = work_link.parent.parent if work_link.parent and work_link.parent.parent else None
        category = None
        if title_list is not None:
            lines = [node.text() for node in title_list.children if node.tag == "li"]
            if len(lines) > 1:
                category = re.sub(r"\s*\(\d{4}\)\s*$", "", lines[1]).strip() or None
        description = None
        wide = next((node for node in card.descendants() if {"small-12", "large-6"} <= node.classes), None)
        if wide is not None:
            items = list(wide.descendants("li"))
            if items:
                candidate = items[-1].text()
                if candidate and "التقييم" not in candidate:
                    description = candidate.replace("...اقرأ المزيد", "").strip() or None
    else:
        lists = [node for node in card.descendants("ul") if "no-margin" in node.classes]
        if not lists:
            raise SourceParseError("programme card is missing a title")
        lines = [node.text() for node in lists[0].children if node.tag == "li"]
        if not lines:
            raise SourceParseError("programme card is missing a title")
        title, category, description = lines[0], None, None
    if not title:
        raise SourceParseError("programme title is empty")
    _bounded(title, 500, "programme title")
    _bounded(description, 5_000, "programme description")
    _bounded(category, 200, "programme category")
    duration = int(duration_match.group(1)) if duration_match else None
    return title, time_match.group(0), duration, description, category


def parse_channel_page(
    html: str,
    channel: Channel,
    year: int,
    reference_date: date | None = None,
) -> tuple[Channel, tuple[Programme, ...]]:
    """Parse one Arabic channel page into channel metadata and programmes."""
    root = _tree(html)
    tvgrid = _first(root, class_name="tvgrid")
    heading = _first(root, tag="h1")
    if tvgrid is None or heading is None or not heading.text():
        raise SourceParseError(f"channel page for {channel.id!r} has no guide")
    icon_meta = next(
        (node for node in root.descendants("meta") if node.attrs.get("property") == "og:image"),
        None,
    )
    parsed_channel = replace(
        channel,
        icon_url=(
            _safe_media_url(icon_meta.attrs.get("content"), channel.url)
            if icon_meta
            else channel.icon_url
        ),
    )
    programmes: list[Programme] = []
    skipped_cards = 0
    pending: tuple[str, datetime, str | None, str | None] | None = None
    previous_month: int | None = None
    current_year = year
    for date_text, cards in _date_groups(tvgrid):
        if len(programmes) + len(cards) > MAX_PROGRAMMES_PER_CHANNEL:
            raise SourceParseError(f"channel page for {channel.id!r} contains too many programmes")
        month_match = re.search(r"\d{1,2}\s+([أ-ي]+)", _clean(date_text))
        month = ARABIC_MONTHS.get(month_match.group(1)) if month_match else None
        if month is None:
            raise SourceParseError(f"invalid date section: {date_text!r}")
        day_match = re.search(r"(\d{1,2})\s+[أ-ي]+", _clean(date_text))
        day = int(day_match.group(1)) if day_match else 1
        if previous_month is None and reference_date is not None:
            candidates = []
            for candidate_year in range(reference_date.year - 1, reference_date.year + 2):
                try:
                    candidates.append(date(candidate_year, month, day))
                except ValueError:
                    continue
            if not candidates:
                raise SourceParseError(f"invalid date section: {date_text!r}")
            current_year = min(
                candidates, key=lambda candidate: abs((candidate - reference_date).days)
            ).year
        elif previous_month == 12 and month == 1:
            current_year += 1
        previous_month = month
        previous_start: datetime | None = None
        for card in cards:
            title, time_text, duration, description, category = _card_fields(card)
            start = parse_arabic_datetime(date_text, time_text, current_year)
            if previous_start is not None and start < previous_start:
                start += timedelta(days=1)
            if pending is not None:
                pending_title, pending_start, pending_description, pending_category = pending
                inferred_duration = start - pending_start
                if timedelta(0) < inferred_duration <= timedelta(hours=6):
                    programmes.append(
                        Programme(
                            channel.id,
                            pending_title,
                            pending_start,
                            start,
                            pending_description,
                            pending_category,
                        )
                    )
                else:
                    skipped_cards += 1
                pending = None
            if duration is None:
                pending = (title, start, description, category)
            else:
                stop = start + timedelta(minutes=duration)
                programmes.append(Programme(channel.id, title, start, stop, description, category))
            previous_start = start
    if pending is not None:
        skipped_cards += 1
    if not programmes:
        raise SourceParseError(f"channel page for {channel.id!r} contained no programmes")
    card_count = len(programmes) + skipped_cards
    if skipped_cards:
        if skipped_cards > max(3, card_count // 10):
            raise SourceParseError(
                f"channel page for {channel.id!r} skipped {skipped_cards}/{card_count} programmes"
            )
        warnings.warn(
            f"{channel.id}: skipped {skipped_cards} programme(s) with invalid durations",
            stacklevel=2,
        )
    return parsed_channel, tuple(programmes)


def build_xmltv(channels: Iterable[Channel], programmes: Iterable[Programme]) -> str:
    """Build deterministic, validated XMLTV text."""
    channel_list = tuple(channels)
    programme_list = tuple(programmes)
    for channel in channel_list:
        if any(len(value) > 300 for value in (channel.id, channel.name, *channel.aliases, *channel.alias_ids)):
            raise ValueError("channel ID or name is too long")
        if len(channel.url) > 4_096 or (channel.icon_url and len(channel.icon_url) > 4_096):
            raise ValueError("channel URL is too long")
    channel_ids = {channel.id for channel in channel_list}
    emitted_ids = [value for channel in channel_list for value in (channel.id, *channel.alias_ids)]
    if len(channel_list) > MAX_DISCOVERED_CHANNELS:
        raise ValueError("too many channels")
    if len(programme_list) > 100_000:
        raise ValueError("too many programmes")
    if len(channel_ids) != len(channel_list):
        raise ValueError("duplicate channel IDs are not allowed")
    if len(set(emitted_ids)) != len(emitted_ids):
        raise ValueError("duplicate canonical or alias channel IDs are not allowed")
    schedules = {channel_id: [] for channel_id in channel_ids}
    for programme in programme_list:
        if programme.channel_id not in schedules:
            raise ValueError(f"programme references unknown channel {programme.channel_id!r}")
        schedules[programme.channel_id].append(programme)
    reconciled: list[Programme] = []
    for channel_id in channel_ids:
        schedule = sorted(
            schedules[channel_id],
            key=lambda item: (item.start, item.stop, item.title),
        )
        for index, programme in enumerate(schedule):
            if index + 1 < len(schedule) and programme.stop > schedule[index + 1].start:
                next_start = schedule[index + 1].start
                if next_start <= programme.start:
                    raise ValueError(f"unresolvable programme overlap on {channel_id!r}")
                programme = replace(programme, stop=next_start)
            reconciled.append(programme)
    programme_list = tuple(reconciled)
    root = ET.Element(
        "tv",
        {
            "generator-info-name": "EPG-Guide elCinema updater",
            "generator-info-url": "https://github.com/MuazT/EPG-Guide",
            "source-info-url": "https://elcinema.com/ar/tvguide/",
        },
    )
    emitted_channels = sorted(
        ((emitted_id, channel) for channel in channel_list for emitted_id in (channel.id, *channel.alias_ids)),
        key=lambda item: item[0].casefold(),
    )
    for emitted_id, channel in emitted_channels:
        element = ET.SubElement(root, "channel", {"id": emitted_id})
        for display_name in (channel.name, *channel.aliases):
            ET.SubElement(element, "display-name", {"lang": _text_language(display_name)}).text = display_name
        if channel.icon_url:
            ET.SubElement(element, "icon", {"src": channel.icon_url})
        ET.SubElement(element, "url").text = channel.url
    channel_lookup = {channel.id: channel for channel in channel_list}
    seen: set[tuple[str, datetime, datetime, str]] = set()
    for programme in sorted(programme_list, key=lambda item: (item.start, item.channel_id, item.title)):
        if programme.channel_id not in channel_ids:
            raise ValueError(f"programme references unknown channel {programme.channel_id!r}")
        if programme.start.tzinfo is None or programme.stop.tzinfo is None:
            raise ValueError("programme datetimes must include a timezone")
        if programme.stop <= programme.start:
            raise ValueError("programme stop must be after start")
        key = (programme.channel_id, programme.start, programme.stop, programme.title.casefold())
        if key in seen:
            continue
        seen.add(key)
        for emitted_id in sorted(
            (programme.channel_id, *channel_lookup[programme.channel_id].alias_ids),
            key=str.casefold,
        ):
            element = ET.SubElement(
                root,
                "programme",
                {
                    "channel": emitted_id,
                    "start": programme.start.strftime("%Y%m%d%H%M%S %z"),
                    "stop": programme.stop.strftime("%Y%m%d%H%M%S %z"),
                },
            )
            ET.SubElement(element, "title", {"lang": _text_language(programme.title)}).text = programme.title
            if programme.description:
                ET.SubElement(element, "desc", {"lang": _text_language(programme.description)}).text = programme.description
            if programme.category:
                ET.SubElement(element, "category", {"lang": _text_language(programme.category)}).text = programme.category
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _text_language(value: str) -> str:
    return "ar" if re.search(r"[\u0600-\u06ff]", value) else "en"
