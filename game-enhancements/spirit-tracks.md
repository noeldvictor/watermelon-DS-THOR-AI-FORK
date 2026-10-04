# The Legend of Zelda: Spirit Tracks

BKIE, USA, with the D-pad patch.

| Enhancement | What it does | Checked |
| --- | --- | --- |
| HD pack | Textures, sprites, BG tiles and four fonts, AI-upscaled from the ROM (200 MB ASTC zip). Textures drawn with a faded palette stay native until the fade ends. Recipe: [BKIE](../tools/hd_remaster/games/BKIE/README.md) | Opening demo, BG tiles 92160/92160 |
| HD message text | The message boxes' text (sprites drawn by NitroSystem's CharCanvas) is drawn HD without the native letters' grey edge around it (`f7589066`) | Save-file messages |
| Save state loads | Loading a state (or rewinding) no longer flashes a half-drawn screen for a frame (`c418d56c`) | Repeated loads, recorded |
| Behind-Link camera | The free camera is on for this game (pack `camera.txt`, Phantom Hourglass's values): low behind Link, swinging round as he walks; the right stick turns, tilts and zooms on top; R3 resets | Niko's house; train, towns and dungeons not checked yet |

![Title, pack off and on](../tools/hd_remaster/games/BKIE/media/title.jpg)

![Niko's house in game, from behind Link](../tools/hd_remaster/games/BKIE/media/ingame_niko_house.png)
