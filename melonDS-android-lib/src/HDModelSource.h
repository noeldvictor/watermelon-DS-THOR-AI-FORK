#ifndef MELONDS_HDMODELSOURCE_H
#define MELONDS_HDMODELSOURCE_H

#include <vector>

#include "types.h"

namespace melonDS
{
/**
 * Where the geometry engine finds HD model replacements (GPU3D_HDModels.cpp): a display list,
 * keyed by the XXH64 of its bytes and its size (the `mdl1_<size>_<hash>` names
 * tools/hd_remaster/models3d.py gives model shapes), maps to the display list to run instead.
 * Implemented by HDTexPack (its models/ folder).
 */
class HDModelSource
{
public:
    // a replaced display list's identity and first words, for recognising it when the game
    // writes it to the GX FIFO with the CPU (no DMA to hash it from)
    struct Original
    {
        u64 Hash;
        u32 Size;
        u32 PrefixLength;   // words of Prefix that are valid
        u32 Prefix[4];
    };

    virtual ~HDModelSource() = default;
    // the replacement's words, or nullptr; the pointer stays valid while the source lives
    virtual const std::vector<u32>* LookupModel(u64 hash, u32 size) const = 0;
    // replaced display lists starting with this word (nullptr when none)
    virtual const std::vector<Original>* OriginalsStartingWith(u32 word) const = 0;
};
}

#endif
