"""Build the bundled cheat database asset from an R4CCE/DeSmuME cheats.xml.

    python tools/cheats/build_bundled_cheats.py [cheats.xml[.gz]] [out.zip]

The source defaults to tools/cheats/source/cheats.xml.gz, the database the app ships.

Writes one `<GAMECODE>.xml` per game code into a zip (default
app/src/main/assets/cheats/bundled_cheats.zip). Each entry is a complete
<codelist> with the source database's <name> and every <game> whose gameid
starts with that code (revisions and hacks share a code; the app picks the
one whose header checksum matches). The app opens a ROM's entry the first
time its cheats are shown, so nothing is imported up front.
"""
import gzip
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO / "tools" / "cheats" / "source" / "cheats.xml.gz"
DEFAULT_OUT = REPO / "app" / "src" / "main" / "assets" / "cheats" / "bundled_cheats.zip"
FIXED_TIME = (2021, 12, 25, 0, 0, 0)  # deterministic zip entries


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT

    opener = gzip.open if source.suffix == ".gz" else open
    with opener(source, "rb") as stream:
        root = ET.parse(stream).getroot()
    if root.tag != "codelist":
        sys.exit(f"{source}: root element is <{root.tag}>, expected <codelist>")
    database_name = root.findtext("name") or "Bundled cheats"

    by_code = defaultdict(list)
    for game in root.findall("game"):
        gameid = (game.findtext("gameid") or "").strip()
        code = gameid[:4].upper()
        if len(code) != 4 or " " not in gameid:
            print(f"skipping game without a usable gameid: {game.findtext('name')!r} {gameid!r}")
            continue
        by_code[code].append(game)

    out.parent.mkdir(parents=True, exist_ok=True)
    name_xml = ET.Element("name")
    name_xml.text = database_name
    games = cheats = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for code in sorted(by_code):
            parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<codelist>\n', ET.tostring(name_xml, encoding="unicode"), "\n"]
            for game in by_code[code]:
                game.tail = "\n"
                parts.append(ET.tostring(game, encoding="unicode"))
                games += 1
                cheats += len(game.findall(".//cheat"))
            parts.append("</codelist>\n")
            info = zipfile.ZipInfo(f"{code}.xml", FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, "".join(parts).encode("utf-8"), compresslevel=9)

    print(f"{database_name}: {len(by_code)} game codes, {games} games, {cheats} cheats -> {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
