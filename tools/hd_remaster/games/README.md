# Game recipes

One folder per game, named by its 4-letter game code. A recipe records what was learned making a
good HD pack for that game, so anyone with the same ROM gets the same pack on their own PC:

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 path\to\game.nds -Push
```

The tool reads the game code from the ROM and applies the matching recipe automatically. Games
without a recipe still work with the defaults.

Recipes contain no game data: every image is generated from your own ROM.

| Game | Code | Textures | Sprites | Backgrounds | Verified |
| --- | --- | --- | --- | --- | --- |
| [Lufia: Curse of the Sinistrals](BSDE/README.md) | BSDE | 1777 | 9574 | 26064 | textures and sprites pixel-identical to in-game dumps |
| [The Legend of Zelda: Phantom Hourglass](AZEE/README.md) | AZEE | 1934 | 4608 | 7811 | not yet |
| [The Legend of Zelda: Spirit Tracks](BKIE/README.md) | BKIE | 3156 | 5422 | 21424 | on device, opening demo (see its README) |

## recipe.json

| Field | Meaning |
| --- | --- |
| `roms` | ROMs the recipe was verified on, by SHA-256. Another revision still runs, with a warning. |
| `scale` | Output scale, 2 or 4. One scale per pack: the emulator requires it. |
| `models` | Model per category (`textures`, `sprites`, `backgrounds`), by name from `../models.json`. Models download on first use. |
| `twod` | Game-specific 2D load rules the files don't describe (see `twod.py`). |
| `baseline` | Key counts an extraction should produce. `extract` compares against them. |
| `verified` | What was checked against real play, and when. |
| `notes` | Anything worth knowing about the game. |

## Adding a game

1. `hd_remaster.py extract game.nds`, then create `games/<CODE>/recipe.json` with the ROM's
   SHA-256 (printed by extract when the ROM isn't known) and the counts extract printed as the
   `baseline`.
2. Play a while with texture dumping on (Settings → Video), pull
   `files/texturedumps/<CODE>` from the device and run
   `hd_remaster.py verify work/<CODE> --dumps <dumps>/textures --sprites <dumps>/sprites`. Pixel
   mismatches must be 0; record the coverage in `verified`.
3. Sprites whose tiles match but whose palettes don't, or that only match after a transform,
   need a `twod` rule. Lufia's portrait rule is the worked example.
4. Try the pack on a device, then fill in `verified.on_device`.
