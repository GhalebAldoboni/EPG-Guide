"""Fetch selected Arabic guides discovered through iptv-org/epg configs."""

from __future__ import annotations

import json
import re
import time
import warnings
from collections.abc import Collection
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from types import MappingProxyType
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from epg_generator import Channel, Programme, SourceParseError, _tree


DUBAI = ZoneInfo("Asia/Dubai")
RIYADH = ZoneInfo("Asia/Riyadh")
CASABLANCA = ZoneInfo("Africa/Casablanca")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PROGRAMMES_PER_CHANNEL = 1_000
ALLOWED_SOURCE_HOSTS = frozenset({"www.artonline.tv", "www.snrt.ma"})
ART_ENDPOINTS = MappingProxyType(
    {
        "ARTAflam1.sa": "https://www.artonline.tv/Home/Tvlist",
        "ARTAflam2.sa": "https://www.artonline.tv/Home/TvlistAflam2",
        "ARTCinema.sa": "https://www.artonline.tv/Home/TvlistCinema",
    }
)
EXTRA_CHANNELS = (
    Channel(
        "ARTAflam1.sa",
        "ART Aflam 1",
        "https://www.artonline.tv/",
        aliases=("AR| ART AFLAM 1", "AR| ART AFLAM 1 FHD"),
    ),
    Channel(
        "ARTAflam2.sa",
        "ART Aflam 2",
        "https://www.artonline.tv/",
        aliases=("AR| ART AFLAM 2 FHD",),
    ),
    Channel(
        "ARTCinema.sa",
        "ART Cinema",
        "https://www.artonline.tv/",
        aliases=("AR| ART CINEMA", "AR| ART CINEMA FHD"),
    ),
    Channel(
        "Assadissa.ma",
        "Assadissa",
        "https://www.snrt.ma/ar/node/4073",
        aliases=("AR| ASSADISSA HD", "AR| ASSADISSA HEVC"),
    ),
)
DATE_CLASS_RE = re.compile(r"^\d{8}$")
SNRT_TIME_RE = re.compile(r"^(\d{1,2})\s*[H:]\s*(\d{2})$")


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
            raise HTTPError(newurl, code, "redirect outside approved EPG sources", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


urlopen = build_opener(_SafeRedirectHandler()).open


def fetch_source_text(
    url: str, limiter, *, data: bytes | None = None, retries: int = 3
) -> str:
    """Fetch a bounded UTF-8 response from an approved upstream source."""
    if not _is_allowed_source_url(url):
        raise SourceParseError(f"unapproved EPG source URL: {url}")
    for attempt in range(retries):
        limiter.wait()
        headers = {
            "User-Agent": "EPG-Guide-Updater/2.0",
            "Accept": "application/json, text/html;q=0.9",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                if not _is_allowed_source_url(response.geturl()):
                    raise SourceParseError("EPG source redirected outside its allowlist")
                if response.status != 200:
                    raise SourceParseError(f"{url} returned HTTP {response.status}")
                content_type = response.headers.get_content_type()
                if content_type not in {
                    "application/json",
                    "text/json",
                    "text/html",
                    "application/xhtml+xml",
                }:
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
            if attempt == retries - 1:
                raise SourceParseError(f"failed to fetch {url}: {error}") from error
            time.sleep(2**attempt)
        except ValueError as error:
            raise SourceParseError(f"{url} returned invalid headers") from error
    raise AssertionError("unreachable")


def _duration(value: object) -> timedelta:
    if not isinstance(value, str):
        raise SourceParseError("ART programme has an invalid duration")
    parts = value.split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        raise SourceParseError("ART programme has an invalid duration")
    hours, minutes = (int(parts[0]), int(parts[1]))
    seconds = int(parts[2]) if len(parts) == 3 else 0
    duration = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    if duration <= timedelta(0) or duration > timedelta(hours=12):
        raise SourceParseError("ART programme has an invalid duration")
    return duration


def parse_art_schedule(content: str, channel: Channel) -> tuple[Programme, ...]:
    """Parse one Arabic ART schedule response and normalize it to Dubai."""
    try:
        items = json.loads(content)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise SourceParseError("ART schedule is not valid JSON") from error
    if not isinstance(items, list) or len(items) > MAX_PROGRAMMES_PER_CHANNEL:
        raise SourceParseError("ART schedule is not a bounded list")
    parsed: list[Programme] = []
    for item in items:
        if not isinstance(item, dict):
            raise SourceParseError("ART schedule contains an invalid programme")
        title = item.get("title")
        description = item.get("description")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise SourceParseError("ART schedule contains an invalid title")
        if description is not None and not isinstance(description, str):
            raise SourceParseError("ART schedule contains an invalid description")
        try:
            source_date = datetime.strptime(item["adddate"], "%m/%d/%Y %H:%M:%S").date()
            source_time = datetime.strptime(item["start_Time"], "%H:%M").time()
            source_start = datetime.combine(source_date, source_time, RIYADH)
        except (KeyError, TypeError, ValueError) as error:
            raise SourceParseError("ART schedule contains an invalid start time") from error
        start = source_start.astimezone(DUBAI)
        parsed.append(
            Programme(
                channel.id,
                title.strip(),
                start,
                start + _duration(item.get("duration")),
                description.strip() if description and description.strip() else None,
            )
        )
    ordered = sorted(parsed, key=lambda programme: programme.start)
    result = []
    for index, programme in enumerate(ordered):
        stop = programme.stop
        if index + 1 < len(ordered):
            stop = min(stop, ordered[index + 1].start)
        if stop > programme.start:
            result.append(
                Programme(
                    programme.channel_id,
                    programme.title,
                    programme.start,
                    stop,
                    programme.description,
                    programme.category,
                )
            )
    return tuple(result)


def _class_text(node, class_name: str) -> str | None:
    for descendant in node.descendants():
        if class_name in descendant.classes:
            text = descendant.text().strip()
            return text or None
    return None


def parse_snrt_schedule(content: str, channel: Channel) -> tuple[Programme, ...]:
    """Parse Assadissa's Arabic SNRT schedule and normalize it to Dubai."""
    root = _tree(content)
    parsed: list[tuple[datetime, str, str | None, str | None]] = []
    for node in root.descendants("div"):
        if "grille-line" not in node.classes:
            continue
        date_class = next((value for value in node.classes if DATE_CLASS_RE.fullmatch(value)), None)
        time_text = _class_text(node, "grille-time")
        title = _class_text(node, "program-title-sm")
        if not date_class or not time_text or not title:
            continue
        match = SNRT_TIME_RE.fullmatch(time_text)
        if not match:
            continue
        hour, minute = (int(match.group(1)), int(match.group(2)))
        if hour > 23 or minute > 59 or len(title) > 500:
            raise SourceParseError("SNRT schedule contains invalid programme data")
        source_start = datetime.combine(
            datetime.strptime(date_class, "%Y%m%d").date(),
            datetime_time(hour, minute),
            CASABLANCA,
        )
        parsed.append(
            (
                source_start.astimezone(DUBAI),
                title,
                _class_text(node, "program-description-sm"),
                _class_text(node, "genre-first"),
            )
        )
    ordered = sorted(set(parsed), key=lambda item: item[0])
    if len(ordered) > MAX_PROGRAMMES_PER_CHANNEL:
        raise SourceParseError("SNRT schedule contains too many programmes")
    programmes = []
    for index, (start, title, description, category) in enumerate(ordered):
        if index + 1 < len(ordered):
            stop = ordered[index + 1][0]
        else:
            source_date = start.astimezone(CASABLANCA).date()
            stop = datetime.combine(
                source_date + timedelta(days=1), datetime_time(), CASABLANCA
            ).astimezone(DUBAI)
        if stop > start:
            programmes.append(
                Programme(channel.id, title, start, stop, description, category)
            )
    if not programmes:
        raise SourceParseError("SNRT schedule did not contain programmes")
    return tuple(programmes)


def _deduplicate(programmes: Collection[Programme]) -> tuple[Programme, ...]:
    unique = {
        (programme.channel_id, programme.start, programme.stop, programme.title): programme
        for programme in programmes
    }
    return tuple(
        sorted(unique.values(), key=lambda programme: (programme.channel_id, programme.start))
    )


def collect_iptv_org_guide(
    limiter,
    *,
    reference_date: date | None = None,
    utc_date: date | None = None,
) -> tuple[tuple[Channel, ...], tuple[Programme, ...]]:
    """Fetch the working Arabic channels absent from the elCinema guide."""
    guide_date = reference_date or datetime.now(DUBAI).date()
    utc_today = utc_date or datetime.now(timezone.utc).date()
    final_offset = (guide_date + timedelta(days=1) - utc_today).days
    if not 1 <= final_offset <= 3:
        raise SourceParseError("ART schedule window has an implausible date offset")
    by_id = {channel.id: channel for channel in EXTRA_CHANNELS}
    window_start = datetime.combine(
        guide_date - timedelta(days=1), datetime_time(), DUBAI
    )
    window_stop = datetime.combine(
        guide_date + timedelta(days=3), datetime_time(), DUBAI
    )
    successful_channels: list[Channel] = []
    programmes: list[Programme] = []
    for channel_id, endpoint in ART_ENDPOINTS.items():
        channel = by_id[channel_id]
        channel_programmes: list[Programme] = []
        try:
            for offset in range(final_offset + 1):
                data = urlencode({"objId": str(offset)}).encode("ascii")
                content = fetch_source_text(endpoint, limiter, data=data)
                channel_programmes.extend(parse_art_schedule(content, channel))
        except SourceParseError as error:
            warnings.warn(f"{channel_id}: supplemental guide skipped: {error}", stacklevel=2)
            continue
        channel_programmes = [
            programme
            for programme in _deduplicate(channel_programmes)
            if programme.stop > window_start and programme.start < window_stop
        ]
        if not channel_programmes:
            warnings.warn(
                f"{channel_id}: supplemental guide skipped: no programmes in update window",
                stacklevel=2,
            )
            continue
        successful_channels.append(channel)
        programmes.extend(channel_programmes)
    assadissa = by_id["Assadissa.ma"]
    try:
        snrt_content = fetch_source_text(assadissa.url, limiter)
        assadissa_programmes = tuple(
            programme
            for programme in parse_snrt_schedule(snrt_content, assadissa)
            if programme.stop > window_start and programme.start < window_stop
        )
    except SourceParseError as error:
        warnings.warn(
            f"{assadissa.id}: supplemental guide skipped: {error}", stacklevel=2
        )
    else:
        if assadissa_programmes:
            successful_channels.append(assadissa)
            programmes.extend(assadissa_programmes)
        else:
            warnings.warn(
                f"{assadissa.id}: supplemental guide skipped: no programmes in update window",
                stacklevel=2,
            )
    return tuple(successful_channels), _deduplicate(programmes)
