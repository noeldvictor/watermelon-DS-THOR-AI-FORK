# Star Fox Command

ASFE, USA.

| Enhancement | What it does | Checked |
| --- | --- | --- |
| Widescreen, fixed | The bundled database's widescreen code wrote a fixed address that holds nothing in this version (the camera lives on the heap and moves per session). The shipped code follows the camera manager's pointer to the active camera's aspect field and only writes the stock value's replacement (`9700d64e`). The Lylat map keeps its flat projection | Flight, 16:9 |
| Title fade | The title's fade is an alpha blend of the 3D with a white layer on alternating screens; every other frame was drawn at full colour (`9e3c93cb`) | Vulkan vs software sweep |
| Boot screen | One black frame inside the white boot screens is gone (`4e231232`) | Boot burst capture |
