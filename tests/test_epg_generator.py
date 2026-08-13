from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import pytest

from epg_generator import (
    Channel,
    Programme,
    SourceParseError,
    build_xmltv,
    discover_channels,
    parse_arabic_datetime,
    parse_channel_page,
)


ELCINEMA = "https://elcinema.com"
DUBAI_OFFSET = timezone(timedelta(hours=4))


def test_discover_channels_uses_english_names_as_stable_ids_and_deduplicates(
    fixture_html,
):
    channels = discover_channels(
        fixture_html("tvguide_index_en.html"), base_url=ELCINEMA
    )

    assert isinstance(channels, tuple)
    assert channels == (
        Channel(
            id="MBC 2",
            name="MBC 2",
            url="https://elcinema.com/en/tvguide/1128/",
            icon_url="https://media0106.elcinema.com/tvguide/1128_2.png",
        ),
        Channel(
            id="Abu Dhabi TV",
            name="Abu Dhabi TV",
            url="https://elcinema.com/en/tvguide/1136/",
            icon_url="https://media0106.elcinema.com/tvguide/1136_2.png",
        ),
    )


def test_channel_and_programme_records_are_frozen():
    channel = Channel("MBC 2", "إم بي سي 2", f"{ELCINEMA}/tvguide/1128/")
    programme = Programme(
        channel_id=channel.id,
        title="فيلم",
        start=datetime(2026, 8, 13, 20, 30, tzinfo=DUBAI_OFFSET),
        stop=datetime(2026, 8, 13, 22, 30, tzinfo=DUBAI_OFFSET),
    )

    with pytest.raises(FrozenInstanceError):
        channel.name = "changed"
    with pytest.raises(FrozenInstanceError):
        programme.title = "changed"


@pytest.mark.parametrize(
    ("date_text", "time_text", "expected"),
    [
        ("الخميس 13 اغسطس", "08:30 مساءً", datetime(2026, 8, 13, 20, 30)),
        ("الخميس 13 أغسطس", "12:00 صباحًا", datetime(2026, 8, 13, 0, 0)),
        ("الخميس 13 أغسطس", "12:15 مساءً", datetime(2026, 8, 13, 12, 15)),
        ("الخميس 13 أغسطس", "09:05 صباحا", datetime(2026, 8, 13, 9, 5)),
    ],
)
def test_parse_arabic_datetime_handles_month_spelling_and_meridiem(
    date_text, time_text, expected
):
    parsed = parse_arabic_datetime(date_text, time_text, year=2026)

    assert parsed == expected.replace(tzinfo=DUBAI_OFFSET)


def test_parse_channel_page_keeps_english_channel_name_and_arabic_programmes(fixture_html):
    discovered = Channel(
        id="MBC 2",
        name="MBC 2",
        url=f"{ELCINEMA}/en/tvguide/1128/",
        icon_url="https://media0106.elcinema.com/tvguide/1128_2.png",
    )

    channel, programmes = parse_channel_page(
        fixture_html("channel_1128_ar.html"), discovered, year=2026
    )

    assert channel == Channel(
        id="MBC 2",
        name="MBC 2",
        url=f"{ELCINEMA}/en/tvguide/1128/",
        icon_url="https://media0106.elcinema.com/tvguide/1128_1.png",
    )
    assert isinstance(programmes, tuple)
    assert programmes == (
        Programme(
            channel_id="MBC 2",
            title="Miss Sloane & Friends",
            start=datetime(2026, 8, 13, 20, 30, tzinfo=DUBAI_OFFSET),
            stop=datetime(2026, 8, 13, 22, 30, tzinfo=DUBAI_OFFSET),
            description="سياسية ناجحة تواجه خصمًا قويًا.",
            category="فيلم",
        ),
        Programme(
            channel_id="MBC 2",
            title="برنامج World Of Movies 3",
            start=datetime(2026, 8, 13, 22, 30, tzinfo=DUBAI_OFFSET),
            stop=datetime(2026, 8, 13, 23, 0, tzinfo=DUBAI_OFFSET),
        ),
    )


def test_midnight_in_same_date_group_rolls_into_next_day(fixture_html):
    channel = Channel("Test TV", "Test TV", f"{ELCINEMA}/en/tvguide/9999/")

    parsed_channel, programmes = parse_channel_page(
        fixture_html("channel_midnight_ar.html"), channel, year=2026
    )

    assert parsed_channel.id == "Test TV"
    assert parsed_channel.name == "Test TV"
    assert [programme.start for programme in programmes] == [
        datetime(2026, 8, 13, 23, 0, tzinfo=DUBAI_OFFSET),
        datetime(2026, 8, 14, 0, 0, tzinfo=DUBAI_OFFSET),
    ]
    assert programmes[0].stop == programmes[1].start


def test_date_groups_roll_december_into_the_next_year(fixture_html):
    channel = Channel("New Year TV", "New Year TV", f"{ELCINEMA}/en/tvguide/9998/")

    _, programmes = parse_channel_page(
        fixture_html("channel_year_boundary_ar.html"), channel, year=2026
    )

    assert [programme.start for programme in programmes] == [
        datetime(2026, 12, 31, 23, 30, tzinfo=DUBAI_OFFSET),
        datetime(2027, 1, 1, 0, 0, tzinfo=DUBAI_OFFSET),
    ]
    assert programmes[0].stop == programmes[1].start
    assert programmes[1].stop == datetime(
        2027, 1, 1, 0, 45, tzinfo=DUBAI_OFFSET
    )


def test_january_scrape_anchors_previous_december_to_prior_year(fixture_html):
    channel = Channel("New Year TV", "New Year TV", f"{ELCINEMA}/en/tvguide/9998/")

    _, programmes = parse_channel_page(
        fixture_html("channel_year_boundary_ar.html"),
        channel,
        year=2027,
        reference_date=datetime(2027, 1, 1).date(),
    )

    assert [programme.start.date().isoformat() for programme in programmes] == [
        "2026-12-31",
        "2027-01-01",
    ]


def test_leap_day_anchor_ignores_invalid_adjacent_years():
    html = """
    <div class="panel jumbo"><h1>قناة كبيسة</h1></div>
    <div class="tvgrid"><div class="dates">الخميس 29 فبراير</div>
      <div class="boxed-category-0"><ul class="no-margin">
        <li>يوم كبيس</li><li>01:00 مساءً <span class="subheader">[60 دقيقة]</span></li>
      </ul></div>
    </div>
    """
    channel = Channel("Leap TV", "Leap TV", f"{ELCINEMA}/en/tvguide/9997/")

    _, programmes = parse_channel_page(
        html, channel, year=2028, reference_date=datetime(2028, 3, 1).date()
    )

    assert programmes[0].start.date().isoformat() == "2028-02-29"


def test_build_xmltv_emits_parseable_escaped_xml_with_dubai_timezone():
    channel = Channel(
        id="MBC & More",
        name="إم بي سي <المزيد>",
        url=f"{ELCINEMA}/tvguide/1128/?a=1&b=2",
        icon_url="https://media0106.elcinema.com/logo.png?a=1&b=2",
    )
    programme = Programme(
        channel_id=channel.id,
        title="Tom & Jerry <Special>",
        start=datetime(2026, 8, 13, 23, 30, tzinfo=DUBAI_OFFSET),
        stop=datetime(2026, 8, 14, 0, 30, tzinfo=DUBAI_OFFSET),
        description='قال "مرحبًا" & غادر',
        category="فيلم & رسوم",
    )

    xml = build_xmltv((channel,), (programme,))
    root = ElementTree.fromstring(xml)

    assert root.tag == "tv"
    assert root.find("channel").attrib["id"] == "MBC & More"
    assert root.findtext("channel/display-name") == "إم بي سي <المزيد>"
    xml_programme = root.find("programme")
    assert xml_programme.attrib == {
        "channel": "MBC & More",
        "start": "20260813233000 +0400",
        "stop": "20260814003000 +0400",
    }
    assert xml_programme.findtext("title") == "Tom & Jerry <Special>"
    assert xml_programme.findtext("desc") == 'قال "مرحبًا" & غادر'
    assert xml_programme.findtext("category") == "فيلم & رسوم"
    assert xml_programme.find("title").attrib["lang"] == "en"
    assert xml_programme.find("desc").attrib["lang"] == "ar"
    assert "MBC &amp; More" in xml
    assert "Tom &amp; Jerry &lt;Special&gt;" in xml


@pytest.mark.parametrize("html", ["", "   ", "<html><body><p>no guide</p></body></html>"])
def test_discover_channels_rejects_empty_or_unrecognized_source(html):
    with pytest.raises(SourceParseError):
        discover_channels(html, base_url=ELCINEMA)


@pytest.mark.parametrize("fixture_name", ["malformed_channel_ar.html"])
def test_parse_channel_page_rejects_malformed_source(fixture_html, fixture_name):
    channel = Channel("Broken TV", "Broken TV", f"{ELCINEMA}/en/tvguide/0/")

    with pytest.raises(SourceParseError):
        parse_channel_page(fixture_html(fixture_name), channel, year=2026)


def test_build_xmltv_rejects_naive_or_backwards_programme_datetimes():
    channel = Channel("Broken TV", "قناة معطوبة", f"{ELCINEMA}/tvguide/0/")
    naive = Programme(
        channel_id=channel.id,
        title="No timezone",
        start=datetime(2026, 8, 13, 20, 0),
        stop=datetime(2026, 8, 13, 21, 0),
    )
    backwards = Programme(
        channel_id=channel.id,
        title="Backwards",
        start=datetime(2026, 8, 13, 22, 0, tzinfo=DUBAI_OFFSET),
        stop=datetime(2026, 8, 13, 21, 0, tzinfo=DUBAI_OFFSET),
    )

    with pytest.raises(ValueError):
        build_xmltv((channel,), (naive,))
    with pytest.raises(ValueError):
        build_xmltv((channel,), (backwards,))


def test_build_xmltv_rejects_duplicate_channel_ids():
    first = Channel("Same ID", "الأولى", f"{ELCINEMA}/tvguide/1/")
    second = Channel("Same ID", "الثانية", f"{ELCINEMA}/tvguide/2/")

    with pytest.raises(ValueError, match="duplicate channel"):
        build_xmltv((first, second), ())


def test_build_xmltv_rejects_unknown_programme_channel():
    channel = Channel("Known", "معروف", f"{ELCINEMA}/tvguide/1/")
    programme = Programme(
        "Unknown",
        "مجهول",
        datetime(2026, 8, 13, 20, 0, tzinfo=DUBAI_OFFSET),
        datetime(2026, 8, 13, 21, 0, tzinfo=DUBAI_OFFSET),
    )

    with pytest.raises(ValueError, match="unknown channel"):
        build_xmltv((channel,), (programme,))


def test_build_xmltv_clips_source_overlap_to_next_start():
    channel = Channel("Test", "اختبار", f"{ELCINEMA}/tvguide/1/")
    first = Programme(
        channel.id,
        "الأول",
        datetime(2026, 8, 13, 20, 0, tzinfo=DUBAI_OFFSET),
        datetime(2026, 8, 13, 21, 5, tzinfo=DUBAI_OFFSET),
    )
    second = Programme(
        channel.id,
        "الثاني",
        datetime(2026, 8, 13, 21, 0, tzinfo=DUBAI_OFFSET),
        datetime(2026, 8, 13, 22, 0, tzinfo=DUBAI_OFFSET),
    )

    root = ElementTree.fromstring(build_xmltv((channel,), (first, second)))

    assert root.findall("programme")[0].attrib["stop"] == "20260813210000 +0400"


def test_build_xmltv_emits_subscription_name_and_id_aliases():
    channel = Channel(
        "Emirates",
        "Emirates",
        f"{ELCINEMA}/en/tvguide/1135/",
        aliases=("AR: Al Emarat TV HD", "AR: Al Emarat TV 4K ◉"),
        alias_ids=("AlEmarat.ae",),
    )
    programme = Programme(
        channel.id,
        "برنامج عربي",
        datetime(2026, 8, 13, 21, 0, tzinfo=DUBAI_OFFSET),
        datetime(2026, 8, 13, 22, 0, tzinfo=DUBAI_OFFSET),
    )

    root = ElementTree.fromstring(build_xmltv((channel,), (programme,)))

    assert [node.attrib["id"] for node in root.findall("channel")] == [
        "AlEmarat.ae",
        "Emirates",
    ]
    assert [
        node.text for node in root.find("channel[@id='Emirates']").findall("display-name")
    ] == ["Emirates", "AR: Al Emarat TV HD", "AR: Al Emarat TV 4K ◉"]
    assert [
        node.attrib["channel"] for node in root.findall("programme")
    ] == ["AlEmarat.ae", "Emirates"]
