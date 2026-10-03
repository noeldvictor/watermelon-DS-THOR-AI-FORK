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

#ifndef GPU2D_HDPACK_H
#define GPU2D_HDPACK_H

#include "types.h"
#include "HDFont.h"

#include <memory>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace melonDS
{

class GPU;
class HDTexPack;
struct HDTexPackImage;

// One replaced 2D asset occurrence for the current frame: an OBJ sprite or a
// text BG tile whose content hash matched a pack entry. The presenter
// composites Image over the packed plane pixels the producer won, so
// occlusion, priority and blending stay with the original composition.
struct HDPack2DInstance
{
    const HDTexPackImage* Image;
    u8 Engine;       // 0 = engine A, 1 = engine B, 2 = carried from the frame before
    u8 Screen = 0;   // 0 top, 1 bottom: where the engine was while this frame was drawn
    u8 RequireMask;  // packed flag bits that must be set (0x90 OBJ, 1<<n BG n)
    u8 RejectMask;   // packed flag bits that must be clear (0x90 for BG tiles)
    u8 Flip;         // bit 0 horizontal, bit 1 vertical
    s16 X, Y;        // native screen coordinates, may be negative
    u16 W, H;        // native pixel size
    // sprites: the sprite's place in the hardware drawing order (see HDPack2D::ObjRank);
    // kNoObjRank for BG tiles and glyphs, which don't use the ownership map
    u8 Rank = 0xFF;
    // the instance also owns ranks Rank+1..Rank+RankSpan: an outlined glyph across two text
    // sprites redraws both sprites' outline pixels
    u8 RankSpan = 0;
    // how much of the sprite's own colour reaches the screen, in 16ths: 16 normally, less
    // while it is blended or faded. Below 16 the composed pixels stay (they carry the
    // effect) and the renderer swaps the sprite's share of them for the art's.
    u8 BlendWeight = 16;
    // the sprite's own pixels before any effect (W*H, source orientation, RGBA8 with alpha
    // 0 where transparent), only while BlendWeight is below 16: with them the renderer
    // turns the composed colour into the art's at the effect's strength. Without them it
    // can only add the art's detail over the native colours, which shows a redraw that
    // moved features as the native picture until the fade ends.
    std::shared_ptr<const std::vector<u32>> Native;
    // a glyph of text drawn into a BG layer: every pixel its layer won there is the text's
    // (the layer's other pixels around it are the box), so the renderer blends the HD glyph
    // over the box colour next to it and lets the glyph's soft edge spill onto the box
    bool Text = false;
    // a BG tile of a layer that takes part in a blend or fade (a target of BLDCNT): where the
    // 2D composite mixed it on the CPU (see kHDBlendInfoOffset) the overlay can't tell its
    // pixels, so the renderer swaps its share of the mixed colour for the art's. Native
    // holds the tile's own pixels for that.
    bool BlendBG = false;
};

constexpr u8 kNoObjRank = 0xFF;
constexpr size_t kObjRankEngineSize = 256 * 192;
// ownership map slots: engine A, engine B, and the engine whose sprites were carried
// over from the frame before (see HDPack2D::CarryAlternatingScreen)
constexpr size_t kObjRankSlots = 3;

// The blend record (GPU2D::Unit::HDBlendInfo) follows the ownership map in the same buffer:
// one u32 per native pixel per engine (A, then B). Bit 31 is set where the 2D composite mixed
// the pixel on the CPU; bits 24-25 say how (1 alpha blend, 2 brightness fade); bits 0-2 are
// the first target's layer (0-3 BG, 4 OBJ, 5 backdrop), bits 3-5 the second's (7 none);
// bits 8-12 and 16-20 their weights in 16ths.
constexpr size_t kHDBlendInfoOffset = kObjRankSlots * kObjRankEngineSize;
constexpr size_t kHDRankBufferBytes = kHDBlendInfoOffset + 2 * kObjRankEngineSize * sizeof(u32);

// CPU-side 2D asset walker: decodes active OBJ sprites and text BG tiles
// straight from OAM/VRAM after a frame has been rendered, dumping them
// through HDTexPack at a throttled cadence and building the per-frame
// replacement instance list. Identity hashing matches the desktop dumper
// (chained XXH64 over the encoded guest bytes plus a used-range palette
// hash) so packs are interchangeable between platforms.
//
// v1 limits (frame-level walker by design):
//  - replacement skips rotscale sprites and non-text BG layers
//  - OAM, scroll and palette state are sampled once at end of frame; games
//    that rewrite BG scroll in HBlank or use mosaic get replacements
//    positioned from the final register state, which can misplace them on
//    such frames (they are not detected and do not fall back)
//  - overlapping sprites all carry the generic OBJ producer mask, so ObjRank
//    records which sprite won each native pixel
class HDPack2D
{
public:
    // call once per emulated frame on the emu thread, at VBlank start (GPU::VBlankStartHook)
    void ProcessFrame(GPU& gpu, HDTexPack* pack);

    std::vector<HDPack2DInstance> Instances;
    // Per engine, per native pixel: the rank (place in the hardware drawing order: OBJ
    // priority bits, then OAM index) of the sprite drawn there, kNoObjRank if none. A
    // replacement is only drawn where its own sprite won, never over a sprite above it,
    // even one the pack has no art for. kObjRankSlots * kObjRankEngineSize bytes.
    std::vector<u8> ObjRank;

private:
    // a sprite the pack had no image for; text is looked for in these (see HDFont.h)
    struct MissedSprite
    {
        s32 X, Y;
        int Width, Height, Type, TileOffset, TileStride, PalOffset;
        u8 Flip;
        u64 TileHash;
        u8 Rank = kNoObjRank;   // the sprite's place in the drawing order (ObjRank)
    };
    struct TextGroup
    {
        s32 X0, Y0;
        u16 Bg;
        std::vector<HDFontSet::Placement> Placements;
        // glyphs drawn over a one-pixel outline, with the outline's palette index
        std::vector<std::pair<HDFontSet::Placement, u16>> Outlined;
        // BG text: each glyph's box index (Placements[i] sits on PlacementBg[i]); empty for
        // sprite text, which has one box per group (Bg)
        std::vector<u16> PlacementBg;
    };

    // a visible tile of a text BG layer the pack had no image for (screen tile column/row
    // in the walk and the tilemap entry)
    struct MissedTile
    {
        int Col, Row;
        u16 Entry;
        u64 TileHash;
    };

    void WalkSprites(GPU& gpu, int num, HDTexPack* pack, bool dump, bool load);
    void ReplaceText(GPU& gpu, int num, HDTexPack* pack, size_t spriteStart);
    // text a game draws into a BG layer (NitroSystem's character canvas: Spirit Tracks'
    // message boxes): the layer's missed tiles are assembled into one screen canvas and
    // read like sprite text
    void ReplaceBGText(HDTexPack* pack, int num, int layer, const std::vector<MissedTile>& tiles,
                       const u8* bgvram, u32 bgvrammask, u32 tilesetaddr, bool eightbpp,
                       const u16* pal, const u16* extPal, int fineX, int fineY);
    // glyphs, outlines and the box index of TextCanvas (w x h) into group; the box is an index
    // covering 40% of boxBasis pixels (0: the canvas area)
    void RecognizeCanvas(const HDFontSet& fonts, int w, int h, TextGroup& group, u32 boxBasis = 0);
    // BG text: a cluster can hold several boxes with different fills (a bubble, buttons, a
    // border), so each common index is tried as the box in turn
    void RecognizeBGCanvas(const HDFontSet& fonts, int w, int h, u32 filled, TextGroup& group);
    void WalkBGLayers(GPU& gpu, int num, HDTexPack* pack, bool dump, bool load);
    void EmitSpriteInstance(const HDTexPackImage* img, int num, u8 flip,
                            s32 xpos, s32 ypos, int width, int height, u8 rank, u8 blendWeight,
                            std::shared_ptr<const std::vector<u32>> native = nullptr);
    // a sprite's pixels as RGBA8 (the texture dump's format), unflipped
    static void DecodeSprite(const u8* objvram, u32 objvrammask, const u16* stdPal, const u16* extPal,
                             int type, int tileOffset, int tileStride, int palOffset,
                             int width, int height, std::vector<u32>& out);
    void CarryAlternatingScreen(bool engineAOnTop, bool prevValid);

    // the previous frame's own sprite replacements and ownership, for CarryAlternatingScreen
    std::vector<HDPack2DInstance> PrevInstances;
    // native pixels of BG tiles in a blend (HDPack2DInstance::Native), by tile and palette hash
    std::unordered_map<u64, std::shared_ptr<const std::vector<u32>>> BGNativeCache;
    std::vector<u8> PrevObjRank;
    bool PrevEngineAOnTop = false;
    bool PrevValid = false;

    u32 FrameCounter = 0;
    u32 NextTextLogFrame = 0;   // the text logs' rate limit
    u32 WalkBatch = 0;

    // bitmap sprites can alias volatile display-capture VRAM; only dump
    // content whose hash survives across two sampled walks
    std::unordered_set<u64> PrevBitmapKeys, CurBitmapKeys;
    // the same for the miss log: bitmaps looked up this frame and the frame before, so a
    // bitmap streamed anew every frame (a scrolling sky) never floods it
    std::unordered_set<u64> PrevLookedUpBitmaps, CurLookedUpBitmaps;

    std::vector<u32> PixelScratch;
    // sprite byte key -> image found by its colours (or nullptr), for ColorKeyPack only
    std::unordered_map<u64, const HDTexPackImage*> ColorKeyCache;
    u64 ColorKeyPack = 0;   // HDTexPack::Id() of the pack the cache belongs to

    std::vector<MissedSprite> Missed;
    std::vector<MissedTile> MissedTiles;
    // BG tiles (by content) a reading found no glyph in: background art, box fill. They are
    // left out of later canvases, so art is looked at once, not every time it moves.
    std::unordered_set<u64> NoTextTiles;
    std::vector<u16> TextCanvas;
    // recognised glyphs per sprite group, keyed by the group's sprites and positions, so
    // text that stays on screen is matched once
    std::unordered_map<u64, TextGroup> TextGroups;
};

}

#endif
