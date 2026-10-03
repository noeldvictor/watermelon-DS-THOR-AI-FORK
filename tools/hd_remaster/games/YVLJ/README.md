# Rosario + Vampire: Tanabata no Miss Youkai Gakuen (YVLJ)

Japan, with jjjewel's English translation patch v1.0 (sha256 in [recipe.json](recipe.json)).
A visual novel by Capcom (Dimps): painted backgrounds and character portraits.

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 "Rosario to Vampire Tanabata no Miss Youkai Gakuen (English Patched v1.0).nds" -Push
```

![The Kotori scene on the Thor: pack off (left) and on (right)](media/scene.jpg)

![Kotori, pack off and on](media/face.jpg)

## What the pack covers

- **Backgrounds, event CGs, title and menu screens**: 197 pictures, 88,955 BG tile keys,
  4x-UltraSharp.
- **Character portraits**: 2,796 sprite pieces (upper body, lower body, eye and mouth frames of
  every character, in both palettes each character has), Real-ESRGAN x4plus_anime_6B.
- **Dialogue text and names**: the game's `#FNT` font (3,387 glyphs) converted to NFTR, so the
  emulator's HD text recognises the glyphs it draws into sprites and redraws them. It is a
  1-pixel pixel font (fill shade 1, accent pixels shade 2): its HD glyphs are drawn from the fill
  mask, smoothly sampled at 4x with soft edges, not upscaled by a model (one turned the grey
  strokes into hollow outlines, and closed the gap between an i's dot and its stem).

![Dialogue text, pack off (top) and on (bottom)](media/text.jpg)

Not covered: the text window frame and the name plate frames (16-colour sprites: their palette
row is chosen at runtime), 16-colour menu sprites.

## How the game stores its graphics

No standard Nitro graphics files. Everything sits in `.bb` archives (`BB` + count + offset/size
pairs) and NARCs:

- **BBG** pictures ([bbg.py](../../bbg.py)): a header with the bit depth and size in tiles, then
  three sections, each LZ10 or stored: tiles, a DS screen map, a 256-colour palette. The game
  shows them as 256-colour text BGs with extended palettes, which is what the BG keys hash.
- **BAC** sprite animations ([bac.py](../../bac.py)): records naming a frame and, per piece, a
  pixel chunk; frames as OAM attributes with a corner relative to the character's anchor; two
  256-colour palettes; chunks that go to OBJ VRAM unchanged (1D mapping). A character is four
  BACs in one archive entry: `up`, `down`, `eye`, `mouth`. Every frame is upscaled drawn over
  the whole body (first upper- and lower-body frames, placed by the anchor), so eyes, mouth and
  waist join without a seam.
- **#FNT** font ([fnt.py](../../fnt.py)): 3,387 glyphs, 16x16 at 4 bpp, each LZ10-compressed,
  2x2 tiles of 8x8; only shades 0-2 in use, so the converted NFTR is 2 bpp.

## Verified

On the Thor (2026-10-02), with a one-image probe pack logging every tile the game drew: 994 of
the first 1000 BG tiles of the title screens and all 41 256-colour sprites of a dialogue scene
(Kotori: body, eyes, mouth, plus another character) are keys the extraction made. With the pack:
the scene's BG lookups 92,160 of 138,240 hit (the rest are the text window), the portrait is HD,
eyes and mouth join the face without a seam, and the HD text recognises all 26 glyphs of "Good
morning, Kotori-sempai." (and the next lines, and the name in the name plate).
