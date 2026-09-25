# Game recipes

One folder per game, named by its 4-letter game code. A recipe records what was learned making a
good HD pack for that game, so anyone with the same ROM gets the same pack on their own PC:

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 path\to\game.nds -Push
```

The tool reads the game code from the ROM and applies the matching recipe automatically. Games
without a recipe still work with the defaults.

Recipes contain no game data: every image is generated from your own ROM.

## Games

**[GAMES.md](GAMES.md)** lists every game with a recipe - status, before/after screenshots,
pack size, build time, coverage - plus the wishlist and the games checked and dropped. Each
game's own README has the command, what was verified, and notes on how that game stores its
graphics.

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
2. `build` and `push`, then play on the device and watch the log: `HDTexPack[Stats]` gives
   hits/lookups per kind every second, and `HDTexPack[Miss]` names each key the pack didn't
   have (up to 200 per kind). A miss whose tile or texture hash is in the pack but whose palette
   hash isn't means the game loads its palette differently from the files; one whose hash isn't
   anywhere was built at runtime.
3. Sprites whose tiles match but whose palettes don't, or that only match after a transform,
   need a `twod` rule. Lufia's portrait rule is the worked example.
4. Fill in `verified.on_device` with what you played and the hit counts. (`verify` still
   compares an extraction with texture dumps, for anyone who has them; this fork no longer
   dumps in game.)
5. Take a before/after screenshot for [GAMES.md](GAMES.md) (see below), write the game's
   README and add its row.

## Screenshots

Each game's `media/` holds before/after images from the device: the same frame with the pack
off and on, side by side. To make one:

1. Pick a scene that shows what the pack covers (a portrait, a painted screen, a title) and
   make a save state there.
2. With **Settings → Video → Load texture packs** off, load the state, pause and take a
   screenshot of the screen that shows it; switch it on, load the same state and take the
   same screenshot. The switch takes effect in a running game.
3. `python tools/hd_remaster/beforeafter.py off.png on.png --crop X,Y,W,H -o games/<CODE>/media/<scene>.jpg`
   crops the same box out of both and puts them side by side, labelled.
