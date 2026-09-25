# The Legend of Zelda: Spirit Tracks (BKIE)

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 "Spirit Tracks.nds" -Push
```

3156 textures, 5422 sprites and 21424 background tiles: a 438 MB pack at 4x, upscaled in
about 23 minutes.

## Before / after

![Title logo over the sky, original vs HD pack](media/title.jpg)

AYN Thor at 4x internal resolution, the same frame of a save state with the pack off and on.

Same engine family as Phantom Hourglass: everything is stored as standard Nitro files in NARC
archives, so no game-specific reader or rule is needed. Most textures are paired with their
palettes through the models' own materials, plus each material palette's animation family (the
blink swaps `zeld_eye1` for `zeld_eye2` and `zeld_eye3`). The four NFTR fonts (dialogue, credits and
the name font) are packed for HD text.

## Faded palettes stay native

The opening fades in from darkness by rewriting palettes. A pack key hashes the palette, so a
texture drawn with a faded palette has no key and stays native until the palette is back to
normal. On the opening, 196 of 200 logged texture misses were this.

## Not verified yet

The keys haven't been checked against textures dumped during play, and in-game dumping is no
longer used; check on the device instead with the `HDTexPack[Stats]` hit counts while playing.

Only the D-pad patched USA ROM has been tried. Run the retail ROM and compare the counts that
`extract` prints with the recipe's baseline.
