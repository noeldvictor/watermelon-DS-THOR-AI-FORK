# The Legend of Zelda: Phantom Hourglass (AZEE)

```
powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 "Phantom Hourglass.nds" -Push
```

1934 textures, 4608 sprites and 7811 background tiles.

Everything is stored as standard Nitro files, so no game-specific reader or rule is needed, and
1773 of the 1934 textures are paired with their palettes through the models' own materials.

## Not verified yet

The keys haven't been checked against textures dumped during play. To help, play a while with
texture dumping on, pull `files/texturedumps/AZEE` from the device and run:

```
tools\hd_remaster\.venv\Scripts\python tools\hd_remaster\hd_remaster.py verify tools\hd_remaster\work\AZEE --dumps <dumps>\textures --sprites <dumps>\sprites
```

Only the D-pad patched USA ROM has been tried. Run the retail ROM and compare the counts that
`extract` prints with the recipe's baseline.
