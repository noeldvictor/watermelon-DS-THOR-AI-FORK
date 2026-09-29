# Bundled cheat database

The app ships **DeadSkullzJr's NDS(i) Cheat Database** so cheats are there without importing
anything. Credit for the database goes to DeadSkullzJr and everyone listed in its `!Credits`
entries (see `AAAA 00000000` / `AAAB 00000001` in the data).

| | |
| --- | --- |
| Edition | `DeadSkullzJr's NDS(i) Cheat Database (20211225)`: 4079 games, 597,353 cheats |
| Source | `Cheat Databases/cheats.xml` from the GitHub mirror of the project's Bitbucket repository, [3song/Hacking-DeadSkullzJr-s-NDS-i--Cheat-Databases](https://github.com/3song/Hacking-DeadSkullzJr-s-NDS-i--Cheat-Databases) (the Bitbucket original is gone; newer editions are posted on the project's [GBAtemp thread](https://gbatemp.net/threads/deadskullzjrs-nds-i-cheat-databases.488711/)) |
| Source SHA-256 | `8e28cd24dc7a282a86bd080931005e16de24508749e56ec2c495a2947fe2aef4` (of the uncompressed `cheats.xml`) |
| Kept here | `source/cheats.xml.gz` (the 103 MB XML is over GitHub's 100 MB file limit) and the project's `source/Changelog.txt` |
| License | GNU AGPL v3, shipped as `app/src/main/assets/cheats/LICENSE.txt` (the project's own `LICENSE`, identical in every mirror) |

## How the app uses it

`app/src/main/assets/cheats/bundled_cheats.zip` holds one `<GAMECODE>.xml` codelist per game
code (3661 entries). Nothing is imported at startup: the first time the cheats screen is opened
for a ROM with no cheats in the app database, `RoomCheatsRepository.findGameForRom` reads that
code's entry, picks the game whose header checksum matches the ROM, and adds it under the
database's name. Importing all ~600k cheats up front would bloat the app database and the
settings mirror, which copies every cheat into `WatermelonThor.opts`.

A newer edition can still be imported by hand (Settings -> Cheats -> Import); imported games
take precedence because the bundled copy is only read for games the app database doesn't have.

## Updating

```
python tools/cheats/build_bundled_cheats.py                      # from source/cheats.xml.gz
python tools/cheats/build_bundled_cheats.py path/to/cheats.xml   # a newer edition
```

rewrites `bundled_cheats.zip` (byte-identical for the same source). For a newer edition, replace
`source/cheats.xml.gz` (gzip it with mtime 0) and `source/Changelog.txt` too. Update the edition, checksum and counts here, and the totals in
`BundledCheatAssetTest.everyBundledEntryParses`.
