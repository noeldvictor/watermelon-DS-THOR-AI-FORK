"""Build the bundled cheat database asset from an R4CCE/DeSmuME cheats.xml.

    python tools/cheats/build_bundled_cheats.py [--index-only] [cheats.xml[.gz]] [out.zip]

The source defaults to tools/cheats/source/cheats.xml.gz, the database the app ships.

Writes one `<GAMECODE>.xml` per game code into a zip (default
app/src/main/assets/cheats/bundled_cheats.zip). Each entry is a complete
<codelist> with the source database's <name> and every <game> whose gameid
starts with that code (revisions and hacks share a code; the app picks the
one whose header checksum matches). The app opens a ROM's entry the first
time its cheats are shown, so nothing is imported up front.

Also writes bundled_cheats_index.txt next to the zip: one line per game,
"<CODE> <CHECKSUM> <cheats> <E|->", E when the game has an enhancement code
(widescreen, anti-aliasing, draw distance). The ROM list reads it for its
CHT / ENH badges without opening the zip. --index-only rewrites just the index.
The name rule must match EnhancementCheats.kt (BundledCheatAssetTest checks).
"""
import gzip
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO / "tools" / "cheats" / "source" / "cheats.xml.gz"
DEFAULT_OUT = REPO / "app" / "src" / "main" / "assets" / "cheats" / "bundled_cheats.zip"
FIXED_TIME = (2021, 12, 25, 0, 0, 0)  # deterministic zip entries
# EnhancementCheats.kt: widescreen (not Animal Crossing's "Widescreen TV" item), anti-aliasing, draw distance
ENHANCEMENT = re.compile(r"wide\s*screen(?!\s*tv)|\b16\s*:\s*(9|10)\b|anti.?alias|draw\s*distance", re.IGNORECASE)


def write_index(games, path):
    entries = {}
    for game in games:
        code, _, checksum = (game.findtext("gameid") or "").strip().partition(" ")
        key = (code.upper(), checksum.strip().upper())
        names = [cheat.findtext("name") or "" for cheat in game.iter("cheat")]
        count, enhanced = entries.get(key, (0, False))
        entries[key] = (count + len(names), enhanced or any(ENHANCEMENT.search(name) for name in names))
    lines = [f"{code} {checksum} {count} {'E' if enhanced else '-'}" for (code, checksum), (count, enhanced) in sorted(entries.items())]
    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    print(f"index: {len(lines)} games, {sum(1 for line in lines if line.endswith('E'))} with enhancement codes -> {path}")


def main():
    args = [arg for arg in sys.argv[1:] if arg != "--index-only"]
    index_only = len(args) != len(sys.argv) - 1
    source = Path(args[0]) if len(args) > 0 else DEFAULT_SOURCE
    out = Path(args[1]) if len(args) > 1 else DEFAULT_OUT

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
    write_index([game for code in sorted(by_code) for game in by_code[code]], out.with_name("bundled_cheats_index.txt"))
    if index_only:
        return

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
