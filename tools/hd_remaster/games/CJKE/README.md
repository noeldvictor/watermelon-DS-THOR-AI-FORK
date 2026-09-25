# Nostalgia (CJKE)

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 "Nostalgia (USA).nds" -Push
```

4434 textures, 4613 sprites and 231,399 background tiles: 240,438 images, 1.2 GB at 4x. The
upscale took about 35 minutes on an RTX 3060.

## Before / after

![Title screen, original vs HD pack](media/title.jpg)

AYN Thor at 4x internal resolution, the same frame with the pack off and on.

## How the game stores its graphics

Nearly everything lives in `MASS/*.dat` archives (magic `SSAM`: a count, then offset, size and
a 32-byte name per file, offsets counted from the end of the table), most files LZ10
compressed. The maps (`*.mmc`) decompress to a table of parts, some nested, each part an `NMDP`
wrapper around a standard BMD0 or BTX0. `nitro.files()` unpacks all three; without them the
tool finds one texture in the whole ROM.

Seven palettes (both title screens among them) store 0 in their size field and let the colours
run to the end of the block. Read literally they are empty, and the title screens came out
black.

Compressed files keep their type in the name (`ci_01_d.NSCR.lz`), so the tool strips both
extensions before matching a screen to a palette by name: `ci_01_d` is the bottom screen of the
painted intro and takes `ci_d.NCLR`. With the `.NSCR` left on, it tied with `ci_u.NCLR` and
every intro page was extracted twice, once in the wrong colours.

## Backgrounds use the palette wildcard

The painted screens are 8bpp BGs whose palettes the game loads into palette memory next to
colours it keeps there for other things. An exact BG key hashes the whole palette memory, so it
missed on almost every tile even though the tile hashes were right (196 of 200 logged misses).
`build` now stores a tile that occurs in only one image under the `$` palette wildcard, which
covers 192,768 of the 231,389 tile files. If the game recoloured such a tile through its
palette, the pack would show it in the original colours.

About 171,000 of the tiles are the painted world map in `mass_navi_map.dat`, which has almost no
repeated tiles; that is most of the pack's size.

## Verified

2026-09-25 on the AYN Thor: the painted intro (BG tiles 91080/92160 lookups hit), the title
screen and main menu (92160/92160), sprites 240/240 on the title. Towns, dungeons and battles
not played yet.
