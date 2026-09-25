# Lufia: Curse of the Sinistrals (BSDE)

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 "Lufia.nds" -Push
```

About 20 minutes on an RTX 3060 for the whole game: 1777 textures, 9574 sprites and 26064
background tiles, a 725 MB pack at 4x.

## Before / after

![Title screen: the logo and the ocean flyover texture, original vs HD pack](media/title.jpg)

AYN Thor at 4x internal resolution, the same frame of a save state with the pack off and on.

## What was verified (2026-09-22)

Against textures and sprites dumped while playing on an AYN Thor:

| | Reproduced from the ROM | Pixel-identical |
| --- | --- | --- |
| 3D textures | 181 of 223 | 181 of 181 |
| Sprites | 324 of 587 | 324 of 324 |

The rest are generated while the game runs, so no ROM-based pack can contain them:

- dialogue and name text, drawn into sprite memory from the font,
- 64x64 bitmap sprites captured from the 3D scene,
- character parts the game assembles into a single texture.

The per-layer filters still apply to all of those.

## Game-specific rule

Dialogue portraits (`2d/bustup/`) are uploaded with every non-zero colour index shifted by 48,
into a 256-colour palette built from the font, window and name-plate palettes followed by the
portrait's own first 208 colours. Without that rule none of the 228 portrait sprites match; with
it all of them do.
