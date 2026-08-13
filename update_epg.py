#!/usr/bin/env python3
"""Download current elCinema listings and replace ArabicEPG.xml safely."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Collection, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import MappingProxyType
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from epg_generator import Channel, Programme, SourceParseError, build_xmltv, discover_channels, parse_channel_page


BASE_URL = "https://elcinema.com"
INDEX_URL = f"{BASE_URL}/en/tvguide/"
SUBSCRIPTION_ALIASES_PATH = Path(__file__).with_name("channel_aliases.json")
USER_AGENT = "EPG-Guide-Updater/2.0 (+https://github.com/MuazT/EPG-Guide)"
DUBAI = ZoneInfo("Asia/Dubai")
CHANNEL_CLOCK_CORRECTIONS = MappingProxyType(
    {
        # The broadcaster runs one hour ahead of elCinema's published grid.
        "Sharjah TV": timedelta(hours=-1),
    }
)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
ALLOWED_SOURCE_HOSTS = frozenset({"elcinema.com", "www.elcinema.com"})
MAX_CHANNELS = 250
MAX_PROGRAMMES = 100_000
ALIASES = {
    "AlSharqiya": "Al sharqya",
    "beIN Series HD 2": "BeIn Series HD 2",
}


def _is_allowed_source_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in ALLOWED_SOURCE_HOSTS
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_allowed_source_url(newurl):
            raise HTTPError(newurl, code, "redirect outside elCinema", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


urlopen = build_opener(_SafeRedirectHandler()).open


class RateLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._next_request = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            pause = max(0.0, self._next_request - now)
            self._next_request = max(now, self._next_request) + self.delay + random.uniform(0, 0.2)
        if pause:
            time.sleep(pause)


def fetch_text(url: str, limiter: RateLimiter, retries: int = 3) -> str:
    """Fetch UTF-8 HTML with bounded retries and polite pacing."""
    for attempt in range(retries):
        limiter.wait()
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with urlopen(request, timeout=30) as response:
                if not _is_allowed_source_url(response.geturl()):
                    raise SourceParseError(f"{url} redirected outside elCinema")
                if response.status != 200:
                    raise SourceParseError(f"{url} returned HTTP {response.status}")
                content_type = response.headers.get_content_type()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise SourceParseError(f"{url} returned unexpected {content_type}")
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                    raise SourceParseError(f"{url} exceeded the response size limit")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise SourceParseError(f"{url} exceeded the response size limit")
                return body.decode("utf-8")
        except SourceParseError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeError) as error:
            if isinstance(error, HTTPError) and error.code < 500 and error.code != 429:
                raise SourceParseError(f"failed to fetch {url}: {error}") from error
            if attempt == retries - 1:
                raise SourceParseError(f"failed to fetch {url}: {error}") from error
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def apply_aliases(channels: tuple[Channel, ...]) -> tuple[Channel, ...]:
    return tuple(
        Channel(ALIASES.get(channel.id, channel.id), channel.name, channel.url, channel.icon_url)
        for channel in channels
    )


def configured_source_timezone() -> tzinfo:
    """Return the timezone elCinema uses for this updater's request region."""
    zone_id = os.environ.get("ELCINEMA_SOURCE_TIMEZONE")
    if not zone_id:
        local_zone = datetime.now().astimezone().tzinfo
        if local_zone is None:
            raise SourceParseError("local timezone is unavailable")
        return local_zone
    if not 1 <= len(zone_id) <= 64:
        raise SourceParseError("ELCINEMA_SOURCE_TIMEZONE is invalid")
    try:
        return ZoneInfo(zone_id)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise SourceParseError("ELCINEMA_SOURCE_TIMEZONE is unknown") from error


def source_wall_clock_shift(
    source_timezone: tzinfo, *, now: datetime | None = None
) -> timedelta:
    """Return the DST-aware shift from elCinema's requester clock to Dubai."""
    instant = (now or datetime.now(DUBAI)).astimezone(timezone.utc)
    dubai_offset = instant.astimezone(DUBAI).utcoffset()
    source_offset = instant.astimezone(source_timezone).utcoffset()
    if dubai_offset is None or source_offset is None:
        raise SourceParseError("could not calculate elCinema's clock offset")
    return dubai_offset - source_offset


def channel_wall_clock_shift(channel: Channel, base_shift: timedelta) -> timedelta:
    """Apply verified broadcaster-specific corrections to the source shift."""
    return base_shift + CHANNEL_CLOCK_CORRECTIONS.get(channel.id, timedelta(0))


def load_subscription_aliases(
    path: Path = SUBSCRIPTION_ALIASES_PATH,
) -> Mapping[str, Mapping[str, tuple[str, ...]]]:
    if not path.exists():
        return MappingProxyType({})
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or len(data) > MAX_CHANNELS:
        raise SourceParseError("channel aliases must be a bounded JSON object")
    validated: dict[str, Mapping[str, tuple[str, ...]]] = {}
    for channel_id, values in data.items():
        if not isinstance(channel_id, str) or not channel_id.strip() or len(channel_id) > 300:
            raise SourceParseError("channel aliases contain an invalid channel ID")
        if not isinstance(values, dict) or set(values) != {"names", "ids"}:
            raise SourceParseError(f"channel aliases for {channel_id!r} must contain names and ids")
        fields: dict[str, tuple[str, ...]] = {}
        for field in ("names", "ids"):
            entries = values[field]
            if not isinstance(entries, list) or len(entries) > 100:
                raise SourceParseError(f"channel aliases {field} for {channel_id!r} must be a bounded list")
            if any(
                not isinstance(entry, str) or not entry.strip() or len(entry) > 300
                for entry in entries
            ):
                raise SourceParseError(f"channel aliases {field} for {channel_id!r} contain invalid text")
            fields[field] = tuple(entries)
        validated[channel_id] = MappingProxyType(fields)
    return MappingProxyType(validated)


def apply_subscription_aliases(
    channels: tuple[Channel, ...], aliases: Mapping[str, Mapping[str, Collection[str]]]
) -> tuple[Channel, ...]:
    canonical_ids = {channel.id for channel in channels}
    id_candidates = [
        alias_id
        for channel_aliases in aliases.values()
        for alias_id in channel_aliases.get("ids", [])
        if alias_id and alias_id != "TS"
    ]
    name_candidates = [
        name.strip()
        for channel_aliases in aliases.values()
        for name in channel_aliases.get("names", [])
        if name.strip()
    ]
    id_counts = Counter(value.strip() for value in id_candidates)
    name_counts = Counter(name_candidates)
    unique_ids = {
        value for value, count in id_counts.items() if count == 1 and value not in canonical_ids
    }
    unique_names = {value for value, count in name_counts.items() if count == 1}
    result = []
    for channel in channels:
        values = aliases.get(channel.id, {})
        names = tuple(
            dict.fromkeys(
                value.strip()
                for value in values.get("names", [])
                if value.strip() in unique_names
            )
        )
        alias_ids = tuple(
            dict.fromkeys(value.strip() for value in values.get("ids", []) if value.strip() in unique_ids)
        )
        result.append(replace(channel, aliases=names, alias_ids=alias_ids))
    return tuple(result)


def collect_guide(
    *, delay: float = 0.75, workers: int = 2
) -> tuple[tuple[Channel, ...], tuple[Programme, ...]]:
    """Fetch and parse the index plus every Arabic channel page."""
    limiter = RateLimiter(delay)
    index_html = fetch_text(INDEX_URL, limiter)
    wall_clock_shift = source_wall_clock_shift(configured_source_timezone())
    discovered = apply_subscription_aliases(
        apply_aliases(discover_channels(index_html, BASE_URL)),
        load_subscription_aliases(),
    )
    if len(discovered) > MAX_CHANNELS:
        raise SourceParseError(f"source advertised too many channels: {len(discovered)}")
    scrape_date = datetime.now(DUBAI).date()

    def parse_one(channel: Channel) -> tuple[Channel, tuple[Programme, ...]]:
        numeric_id = channel.url.rstrip("/").rsplit("/", 1)[-1]
        html = fetch_text(f"{BASE_URL}/ar/tvguide/{numeric_id}/", limiter)
        return parse_channel_page(
            html,
            channel,
            scrape_date.year,
            reference_date=scrape_date,
            wall_clock_shift=channel_wall_clock_shift(channel, wall_clock_shift),
        )

    parsed: dict[str, tuple[Channel, tuple[Programme, ...]]] = {}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(parse_one, channel): channel for channel in discovered}
        for future in as_completed(futures):
            channel = futures[future]
            try:
                parsed[channel.id] = future.result()
            except Exception as error:
                failures.append(f"{channel.id}: {error}")
    if failures:
        preview = "; ".join(failures[:8])
        raise SourceParseError(f"{len(failures)} channel pages failed: {preview}")
    channels = tuple(parsed[channel.id][0] for channel in discovered)
    programmes = tuple(
        programme
        for channel in discovered
        for programme in parsed[channel.id][1]
    )
    if len(programmes) > MAX_PROGRAMMES:
        raise SourceParseError(f"source advertised too many programmes: {len(programmes)}")
    return channels, programmes


def validate_guide(
    xml: str,
    *,
    canonical_channel_ids: Collection[str] | None = None,
    min_channels: int = 80,
    min_programmes: int = 500,
    min_future_days: int = 2,
) -> tuple[int, int, str, str]:
    """Reject structurally valid but suspiciously incomplete output."""
    root = ET.fromstring(xml)
    channels = root.findall("channel")
    programmes = root.findall("programme")
    canonical_ids = (
        frozenset(canonical_channel_ids)
        if canonical_channel_ids is not None
        else frozenset(node.attrib["id"] for node in channels)
    )
    emitted_ids = {node.attrib["id"] for node in channels}
    if not canonical_ids.issubset(emitted_ids):
        raise SourceParseError("one or more canonical channels are missing from the XML")
    canonical_programmes = [
        node for node in programmes if node.attrib.get("channel") in canonical_ids
    ]
    if len(canonical_ids) < min_channels:
        raise SourceParseError(f"only {len(canonical_ids)} channels; expected at least {min_channels}")
    if len(canonical_programmes) < min_programmes:
        raise SourceParseError(
            f"only {len(canonical_programmes)} programmes; expected at least {min_programmes}"
        )
    starts = [
        datetime.strptime(node.attrib["start"], "%Y%m%d%H%M%S %z")
        for node in canonical_programmes
    ]
    stops = [
        datetime.strptime(node.attrib["stop"], "%Y%m%d%H%M%S %z")
        for node in canonical_programmes
    ]
    if any(stop <= start for start, stop in zip(starts, stops)):
        raise SourceParseError("one or more programmes have an invalid duration")
    now = datetime.now(DUBAI)
    earliest = min(starts).astimezone(DUBAI)
    latest = max(stops).astimezone(DUBAI)
    current_channels = {
        node.attrib["channel"]
        for node, start, stop in zip(canonical_programmes, starts, stops)
        if start.astimezone(DUBAI) <= now < stop.astimezone(DUBAI)
    }
    required_current_channels = max(1, (len(canonical_ids) + 1) // 2)
    if (
        earliest > now + timedelta(hours=6)
        or latest < now
        or len(current_channels) < required_current_channels
    ):
        raise SourceParseError(
            "guide does not cover the current broadcast window "
            f"({len(current_channels)}/{required_current_channels} channels)"
        )
    by_channel: dict[str, list[tuple[datetime, datetime]]] = {}
    for node, start, stop in zip(canonical_programmes, starts, stops):
        by_channel.setdefault(node.attrib["channel"], []).append((start, stop))
    for channel_id, schedule in by_channel.items():
        ordered = sorted(schedule)
        for (_, previous_stop), (next_start, _) in zip(ordered, ordered[1:]):
            if previous_stop > next_start:
                raise SourceParseError(f"overlapping programmes on {channel_id!r}")
    horizon = max(stops).astimezone(DUBAI) - now
    if horizon.total_seconds() < min_future_days * 86400:
        raise SourceParseError(f"guide horizon is only {horizon.total_seconds() / 86400:.1f} days")
    return len(channels), len(programmes), min(starts).isoformat(), max(stops).isoformat()


def atomic_write(path: Path, text: str) -> None:
    """Replace the output only after a complete temporary write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary:
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("ArabicEPG.xml"))
    parser.add_argument("--delay", type=float, default=0.75)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.delay < 0 or not 1 <= args.workers <= 4:
        parser.error("delay must be non-negative and workers must be between 1 and 4")
    try:
        channels, programmes = collect_guide(delay=args.delay, workers=args.workers)
        xml = build_xmltv(channels, programmes)
        summary = validate_guide(
            xml, canonical_channel_ids=frozenset(channel.id for channel in channels)
        )
        atomic_write(args.output, xml)
    except (SourceParseError, ValueError, ET.ParseError) as error:
        print(f"EPG update failed; existing output was preserved: {error}", file=sys.stderr)
        return 1
    print(
        f"Updated {args.output}: {summary[0]} channels, {summary[1]} programmes, "
        f"from {summary[2]} through {summary[3]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
