from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import update_epg
from epg_generator import Channel, Programme, SourceParseError, build_xmltv


def sample_guide():
    now = datetime.now(update_epg.DUBAI)
    channel = Channel("Test TV", "قناة الاختبار", "https://elcinema.com/tvguide/1/")
    programmes = (
        Programme(channel.id, "برنامج", now, now + timedelta(hours=1)),
        Programme(
            channel.id,
            "برنامج الغد",
            now + timedelta(days=3),
            now + timedelta(days=3, hours=1),
        ),
    )
    return (channel,), programmes


def test_apply_aliases_preserves_metadata_and_known_playlist_id():
    source = (
        Channel("AlSharqiya", "AlSharqiya", "https://example.test/1", "logo.png"),
        Channel("MBC", "MBC", "https://example.test/2"),
    )

    result = update_epg.apply_aliases(source)

    assert [channel.id for channel in result] == ["Al sharqya", "MBC"]
    assert result[0].icon_url == "logo.png"


def test_apply_subscription_aliases_adds_names_and_only_safe_unique_ids():
    channels = (
        Channel("Emirates", "Emirates", "https://example.test/1"),
        Channel("DMC", "DMC", "https://example.test/2"),
    )
    aliases = {
        "Emirates": {
            "names": ["AR: Al Emarat TV HD"],
            "ids": ["AlEmarat.ae", "TS"],
        },
        "DMC": {"names": ["AR: DMC TV 4K"], "ids": ["TS"]},
    }

    result = update_epg.apply_subscription_aliases(channels, aliases)

    assert result[0].aliases == ("AR: Al Emarat TV HD",)
    assert result[0].alias_ids == ("AlEmarat.ae",)
    assert result[1].aliases == ("AR: DMC TV 4K",)
    assert result[1].alias_ids == ()


def test_apply_subscription_aliases_excludes_ambiguous_names():
    channels = (
        Channel("First", "First", "https://example.test/1"),
        Channel("Second", "Second", "https://example.test/2"),
    )
    aliases = {
        "First": {"names": ["Same playlist name", "First HD"], "ids": []},
        "Second": {"names": ["Same playlist name", "Second HD"], "ids": []},
    }

    result = update_epg.apply_subscription_aliases(channels, aliases)

    assert result[0].aliases == ("First HD",)
    assert result[1].aliases == ("Second HD",)


@pytest.mark.parametrize(
    "content",
    [
        '[]',
        '{"Emirates": "not an object"}',
        '{"Emirates": {"names": "not a list", "ids": []}}',
        '{"Emirates": {"names": [42], "ids": []}}',
        '{"Emirates": {"names": [], "ids": [], "secret": []}}',
    ],
)
def test_load_subscription_aliases_rejects_invalid_schema(tmp_path: Path, content):
    path = tmp_path / "aliases.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(SourceParseError, match="channel aliases"):
        update_epg.load_subscription_aliases(path)


def test_committed_playlist_aliases_are_sanitized_unique_and_representative():
    aliases = update_epg.load_subscription_aliases()
    expected = {
        "Al sharqya": ({"AR| AL SHARQIYA TV FHD"}, set()),
        "Emirates": ({"AR| AL EMARAT TV HD"}, {"AlEmarat.ae"}),
        "MBC": ({"AR| MBC 1"}, {"MBC1En.ae"}),
        "National Geographic": (
            {"AR| ABU DHABI NATIONAL GEO HD", "AR| AD NAT GEO HD"},
            set(),
        ),
        "Utv": ({"AR| UTV IRAQ"}, set()),
    }

    for channel_id, (names, ids) in expected.items():
        assert names.issubset(set(aliases[channel_id]["names"]))
        assert ids.issubset(set(aliases[channel_id]["ids"]))

    all_names = [name for values in aliases.values() for name in values["names"]]
    all_ids = [alias_id for values in aliases.values() for alias_id in values["ids"]]
    assert len({name.casefold() for name in all_names}) == len(all_names)
    assert len(set(all_ids)) == len(all_ids)
    assert "TS" not in all_ids
    assert all(
        "://" not in value
        and "username=" not in value.casefold()
        and "password=" not in value.casefold()
        and "+6h" not in value.casefold()
        for value in (*all_names, *all_ids)
    )
    assert not {"BE| MTV HD", "BE| MTV HEVC"} & set(aliases["MTV"]["names"])


def test_committed_aliases_cover_verified_playlist_broadcast_names():
    aliases = update_epg.load_subscription_aliases()
    expected = {
        "LBC": {"AR| LBC SAT HD"},
        "MBC": {"AR| GOBX MBC 1 FHD", "AR| GOBX MBC 1 HD"},
        "MBC 2": {"AR| GOBX MBC 2 FHD", "AR| GOBX MBC 2 HD"},
        "MBC 3": {"AR| GOBX MBC 3 FHD", "AR| GOBX MBC 3 HD"},
        "MBC 4": {"AR| GOBX MBC 4 FHD", "AR| GOBX MBC 4 HD"},
        "MBC 5": {"AR| GOBX MBC 5 FHD", "AR| GOBX MBC 5 HD"},
        "MBC Action": {
            "AR| GOBX MBC ACTION FHD",
            "AR| GOBX MBC ACTION HD",
        },
        "MBC Bollywood": {"AR| GOBX MBC BOLLYWOOD FHD"},
        "MBC Drama": {
            "AR| GOBX MBC DRAMA FHD",
            "AR| GOBX MBC DRAMA HD",
            "AR| MBC DRAMA",
            "AR| MBC DRAMA SD",
        },
        "MBC Drama +": {
            "AR| GOBX MBC DRAMA+ FHD",
            "AR| GOBX MBC DRAMA+ HD",
            "AR| MBC DRAMA+ FHD",
        },
        "MBC Egypt": {
            "AR| GOBX MBC MASR 1 FHD",
            "AR| GOBX MBC MASR 1 HD",
            "AR| MBC MASR",
        },
        "MBC Iraq": {
            "AR| GOBX MBC IRAQ FHD",
            "AR| GOBX MBC IRAQ HD",
        },
        "MBC MASR 2": {
            "AR| GOBX MBC MASR 2 FHD",
            "AR| GOBX MBC MASR 2 HD",
        },
        "MBC MAX": {"AR| GOBX MBC MAX HD"},
        "OSN TV Comedy": {"AR| OSN COMDEY"},
        "OSN TV Crime": {"AR| OSN CRIME"},
        "OSN TV Kids": {"AR| OSN KIDS"},
        "OSN TV Movies Action": {"AR| OSN MOVIES ACTION"},
        "OSN TV Movies Family": {"AR| OSN MOVIES FAMILY"},
        "OSN TV Movies Hollywood": {"AR| OSN MOVIES HOLLOWAY"},
        "OSN TV Movies Premiere": {"AR| OSN MOVIES PREMIERE"},
        "OSN TV Now": {"AR| OSN NOW"},
        "OSN TV One": {"AR| OSN ONE"},
        "OSN TV Showcase": {"AR| OSN SHOWCASE"},
        "OSN TV Yahala Bil Arabi": {"AR| OSN YAHALA BIL ARABI"},
        "OSN Ya Hala": {"AR| OSN YAHALA"},
        "Osn Ya Hala Aflam": {"AR| OSN YAHALA AFLAM"},
        "Sharjah TV": {"AR| Al SHARJAH HD"},
        "Watania 2": {"AR| TUNISIA NAT 2"},
    }

    for channel_id, names in expected.items():
        assert names.issubset(set(aliases[channel_id]["names"])), channel_id


def test_collect_guide_integrates_discovery_and_channel_parser(monkeypatch, fixture_html):
    def fake_fetch(url, limiter, retries=3):
        if url == update_epg.INDEX_URL:
            return fixture_html("tvguide_index_en.html")
        assert url in {
            f"{update_epg.BASE_URL}/ar/tvguide/1128/",
            f"{update_epg.BASE_URL}/ar/tvguide/1136/",
        }
        return fixture_html("channel_1128_ar.html").replace(
            "Miss Sloane &amp; Friends", "الآنسة سلون وأصدقاؤها"
        )

    monkeypatch.setattr(update_epg, "fetch_text", fake_fetch)
    monkeypatch.setattr(
        update_epg, "configured_source_timezone", lambda: ZoneInfo("Asia/Dubai")
    )
    monkeypatch.setattr(
        update_epg, "collect_iptv_org_guide", lambda *args, **kwargs: ((), ())
    )

    channels, programmes = update_epg.collect_guide(delay=0, workers=1)

    assert [(channel.id, channel.name) for channel in channels] == [
        ("MBC 2", "MBC 2"),
        ("Abu Dhabi TV", "Abu Dhabi TV"),
    ]
    assert len(programmes) == 4
    assert {programme.channel_id for programme in programmes} == {"MBC 2", "Abu Dhabi TV"}
    assert (
        programmes[0].title,
        programmes[0].description,
        programmes[0].category,
    ) == (
        "الآنسة سلون وأصدقاؤها",
        "سياسية ناجحة تواجه خصمًا قويًا.",
        "فيلم",
    )


def test_collect_guide_applies_source_and_schedule_clock_shifts(monkeypatch, fixture_html):
    index_html = fixture_html("tvguide_index_en.html")

    def fake_fetch(url, limiter, retries=3):
        if url == update_epg.INDEX_URL:
            return index_html
        return fixture_html("channel_1128_ar.html")

    monkeypatch.setattr(update_epg, "fetch_text", fake_fetch)
    monkeypatch.setattr(
        update_epg, "configured_source_timezone", lambda: ZoneInfo("Asia/Dubai")
    )
    monkeypatch.setattr(
        update_epg,
        "source_wall_clock_shift",
        lambda source_timezone: timedelta(hours=8),
    )
    monkeypatch.setattr(
        update_epg, "collect_iptv_org_guide", lambda *args, **kwargs: ((), ())
    )

    _, programmes = update_epg.collect_guide(delay=0, workers=1)

    assert programmes[0].start.hour == 3


def test_collect_guide_merges_supplemental_iptv_org_channels(monkeypatch, fixture_html):
    index_html = fixture_html("tvguide_index_en.html")

    def fake_fetch(url, limiter, retries=3):
        if url == update_epg.INDEX_URL:
            return index_html
        return fixture_html("channel_1128_ar.html")

    supplemental = Channel(
        "ARTAflam1.sa", "ART Aflam 1", "https://www.artonline.tv/"
    )
    start = datetime(2026, 8, 14, 0, 0, tzinfo=update_epg.DUBAI)
    monkeypatch.setattr(update_epg, "fetch_text", fake_fetch)
    monkeypatch.setattr(
        update_epg, "configured_source_timezone", lambda: ZoneInfo("Asia/Dubai")
    )
    monkeypatch.setattr(
        update_epg,
        "collect_iptv_org_guide",
        lambda *args, **kwargs: (
            (supplemental,),
            (Programme(supplemental.id, "فيلم عربي", start, start + timedelta(hours=2)),),
        ),
    )

    channels, programmes = update_epg.collect_guide(delay=0, workers=1)

    assert supplemental in channels
    assert any(programme.channel_id == supplemental.id for programme in programmes)


def test_source_wall_clock_shift_is_dst_aware():
    new_york = ZoneInfo("America/New_York")

    assert update_epg.source_wall_clock_shift(
        new_york,
        now=datetime(2026, 8, 13, 20, 0, tzinfo=update_epg.DUBAI),
    ) == timedelta(hours=8)
    assert update_epg.source_wall_clock_shift(
        new_york,
        now=datetime(2026, 1, 13, 20, 0, tzinfo=update_epg.DUBAI),
    ) == timedelta(hours=9)


def test_source_wall_clock_shift_supports_fractional_timezone_offsets():
    assert update_epg.source_wall_clock_shift(
        ZoneInfo("Asia/Kathmandu"),
        now=datetime(2026, 8, 13, 20, 0, tzinfo=update_epg.DUBAI),
    ) == -timedelta(hours=1, minutes=45)


def test_schedule_wall_clock_shift_corrects_all_channels():
    base_shift = timedelta(hours=8)

    assert update_epg.schedule_wall_clock_shift(base_shift) == timedelta(hours=7)
    assert update_epg.schedule_wall_clock_shift(timedelta(0)) == timedelta(hours=-1)


def test_configured_source_timezone_uses_valid_environment(monkeypatch):
    monkeypatch.setenv("ELCINEMA_SOURCE_TIMEZONE", "America/New_York")

    assert update_epg.configured_source_timezone() == ZoneInfo("America/New_York")


def test_configured_source_timezone_rejects_unknown_environment(monkeypatch):
    monkeypatch.setenv("ELCINEMA_SOURCE_TIMEZONE", "Not/A_Timezone")

    with pytest.raises(SourceParseError, match="unknown"):
        update_epg.configured_source_timezone()


def test_validate_guide_accepts_complete_xml_and_reports_summary():
    channels, programmes = sample_guide()
    xml = build_xmltv(channels, programmes)

    summary = update_epg.validate_guide(
        xml, min_channels=1, min_programmes=2, min_future_days=2
    )

    assert summary[:2] == (1, 2)


def test_validate_guide_rejects_suspiciously_small_output():
    channels, programmes = sample_guide()

    with pytest.raises(SourceParseError, match="channels"):
        update_epg.validate_guide(build_xmltv(channels, programmes))


def test_validate_guide_does_not_count_alias_programme_duplicates():
    now = datetime.now(update_epg.DUBAI)
    channel = Channel(
        "Test TV",
        "Test TV",
        "https://elcinema.com/tvguide/1/",
        alias_ids=("TestTV.example",),
    )
    programme = Programme(channel.id, "برنامج", now, now + timedelta(days=3))
    xml = build_xmltv((channel,), (programme,))

    with pytest.raises(SourceParseError, match="programmes"):
        update_epg.validate_guide(
            xml,
            canonical_channel_ids={channel.id},
            min_channels=1,
            min_programmes=2,
            min_future_days=2,
        )


def test_validate_guide_rejects_future_only_output():
    channel = Channel("Test TV", "قناة الاختبار", "https://elcinema.com/tvguide/1/")
    start = datetime.now(update_epg.DUBAI) + timedelta(days=2)
    xml = build_xmltv(
        (channel,),
        (Programme(channel.id, "بعيد", start, start + timedelta(days=2)),),
    )

    with pytest.raises(SourceParseError, match="current broadcast"):
        update_epg.validate_guide(
            xml, min_channels=1, min_programmes=1, min_future_days=2
        )


def test_validate_guide_rejects_old_and_future_data_with_current_gap():
    channel = Channel("Test TV", "قناة الاختبار", "https://elcinema.com/tvguide/1/")
    now = datetime.now(update_epg.DUBAI)
    xml = build_xmltv(
        (channel,),
        (
            Programme(channel.id, "قديم", now - timedelta(days=10), now - timedelta(days=9)),
            Programme(channel.id, "بعيد", now + timedelta(days=2), now + timedelta(days=4)),
        ),
    )

    with pytest.raises(SourceParseError, match="current broadcast"):
        update_epg.validate_guide(
            xml, min_channels=1, min_programmes=2, min_future_days=2
        )


def test_validate_guide_rejects_small_gap_around_now():
    channel = Channel("Test TV", "قناة الاختبار", "https://elcinema.com/tvguide/1/")
    now = datetime.now(update_epg.DUBAI)
    xml = build_xmltv(
        (channel,),
        (
            Programme(channel.id, "انتهى", now - timedelta(hours=2), now - timedelta(hours=1)),
            Programme(channel.id, "لاحقًا", now + timedelta(hours=1), now + timedelta(days=3)),
        ),
    )

    with pytest.raises(SourceParseError, match="current broadcast"):
        update_epg.validate_guide(
            xml, min_channels=1, min_programmes=2, min_future_days=2
        )


def test_validate_guide_requires_current_coverage_on_half_the_channels():
    now = datetime.now(update_epg.DUBAI)
    channels = tuple(
        Channel(
            f"Test {index}",
            f"قناة {index}",
            f"https://elcinema.com/tvguide/{index}/",
        )
        for index in range(8)
    )
    programmes = tuple(
        programme
        for index, channel in enumerate(channels)
        for programme in (
            Programme(
                channel.id,
                "الحالي" if index == 0 else "السابق",
                now - timedelta(hours=1 if index == 0 else 2),
                now + timedelta(hours=1) if index == 0 else now - timedelta(hours=1),
            ),
            Programme(
                channel.id,
                "لاحقًا",
                now + timedelta(hours=1),
                now + timedelta(days=3),
            ),
        )
    )

    with pytest.raises(SourceParseError, match=r"\(1/4 channels\)"):
        update_epg.validate_guide(
            build_xmltv(channels, programmes),
            min_channels=8,
            min_programmes=16,
            min_future_days=2,
        )


def test_atomic_write_replaces_file(tmp_path: Path):
    output = tmp_path / "guide.xml"
    output.write_text("old", encoding="utf-8")

    update_epg.atomic_write(output, "new\n")

    assert output.read_text(encoding="utf-8") == "new\n"
    assert list(tmp_path.iterdir()) == [output]


def test_fetch_text_rejects_non_html(monkeypatch):
    class Response:
        status = 200
        headers = SimpleNamespace(
            get_content_type=lambda: "application/json",
            get=lambda name: None,
        )

        def geturl(self):
            return "https://elcinema.com/test"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    response = Response()
    monkeypatch.setattr(update_epg, "urlopen", lambda *args, **kwargs: response)
    limiter = SimpleNamespace(wait=lambda: None)

    with pytest.raises(SourceParseError, match="unexpected"):
        update_epg.fetch_text("https://example.test", limiter)


def test_main_writes_validated_output(monkeypatch, tmp_path: Path):
    output = tmp_path / "guide.xml"
    monkeypatch.setattr(update_epg, "collect_guide", lambda **kwargs: sample_guide())
    monkeypatch.setattr(
        update_epg, "validate_guide", lambda xml, **kwargs: (1, 2, "start", "stop")
    )
    monkeypatch.setattr(update_epg.sys, "argv", ["update_epg.py", "--output", str(output)])

    assert update_epg.main() == 0
    assert output.read_text(encoding="utf-8").startswith("<?xml")


def test_main_preserves_output_on_source_failure(monkeypatch, tmp_path: Path):
    output = tmp_path / "guide.xml"
    output.write_text("last known good", encoding="utf-8")

    def fail(**kwargs):
        raise SourceParseError("source changed")

    monkeypatch.setattr(update_epg, "collect_guide", fail)
    monkeypatch.setattr(update_epg.sys, "argv", ["update_epg.py", "--output", str(output)])

    assert update_epg.main() == 1
    assert output.read_text(encoding="utf-8") == "last known good"
