from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
