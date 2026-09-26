/*
    Copyright 2016-2025 melonDS team

    This file is part of melonDS.

    melonDS is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.

    melonDS is distributed in the hope that it will be useful, but WITHOUT ANY
    WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
    FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
    details.

    You should have received a copy of the GNU General Public License along
    with melonDS. If not, see http://www.gnu.org/licenses/.
*/

#ifndef HDTEXPACK_H
#define HDTEXPACK_H

#include "types.h"
#include "HDFont.h"

#include <atomic>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace melonDS
{

// HD texture pack repository: content-hash keyed replacement images plus
// artist-facing dumping. The identity scheme and file layout are shared
// with the desktop tooling; filenames are the database:
//   tex1_<W>x<H>_<texhash16>_<palhash16|none|$>_<fmt>.png
//   obj1_<W>x<H>_<tilehash16>_<palhash16|none|$>_<4|8|bmp>.png
//   bg1_8x8_<tilehash16>_<palhash16|none|$>_<4|8>.png
// All methods are intended for use from the render thread only.

struct HDTexPackImage
{
    u32 Width = 0, Height = 0;   // pixel size of the replacement image
    u32 Scale = 1;               // integer factor vs the native asset
    std::vector<u32> RGBA;       // packed r | g<<8 | b<<16 | a<<24, 8-bit channels
};

class HDTexPack
{
public:
    // packDir: TexturePacks/<GAMECODE> root holding textures/ and sprites/.
    // dumpDir: Dump/<GAMECODE> root receiving textures/ and sprites/ dumps.
    HDTexPack(const std::string& packDir, const std::string& dumpDir,
              bool loadEnabled, bool dumpEnabled);

    bool LoadActive() const { return LoadEnabled && (EntryCount > 0 || !FontSet.Empty()); }
    // unique per pack object, for callers that cache image pointers across frames (a new pack
    // can be allocated at the old one's address)
    u64 Id() const { return InstanceId; }
    bool DumpActive() const { return DumpEnabled; }
    u32 Scale() const { return PackScale; }
    bool Has2DEntries() const
    {
        return !SpriteIndex.empty() || !SpriteWildIndex.empty() || !SpriteColorIndex.empty()
            || !BGIndex.empty() || !BGWildIndex.empty() || !FontSet.Empty();
    }
    // the pack's fonts/ folder: HD glyphs for text the game draws at runtime (see HDFont.h)
    HDFontSet* Fonts() { return FontSet.Empty() ? nullptr : &FontSet; }
    bool Has3DEntries() const
    {
        return !TexIndex.empty() || !TexWildIndex.empty();
    }

    // 3D textures. hasPal is false for fmt 7 (direct bitmap). rows > 0 looks up a partial entry
    // (file name ending in _rows<N>), whose texHash covers only the texture's first N rows.
    const HDTexPackImage* LookupTexture(u32 width, u32 height, u64 texHash,
                                        u64 palHash, bool hasPal, u32 fmt, u32 rows = 0) const;
    // Row counts the pack has partial entries for at this size and format, or nullptr. A game
    // that uploads a 256x192 picture into a 256x256 texture leaves the last 64 rows as whatever
    // VRAM held before, so its full-texture hash differs from play to play; a partial entry
    // hashes only the rows the picture fills.
    const std::vector<u32>* PartialRows(u32 width, u32 height, u32 fmt) const;
    // Logs a texture key once (Warn) after every lookup for it failed; see ShouldLogMiss.
    void ReportTextureMiss(u32 width, u32 height, u64 texHash, u64 palHash, bool hasPal, u32 fmt) const;
    // rgb6a5 spans width*scale x height*scale texels; scale > 1 dumps a
    // pre-filtered image that loads back as a scaled entry (scale 1 = plain
    // native-res dump).
    void DumpTexture(u32 width, u32 height, u64 texHash,
                     u64 palHash, bool hasPal, u32 fmt, const u32* rgb6a5,
                     u32 scale = 1);

    // 2D OBJ sprites. bppTag: "4", "8" or "bmp". logMiss=false keeps a miss out of the
    // HDTexPack[Miss] log.
    const HDTexPackImage* LookupSprite(u32 width, u32 height, u64 tileHash,
                                       u64 palHash, bool hasPal, const char* bppTag,
                                       bool logMiss = true) const;
    // Colour-keyed sprites ("obj1_WxH_<colourhash>_rgb_<bpp>.png"): keyed by the colours the
    // sprite shows, not the bytes it is stored as. A game that places a sprite's colours
    // wherever palette memory has room (Lufia's portraits land at a different index shift per
    // scene) changes the tile and palette hashes but not these. Looked up after the byte key
    // misses; see SpriteColorHash.
    bool HasSpriteColorKeys() const { return !SpriteColorIndex.empty(); }
    const HDTexPackImage* LookupSpriteColors(u32 width, u32 height, u64 colorHash, u32 bpp) const;
    // XXH64 over the sprite's decoded RGBA8 words (Pal555ToRGBA8), transparent pixels as 0
    static u64 SpriteColorHash(const u32* rgba8, size_t count);
    // Logs a sprite miss once, like LookupSprite does, for callers that try more keys first
    void ReportSpriteMiss(u32 width, u32 height, u64 tileHash, u64 palHash, bool hasPal,
                          const char* bppTag, u64 colorHash) const;
    // rgba8: assembled sprite pixels, 8-bit channels.
    void DumpSprite(u32 width, u32 height, u64 tileHash,
                    u64 palHash, bool hasPal, const char* bppTag, const u32* rgba8,
                    u32 frame, char screen, int oamSlot, int x, int y);

    // 2D BG tiles, always 8x8, pre-flip. bpp: 4 or 8.
    const HDTexPackImage* LookupBGTile(u64 tileHash, u64 palHash, bool hasPal, u32 bpp) const;
    // rgba8: decoded unflipped tile pixels; x/y are tilemap coordinates.
    void DumpBGTile(u64 tileHash, u64 palHash, bool hasPal, u32 bpp, const u32* rgba8,
                    u32 frame, char screen, int layer, int x, int y);

    static u32 RGB6A5ToRGBA8(u32 texel);
    static u32 RGBA8ToRGB6A5(u32 pixel);

    // One Warn line with lookup hits and misses per kind since the last call, then reset.
    // instances2D: replacement instances the 2D walker emitted for the latest frame.
    void LogStats(size_t instances2D) const;

private:
    void LoadDir(const std::string& dir, const char* kind);
    bool AddEntry(const std::string& path, const std::string& name, const char* kind);
    struct Ref
    {
        std::string Path;
        u32 Width = 0, Height = 0, Scale = 1;   // from the PNG header, checked again on decode
    };
    using Index = std::unordered_map<u64, Ref>;
    using Cache = std::unordered_map<u64, HDTexPackImage>;
    const HDTexPackImage* Find(const Index& exactIndex, const Index& wildIndex,
                               Cache& exactCache, Cache& wildCache,
                               u64 exactKey, u64 wildcardKey) const;
    const HDTexPackImage* Load(const Index& index, Cache& cache, u64 key) const;
    void WriteDumpPNG(const char* subdir, const std::string& name,
                      u32 width, u32 height, const u32* rgba8);
    void AppendManifest(const char* subdir, const std::string& line);

    std::string PackDir, DumpDir;
    u64 InstanceId = 0;
    bool LoadEnabled = false, DumpEnabled = false;
    u32 PackScale = 1;
    u32 EntryCount = 0;

    // Keyed by XXH64 over the canonical key fields; wildcard maps ignore the palette hash.
    // The index (key -> PNG path) is built at startup from file names and headers only. An
    // image is decoded the first time it is looked up and then stays resident for the pack's
    // lifetime: callers keep the returned pointers across the frame, and unordered_map nodes
    // never move, so cache entries are never evicted. A whole-game pack therefore costs only
    // what the game actually shows, not everything in the folder.
    Index TexIndex, TexWildIndex;
    std::unordered_map<u32, std::vector<u32>> TexPartialRows;   // PartialRowsKey -> row counts
    Index SpriteIndex, SpriteWildIndex;
    Index SpriteColorIndex;
    Index BGIndex, BGWildIndex;
    mutable Cache TexEntries, TexWildcard;
    mutable Cache SpriteEntries, SpriteWildcard;
    mutable Cache SpriteColorEntries;
    mutable Cache BGEntries, BGWildcard;
    mutable std::unordered_set<const void*> FailedLoads;   // node addresses of index entries
    mutable std::mutex CacheLock;                           // 2D and 3D look up from different threads
    mutable u32 LoadedCount = 0;
    // lookups and hits since the last LogStats: [0] textures, [1] sprites, [2] BG tiles
    mutable std::atomic<u32> Lookups[3]{}, Hits[3]{};
    // Each distinct texture or sprite key that misses is logged once, by its pack file name, so
    // a scene that stays native shows which keys it asked for. Capped per kind: dialogue text
    // alone produces hundreds of sprite misses. Texture misses are reported by the texcache
    // (ReportTextureMiss) because the filter disk cache shares LookupTexture and misses by design.
    bool ShouldLogMiss(int kind, u64 key) const;
    static constexpr u32 MaxLoggedMisses = 200;
    mutable std::unordered_set<u64> MissKeys[3];
    mutable u32 MissesLogged[3]{};

    HDFontSet FontSet;

    std::unordered_set<u64> DumpedKeys;
    std::unordered_set<u64> LoggedSpriteInstances;
};

}

#endif
