# Mario Kart DS (Europe)

AMCP, Europe.

| Enhancement | What it does | Checked |
| --- | --- | --- |
| Widescreen, added | The bundled database has a widescreen code only for the US version. The same two patches (aspect literal, `mov r0,#0xAA000`) sit at the same addresses in the European ARM9; shipped with guards on the original values (`214cb277`). The literal is read at camera setup, so a change shows from the next race | Grand Prix race start, 16:9 |
