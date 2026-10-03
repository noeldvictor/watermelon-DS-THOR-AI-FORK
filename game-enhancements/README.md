# Game enhancements

What Watermelon Thor does for particular games, one page per game. Everything listed was checked
on the AYN Thor. Kinds of enhancement:

- **HD pack**: textures, sprites, backgrounds and fonts AI-upscaled straight from the ROM
  ([the tool](../tools/hd_remaster/README.md)); the pack is installed per game and loads by
  itself (Settings -> Video -> Load texture packs).
- **AI redraws**: character portraits redrawn from the official art by an image model
  ([guide](../tools/hd_remaster/REDRAW.md)).
- **HD 3D models**: a model part replaced by a new mesh on the game's own skeleton and lights
  (Vulkan renderer; [how](../tools/hd_remaster/README.md#3d-models-first-step-extraction-and-keys)).
- **Camera**: the free camera (right stick) on for the game, set up by the pack's `camera.txt`
  (see [Free camera](../README.md#what-this-fork-adds)).
- **Widescreen**: a working 16:9 code for the pause menu's Enhancements panel where the bundled
  cheat database had a broken one or none ([`code_fixes.txt`](../app/src/main/assets/cheats/code_fixes.txt)).
  About 300 games have widescreen, anti-aliasing or draw-distance codes in the bundled database
  already; they show the ENH badge in the ROM list.
- **Rendering fixes**: a game-visible bug in the Vulkan renderer found on that game and fixed
  (the fix helps every game that does the same thing).

## Games

| Game | Code | HD pack | AI redraws | 3D models | Camera | Widescreen | Rendering fixes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [The Legend of Zelda: Phantom Hourglass](phantom-hourglass.md) | AZEE | yes | | Link | behind Link | | shadows, storybook |
| [Lufia: Curse of the Sinistrals](lufia-curse-of-the-sinistrals.md) | BSDE | yes | 180 portraits | (smoothing test) | | | intro, title, portraits |
| [The Legend of Zelda: Spirit Tracks](spirit-tracks.md) | BKIE | yes | | | | | message text, state loads |
| [Nostalgia](nostalgia.md) | CJKE | yes | | | | | |
| [Chrono Trigger](chrono-trigger.md) | YQUE | sprites | | | | | |
| [Star Fox Command](star-fox-command.md) | ASFE | | | | | fixed code | title fade, boot screen |
| [Mario Kart DS (Europe)](mario-kart-ds.md) | AMCP | | | | | added | |
| [New Super Mario Bros. (Europe)](new-super-mario-bros.md) | A2DP | | | | | added | |
| [Sonic & SEGA All-Stars Racing (Europe)](sonic-sega-all-stars-racing.md) | CS3P | | | | | added | |
| [Burnout Legends (Europe)](burnout-legends.md) | ABOP | | | | | added | |
| [Hotel Dusk: Room 215](hotel-dusk.md) | | | | | | | text blink, state loads |
| [Castlevania: Dawn of Sorrow](castlevania-dawn-of-sorrow.md) | | | | | | | intro video |
| [Metroid Prime Hunters](metroid-prime-hunters.md) | AMHE | | | | | (bundled) | intro flash |
| [Dragon Quest IV](dragon-quest-iv.md) | | | | | | | opening band |
| [Mega Man ZX](mega-man-zx.md) | | | | | | | logo fade |
| [Solatorobo](solatorobo.md) | | | | | | | boot screen |

## Wishlist

Games wanted for an HD pack next, not started. All run at 60 fps on the Thor (45 s intro
recordings of both screens, 2026-09-24): Lost Magic, Rosario + Vampire, Rummikub, Pokemon Black
Version 2 (its bottom screen stays black through intro and title, as on the software renderer),
Professor Layton and the Last Specter, Metroid Prime Hunters, Mario Slam Basketball, Mega Man ZX,
Lunar Knights.

## Checked, not a candidate

Games whose graphics turned out not to be buildable from the ROM, with the reason, so nobody
repeats the work. Nothing here yet.

## Adding a game

A game gets a page here when the emulator does something for it. Name the page after the game,
start with its code and region, list each enhancement with what it does and how it was checked,
and add a row to the table. HD pack recipes, their build steps and before/after images live in
[tools/hd_remaster/games](../tools/hd_remaster/games/README.md); link to them rather than
repeating them.
