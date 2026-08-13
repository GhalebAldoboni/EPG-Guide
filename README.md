# EPG Guide

Arabic and Turkish XMLTV guides for IPTV players.

- [Arabic EPG](https://raw.githubusercontent.com/GhalebAldoboni/EPG-Guide/master/ArabicEPG.xml)
- [Turkish EPG](https://raw.githubusercontent.com/GhalebAldoboni/EPG-Guide/master/TurkishEPG.xml)

## Arabic guide updates

`ArabicEPG.xml` is generated from the Arabic listings published by
[elCinema](https://elcinema.com/ar/tvguide/). The updater discovers channel IDs
and display names from the English guide so existing playlist `tvg-id` values
continue to match, then obtains programme titles, descriptions, and categories
from the Arabic guide pages.

Four additional Arabic channels present in the subscription playlist but absent
from elCinema are sourced from the official ART and SNRT endpoints catalogued by
[iptv-org/epg](https://github.com/iptv-org/epg): ART Aflam 1, ART Aflam 2,
ART Cinema, and Assadissa. Candidates that currently return no usable programme
data are not emitted.

`channel_aliases.json` adds verified Xtream display-name and `tvg-id` aliases so
the same schedule can match alternate channel names used by IPTV playlists. It
contains no subscription credentials or provider URLs.

Run a refresh locally with Python 3.12 or later:

```sh
python update_epg.py
```

The update is fail-safe: it downloads all channel pages, validates the channel
count, programme count, dates, durations, references, and guide horizon, then
atomically replaces the XML. If fetching or validation fails, the previous guide
is preserved. GitHub Actions is scheduled approximately every three hours and
commits only a validated change. Refreshing the Arabic EPG in an IPTV application
downloads the latest available committed guide from the same raw URL; no URL
change is required. The automation runs entirely on GitHub's servers and does
not require a personal computer to remain powered on. GitHub scheduling, its
raw-file cache, or the IPTV player's own cache can add a short delay.

The primary source normally exposes about four days of listings. Website times
are preserved as `Asia/Dubai` wall-clock times (`+0400`) so IPTV applications in
the UAE show the same programme time instead of adding one hour.
Please use the generated listings in accordance with elCinema's terms and
applicable rights; `robots.txt` permission to crawl does not itself grant content
redistribution rights.
