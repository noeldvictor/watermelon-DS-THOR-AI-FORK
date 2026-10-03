# Rosario + Vampire: Tanabata no Miss Youkai Gakuen

YVLJ, Japan, with jjjewel's English translation patch v1.0. A visual novel by Capcom (Dimps).

| Enhancement | What it does | Checked |
| --- | --- | --- |
| HD pack | Every background, event CG and menu screen (197 pictures, 88,955 tiles), the characters' portrait art (body, eyes and mouth frames, 2,796 sprite pieces) and the menus, text window, name plates and buttons (11,800 16-colour sprite pieces), AI-upscaled from the ROM: backgrounds with 4x-UltraSharp, characters with the anime model, each frame upscaled over the whole body so eyes and mouth join without a seam. Recipe: [YVLJ](../tools/hd_remaster/games/YVLJ/README.md) | 994 of the first 1000 background tiles the title screens draw, and all 41 256-colour character sprites of a dialogue scene, are keys the extraction made |
| HD text | The dialogue and the names are drawn from the game's own `#FNT` font: the tool converts it ([`fnt.py`](../tools/hd_remaster/fnt.py)) so the emulator's HD text recognises each glyph in the text sprites and redraws it smooth | All 26 glyphs of a line recognised; following lines and the name plate too |
| Renderer fix | The game leaves a full-strength alpha blend (EVA 16, EVB 0) on every picture and a brightness fade at 0 after its logos. Neither changes a pixel, but the 2D renderer marked those pixels as blended and HD replacement skipped them: the logos, the opening and the title stayed native. Such a blend or fade now leaves the pixel as it is | Logos, opening and title HD on the Thor; the 13 frame-exact regression scenes unchanged |

The game uses none of the standard Nitro graphics formats, so the tool learned its own:
BB archives of BBG pictures (tiled 256-colour backgrounds with extended palettes,
[`bbg.py`](../tools/hd_remaster/bbg.py)) and BAC sprite animations (character parts as
256-colour OBJ chunks, [`bac.py`](../tools/hd_remaster/bac.py)).

Not yet: one 16-colour piece (`fix_up.bac`) the game draws with a palette from elsewhere.

![The Kotori scene on the Thor: pack off (left) and on (right)](../tools/hd_remaster/games/YVLJ/media/scene.jpg)

![Kotori, pack off and on](../tools/hd_remaster/games/YVLJ/media/face.jpg)
