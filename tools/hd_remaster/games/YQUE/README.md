# Chrono Trigger (YQUE)

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 "Chrono Trigger.nds" -Push
```

Sprites only: 22,670 sprite images (85% of them the character sheets under `Chara/`), cut into
54,754 pack files, 412 MB at 4x. The upscale took about 45 minutes on an RTX 3060. The game has
no 3D textures and no NFTR fonts.

## Before / after

![Title logo (sprites), original vs HD pack](media/title.jpg)

AYN Thor at 4x internal resolution, the same frame with the pack off and on.

## Backgrounds are not covered yet

The fields and towns are the SNES game's maps. The tiles are in `Map/Bg/Bg_*_ncg.bin` and the
palettes in `BgPlt_*_ncl.bin` (11 rows of 16 colours), but there are no Nitro screen files: the
game lays each map out at runtime from `PS_BIN/ChipTable/*.dat` (16x16 chips, 3072 bytes each)
and `PS_BIN/MapTable/*.dat` (the maps, compressed). A 4bpp BG key includes the palette row, and
only the chip tables say which row each tile uses, so without parsing them the tool has no key
for any field tile. Parsing the chip tables would give both the palette rows and whole maps to
upscale in one piece.

## Verified

2026-09-24 on the AYN Thor: the title logo is sprites, 2580 of 2640 lookups hit. The anime
intro is a movie and the pendulum sequence and fields are BG, so they stay native.
