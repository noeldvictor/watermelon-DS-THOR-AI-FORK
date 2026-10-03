# Rosario + Vampire: Tanabata no Miss Youkai Gakuen

YVLJ, Japan, with jjjewel's English translation patch v1.0. A visual novel by Capcom (Dimps).

| Enhancement | What it does | Checked |
| --- | --- | --- |
| HD pack | Every background, event CG and menu screen (197 pictures, 88,955 tiles) and the characters' portrait art (body, eyes and mouth frames, 2,796 sprite pieces), AI-upscaled from the ROM: backgrounds with 4x-UltraSharp, characters with the anime model, each frame upscaled over the whole body so eyes and mouth join without a seam. Recipe: [YVLJ](../tools/hd_remaster/games/YVLJ/README.md) | 994 of the first 1000 background tiles the title screens draw, and all 41 256-colour character sprites of a dialogue scene, are keys the extraction made |
| HD text | The dialogue and the names are drawn from the game's own `#FNT` font: the tool converts it ([`fnt.py`](../tools/hd_remaster/fnt.py)) so the emulator's HD text recognises each glyph in the text sprites and redraws it smooth | All 26 glyphs of a line recognised; following lines and the name plate too |

The game uses none of the standard Nitro graphics formats, so the tool learned its own:
BB archives of BBG pictures (tiled 256-colour backgrounds with extended palettes,
[`bbg.py`](../tools/hd_remaster/bbg.py)) and BAC sprite animations (character parts as
256-colour OBJ chunks, [`bac.py`](../tools/hd_remaster/bac.py)).

Not yet: the text window frame, the name plate frames and other 16-colour menu sprites (their
palette row is chosen at runtime).

![The Kotori scene on the Thor: pack off (left) and on (right)](../tools/hd_remaster/games/YVLJ/media/scene.jpg)

![Kotori, pack off and on](../tools/hd_remaster/games/YVLJ/media/face.jpg)
