# Lufia: Curse of the Sinistrals (BSDE)

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 "Lufia.nds" -Push
```

About 20 minutes on an RTX 3060 for the whole game: 1777 textures, 9574 sprites and 26064
background tiles, a 725 MB pack at 4x.

## Before / after

![Title screen: the logo and the ocean flyover texture, original vs HD pack](media/title.jpg)

AYN Thor at 4x internal resolution, the same frame of a save state with the pack off and on.

## AI-redrawn portraits

![Native pixels, 4x upscale and AI redraw: Tia, Gades, Guy](media/portraits.jpg)

180 of the 229 character portraits are redrawn with `google/gemini-3-pro-image` from the
official character art (Yusuke Naora, via Creative Uncut), one call per portrait, each checked
against the game's face by a vision model. The other 49, minor characters whose redraw failed
the check, keep the 4x upscale. Gades' armour is the upscale under a redrawn head. The title
logo keeps the upscale: its redraw read worse. How to do this for another game:
[REDRAW.md](../../REDRAW.md). Settings: [redraw.json](redraw.json).

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

When two characters talk (two portraits on screen), the game loads one portrait with the usual
shift but into palette memory it shares with the other, and the other portrait with a shift of
144. Neither matched, so both stayed native. The rule's `also_shifts: [144]` and `wildcard`
options add, for every portrait piece, a key for the 144 shift and a `$`-palette key; the ship
scene's 59 missed pieces all match now.
