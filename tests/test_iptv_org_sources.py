from datetime import date, datetime
from types import SimpleNamespace
from urllib.error import URLError
from zoneinfo import ZoneInfo

import pytest

import iptv_org_sources
from epg_generator import Channel, SourceParseError


DUBAI = ZoneInfo("Asia/Dubai")


class Response:
    def __init__(
        self,
        body=b"[]",
        *,
        url="https://www.artonline.tv/Home/Tvlist",
        content_type="application/json",
        content_length=None,
    ):
        self.body = body
        self.url = url
        self.status = 200
        self.headers = SimpleNamespace(
            get_content_type=lambda: content_type,
            get=lambda name: str(content_length)
            if name == "Content-Length" and content_length is not None
            else None,
        )

    def geturl(self):
        return self.url

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_extra_channels_are_missing_playlist_ids_with_aliases():
    channels = iptv_org_sources.EXTRA_CHANNELS

    assert {channel.id for channel in channels} == {
        "ARTAflam1.sa",
        "ARTAflam2.sa",
        "ARTCinema.sa",
        "Assadissa.ma",
    }
    assert "AR| ART AFLAM 1 FHD" in channels[0].aliases
    assert "AR| ASSADISSA HEVC" in channels[-1].aliases


def test_parse_art_schedule_preserves_arabic_and_clamps_overlap(fixture_html):
    channel = Channel("ARTAflam1.sa", "ART Aflam 1", "https://www.artonline.tv/")

    programmes = iptv_org_sources.parse_art_schedule(
        fixture_html("art_schedule_ar.json"), channel
    )

    assert [programme.title for programme in programmes] == [
        "الفيلم الأول",
        "الفيلم الثاني",
    ]
    assert programmes[0].description == "وصف عربي"
    assert programmes[0].start == datetime(2026, 8, 14, 21, 0, tzinfo=DUBAI)
    assert programmes[0].stop == programmes[1].start
    assert programmes[1].stop == datetime(2026, 8, 15, 1, 0, tzinfo=DUBAI)


def test_parse_art_schedule_rejects_non_list_json():
    channel = Channel("ARTAflam1.sa", "ART Aflam 1", "https://www.artonline.tv/")

    with pytest.raises(SourceParseError, match="ART schedule"):
        iptv_org_sources.parse_art_schedule("{}", channel)


def test_fetch_source_text_accepts_bounded_approved_response(monkeypatch):
    monkeypatch.setattr(iptv_org_sources, "urlopen", lambda *args, **kwargs: Response())
    limiter = SimpleNamespace(wait=lambda: None)

    assert (
        iptv_org_sources.fetch_source_text(
            "https://www.artonline.tv/Home/Tvlist", limiter
        )
        == "[]"
    )


def test_fetch_source_text_rejects_unapproved_url():
    limiter = SimpleNamespace(wait=lambda: None)

    with pytest.raises(SourceParseError, match="unapproved"):
        iptv_org_sources.fetch_source_text("https://example.test/guide", limiter)


def test_fetch_source_text_rejects_oversized_response(monkeypatch):
    response = Response(content_length=iptv_org_sources.MAX_RESPONSE_BYTES + 1)
    monkeypatch.setattr(iptv_org_sources, "urlopen", lambda *args, **kwargs: response)
    limiter = SimpleNamespace(wait=lambda: None)

    with pytest.raises(SourceParseError, match="size limit"):
        iptv_org_sources.fetch_source_text(
            "https://www.artonline.tv/Home/Tvlist", limiter
        )


def test_fetch_source_text_retries_network_errors(monkeypatch):
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(None)
        raise URLError("offline")

    monkeypatch.setattr(iptv_org_sources, "urlopen", fail)
    monkeypatch.setattr(iptv_org_sources.time, "sleep", lambda delay: None)
    limiter = SimpleNamespace(wait=lambda: None)

    with pytest.raises(SourceParseError, match="failed to fetch"):
        iptv_org_sources.fetch_source_text(
            "https://www.snrt.ma/ar/node/4073", limiter
        )
    assert len(attempts) == 3


def test_parse_snrt_schedule_preserves_arabic_and_uses_next_start(fixture_html):
    channel = Channel("Assadissa.ma", "Assadissa", "https://www.snrt.ma/")

    programmes = iptv_org_sources.parse_snrt_schedule(
        fixture_html("snrt_assadissa_ar.html"), channel
    )

    assert [programme.title for programme in programmes[:2]] == [
        "سورة الكهف",
        "جلسات قرآنية",
    ]
    assert programmes[0].description == "تلاوة قرآنية"
    assert programmes[0].category == "ديني"
    assert programmes[0].start == datetime(2026, 8, 14, 3, 0, tzinfo=DUBAI)
    assert programmes[0].stop == programmes[1].start
    assert programmes[1].stop == programmes[2].start


def test_collect_extra_guide_fetches_each_source(monkeypatch, fixture_html):
    calls = []

    def fake_fetch(url, limiter, *, data=None, retries=3):
        calls.append((url, data))
        if "artonline.tv" in url:
            return fixture_html("art_schedule_ar.json")
        return fixture_html("snrt_assadissa_ar.html")

    monkeypatch.setattr(iptv_org_sources, "fetch_source_text", fake_fetch)
    limiter = type("Limiter", (), {"wait": lambda self: None})()

    channels, programmes = iptv_org_sources.collect_iptv_org_guide(
        limiter, reference_date=date(2026, 8, 14), utc_date=date(2026, 8, 13)
    )

    assert len(channels) == 4
    assert {programme.channel_id for programme in programmes} == {
        "ARTAflam1.sa",
        "ARTAflam2.sa",
        "ARTCinema.sa",
        "Assadissa.ma",
    }
    assert len(calls) == 10


def test_collect_extra_guide_skips_only_failed_source(monkeypatch, fixture_html):
    def fake_fetch(url, limiter, *, data=None, retries=3):
        if url == iptv_org_sources.ART_ENDPOINTS["ARTAflam1.sa"]:
            raise SourceParseError("temporarily unavailable")
        if "artonline.tv" in url:
            return fixture_html("art_schedule_ar.json")
        return fixture_html("snrt_assadissa_ar.html")

    monkeypatch.setattr(iptv_org_sources, "fetch_source_text", fake_fetch)
    limiter = SimpleNamespace(wait=lambda: None)

    with pytest.warns(UserWarning, match="ARTAflam1.sa"):
        channels, programmes = iptv_org_sources.collect_iptv_org_guide(
            limiter, reference_date=date(2026, 8, 14), utc_date=date(2026, 8, 13)
        )

    assert "ARTAflam1.sa" not in {channel.id for channel in channels}
    assert {programme.channel_id for programme in programmes} == {
        "ARTAflam2.sa",
        "ARTCinema.sa",
        "Assadissa.ma",
    }


def test_collect_extra_guide_does_not_emit_out_of_window_channels(
    monkeypatch, fixture_html
):
    def fake_fetch(url, limiter, *, data=None, retries=3):
        if "artonline.tv" in url:
            return fixture_html("art_schedule_ar.json")
        return fixture_html("snrt_assadissa_ar.html")

    monkeypatch.setattr(iptv_org_sources, "fetch_source_text", fake_fetch)
    limiter = SimpleNamespace(wait=lambda: None)

    with pytest.warns(UserWarning, match="no programmes in update window"):
        channels, programmes = iptv_org_sources.collect_iptv_org_guide(
            limiter, reference_date=date(2027, 8, 14), utc_date=date(2027, 8, 13)
        )

    assert channels == ()
    assert programmes == ()
