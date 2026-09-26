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

#include "GPU2D_HDPack.h"

#include "GPU.h"
#include "HDTexPack.h"
#include "NDS.h"

#include <algorithm>

#define XXH_STATIC_LINKING_ONLY
#include "xxhash/xxhash.h"

namespace melonDS
{

namespace
{

constexpr size_t kMaxInstances = 4096;
constexpr size_t kMaxTextGroups = 512;
constexpr u32 kSpriteDumpInterval = 8;
constexpr u32 kBGDumpInterval = 16;

// chained XXH64 over a VRAM byte range, wrap-masked like VRAMRead8
u64 HashVRAMRange(const u8* vram, u32 vrammask, u64 hash, u32 addr, u32 size)
{
    addr &= vrammask;
    if (addr + size > vrammask + 1)
    {
        u32 first = vrammask + 1 - addr;
        hash = XXH64(&vram[addr], first, hash);
        return XXH64(&vram[0], size - first, hash);
    }
    return XXH64(&vram[addr], size, hash);
}

// software-renderer parity: 5-bit channels expand through the 6-bit DS space
// (v5*2, palette bit 15 ignored) and quantize as round(v6*255/63)
u32 Expand6To8(u32 v6)
{
    return (v6 * 510 + 63) / 126;
}

u32 Pal555ToRGBA8(u16 entry, bool opaque)
{
    u32 r6 = (entry & 0x1F) << 1;
    u32 g6 = ((entry >> 5) & 0x1F) << 1;
    u32 b6 = ((entry >> 10) & 0x1F) << 1;
    return Expand6To8(r6) | (Expand6To8(g6) << 8) | (Expand6To8(b6) << 16)
        | (opaque ? 0xFF000000u : 0u);
}

u32 Bitmap555ToRGBA8(u16 col)
{
    u32 r6 = (col << 1) & 0x3E;
    u32 g6 = (col >> 4) & 0x3E;
    u32 b6 = (col >> 9) & 0x3E;
    return Expand6To8(r6) | (Expand6To8(g6) << 8) | (Expand6To8(b6) << 16)
        | ((col & 0x8000) ? 0xFF000000u : 0u);
}

// chained XXH64 over the exact OBJ VRAM bytes the sprite decodes from,
// pre-flip; identical to the desktop dumper's SpriteTileHash
u64 SpriteTileHash(const u8* vram, u32 vrammask, int type,
                   int tileOffset, int tileStride, int w, int h)
{
    u64 hash = 0;
    if (type == 2)
    {
        // direct-color bitmap: rows of w*2 bytes at TileStride pitch
        for (int y = 0; y < h; y++)
            hash = HashVRAMRange(vram, vrammask, hash,
                                 (u32)(tileOffset + y * tileStride), (u32)w * 2);
    }
    else
    {
        const u32 tileBytes = (type == 0) ? 32 : 64;
        for (int ty = 0; ty < h / 8; ty++)
            for (int tx = 0; tx < w / 8; tx++)
                hash = HashVRAMRange(vram, vrammask, hash,
                                     (u32)(tileOffset + tx * (int)tileBytes + ty * tileStride),
                                     tileBytes);
    }
    return hash;
}

// Marks the native pixels a sprite draws with its rank, keeping the lowest (topmost) rank
// where sprites overlap. Affine sprites cover their whole bounding box: their pixels would
// need the matrix, and they are never replaced, only possibly on top of something that is.
void MarkSpriteCoverage(u8* rankMap, u8 rank, const u8* vram, u32 vrammask, int type,
                        int tileOffset, int tileStride, s32 xpos, s32 ypos, int width,
                        int height, bool affine, s32 boundWidth, s32 boundHeight,
                        bool hflip, bool vflip)
{
    if (rank == kNoObjRank)
        return;
    const int w = affine ? boundWidth : width;
    const int h = affine ? boundHeight : height;
    for (int y = 0; y < h; y++)
    {
        const s32 sy = (ypos + y) & 0xFF;
        if (sy >= 192)
            continue;
        u8* row = &rankMap[(size_t)sy * 256];
        const int ty = vflip ? height - 1 - y : y;
        for (int x = 0; x < w; x++)
        {
            const s32 sx = xpos + x;
            if (sx < 0 || sx >= 256 || row[sx] <= rank)
                continue;
            bool opaque = true;
            if (!affine)
            {
                const int tx = hflip ? width - 1 - x : x;
                if (type == 0)
                {
                    u32 addr = (u32)(tileOffset + ((tx >> 3) * 32) + ((ty >> 3) * tileStride)
                                     + ((tx & 0x7) >> 1) + ((ty & 0x7) << 2));
                    u8 byte = vram[addr & vrammask];
                    opaque = ((tx & 1) ? (byte >> 4) : (byte & 0xF)) != 0;
                }
                else if (type == 1)
                {
                    u32 addr = (u32)(tileOffset + ((tx >> 3) * 64) + ((ty >> 3) * tileStride)
                                     + (tx & 0x7) + ((ty & 0x7) << 3));
                    opaque = vram[addr & vrammask] != 0;
                }
                else
                {
                    u32 addr = (u32)(tileOffset + (tx * 2) + (ty * tileStride)) & vrammask;
                    opaque = (vram[(addr + 1) & vrammask] & 0x80) != 0;
                }
            }
            if (opaque)
                row[sx] = rank;
        }
    }
}

// hash of the palette range the sprite can address; matches the desktop
// dumper's SpritePalHash
u64 SpritePalHash(const u16* stdPal, const u16* extPal, int type, int palOffset, bool& hasPal)
{
    hasPal = (type != 2);
    if (!hasPal)
        return 0;

    if (type == 0)
    {
        int start = palOffset & 0xF0;
        return XXH64(&stdPal[start], 16 * 2, 0);
    }

    if (palOffset == 0)
        return XXH64(stdPal, 256 * 2, 0);
    return XXH64(&extPal[(palOffset - 1) * 256], 256 * 2, 0);
}

}

void HDPack2D::ProcessFrame(GPU& gpu, HDTexPack* pack)
{
    Instances.clear();
    const bool prevValid = PrevValid;
    PrevValid = false;
    if (!pack)
        return;

    const bool load = pack->LoadActive() && pack->Has2DEntries();
    const bool dumpEnabled = pack->DumpActive();
    if (!load && !dumpEnabled)
        return;

    FrameCounter++;
    const bool dumpSprites = dumpEnabled && (FrameCounter % kSpriteDumpInterval) == 0;
    const bool dumpBG = dumpEnabled && (FrameCounter % kBGDumpInterval) == 0;
    if (!load && !dumpSprites && !dumpBG)
        return;

    if (dumpSprites || dumpBG)
        WalkBatch++;
    if (dumpSprites)
        CurBitmapKeys.clear();
    ObjRank.assign(kObjRankSlots * kObjRankEngineSize, kNoObjRank);

    for (int num = 0; num < 2; num++)
    {
        const GPU2D::Unit& unit = num ? (const GPU2D::Unit&)gpu.GPU2D_B : gpu.GPU2D_A;
        if (unit.RenderDisplayMode() != 1)
            continue;

        if (load || dumpSprites)
            WalkSprites(gpu, num, pack, dumpSprites, load);
        if (load || dumpBG)
            WalkBGLayers(gpu, num, pack, dumpBG, load);
    }

    // the screens the engines drew this frame on: POWCNT as it was while the frame was drawn
    // (the game may flip it in its VBlank handler, before the frame is presented)
    const bool engineAOnTop = (gpu.NDS.PowerControl9 & (1u << 15)) != 0;
    for (HDPack2DInstance& inst : Instances)
        inst.Screen = ((inst.Engine == 0) == engineAOnTop) ? 0 : 1;
    if (load)
        CarryAlternatingScreen(engineAOnTop, prevValid);

    if (dumpSprites)
    {
        std::swap(PrevBitmapKeys, CurBitmapKeys);
        CurBitmapKeys.clear();
    }
    if (load)
    {
        std::swap(PrevLookedUpBitmaps, CurLookedUpBitmaps);
        CurLookedUpBitmaps.clear();
    }
}

// Dual-screen 3D scenes flip the screen swap every frame: engine A draws each screen live
// every other frame and the frame in between shows a capture of it, by which time its
// sprites have left OAM (Lufia's portraits over the island). That capture is the previous
// frame's picture, so the previous frame's sprite replacements (and glyphs) still fit it:
// they are carried to a screen with none of its own this frame, with their ownership in
// slot 2. They only land where this frame's planes still show sprites at those pixels.
// Without this the portrait was HD on one frame and native on the next.
void HDPack2D::CarryAlternatingScreen(bool engineAOnTop, bool prevValid)
{
    std::vector<HDPack2DInstance> carried;
    if (prevValid && PrevEngineAOnTop != engineAOnTop)
    {
        // per screen and engine: the other engine's own sprites on that screen (a HUD gauge,
        // an icon) must not stop this engine's sprites being carried, or a portrait over the
        // alternating 3D fell back to native every other frame once the HUD was replaced too
        bool hasSprites[2][2] = { { false, false }, { false, false } };
        for (const HDPack2DInstance& inst : Instances)
            if (inst.RequireMask == 0x90 && inst.Engine < 2)
                hasSprites[inst.Screen & 1][inst.Engine] = true;
        // one carry slot, so the sprites of one engine
        int source = -1;
        for (const HDPack2DInstance& prev : PrevInstances)
        {
            if (prev.RequireMask != 0x90 || prev.Engine >= 2 || hasSprites[prev.Screen & 1][prev.Engine])
                continue;
            if (source < 0)
                source = prev.Engine;
            if (prev.Engine != source)
                continue;
            HDPack2DInstance inst = prev;
            inst.Engine = 2;
            carried.push_back(inst);
        }
        if (source >= 0)
            std::copy_n(&PrevObjRank[(size_t)source * kObjRankEngineSize], kObjRankEngineSize,
                        &ObjRank[2 * kObjRankEngineSize]);
    }
    // this frame's own replacements only, so carries don't chain
    PrevInstances = Instances;
    PrevObjRank.assign(ObjRank.begin(), ObjRank.begin() + 2 * kObjRankEngineSize);
    PrevEngineAOnTop = engineAOnTop;
    PrevValid = true;
    for (const HDPack2DInstance& inst : carried)
    {
        if (Instances.size() >= kMaxInstances)
            break;
        Instances.push_back(inst);
    }
}

void HDPack2D::DecodeSprite(const u8* objvram, u32 objvrammask, const u16* stdPal, const u16* extPal,
                            int type, int tileOffset, int tileStride, int palOffset,
                            int width, int height, std::vector<u32>& out)
{
    out.resize((size_t)width * height);
    for (int y = 0; y < height; y++)
    {
        for (int x = 0; x < width; x++)
        {
            u32 pixel;
            if (type == 0)
            {
                u32 addr = (u32)(tileOffset + ((x >> 3) * 32) + ((y >> 3) * tileStride)
                                 + ((x & 0x7) >> 1) + ((y & 0x7) << 2));
                u8 byte = objvram[addr & objvrammask];
                int col = (x & 1) ? (byte >> 4) : (byte & 0xF);
                col += palOffset;
                pixel = Pal555ToRGBA8(stdPal[col], (col & 0xF) != 0);
            }
            else if (type == 1)
            {
                u32 addr = (u32)(tileOffset + ((x >> 3) * 64) + ((y >> 3) * tileStride)
                                 + (x & 0x7) + ((y & 0x7) << 3));
                u8 col = objvram[addr & objvrammask];
                u16 entry = (palOffset == 0)
                    ? stdPal[col]
                    : extPal[(palOffset - 1) * 256 + col];
                pixel = Pal555ToRGBA8(entry, col != 0);
            }
            else
            {
                u32 addr = (u32)(tileOffset + (x * 2) + (y * tileStride)) & objvrammask;
                u16 col = (u16)(objvram[addr] | (objvram[(addr + 1) & objvrammask] << 8));
                pixel = Bitmap555ToRGBA8(col);
            }
            out[(size_t)y * width + x] = pixel;
        }
    }
}

void HDPack2D::EmitSpriteInstance(const HDTexPackImage* img, int num, u8 flip,
                                  s32 xpos, s32 ypos, int width, int height, u8 rank, u8 blendWeight,
                                  std::shared_ptr<const std::vector<u32>> native)
{
    // the renderer draws sprite row r at scanline (ypos + r) & 0xFF, so a
    // sprite near the bottom edge also wraps to the top of the screen
    s32 baseY = ypos & 0xFF;
    const s32 candidates[2] = { baseY, baseY - 256 };
    for (s32 y : candidates)
    {
        if (y + height <= 0 || y >= 192)
            continue;
        if (Instances.size() >= kMaxInstances)
            return;
        HDPack2DInstance inst;
        inst.Image = img;
        inst.Engine = (u8)num;
        inst.RequireMask = 0x90;
        inst.RejectMask = 0;
        inst.Flip = flip;
        inst.X = (s16)xpos;
        inst.Y = (s16)y;
        inst.W = (u16)width;
        inst.H = (u16)height;
        inst.Rank = rank;
        inst.BlendWeight = blendWeight;
        inst.Native = native;
        Instances.push_back(inst);
    }
}

void HDPack2D::WalkSprites(GPU& gpu, int num, HDTexPack* pack, bool dump, bool load)
{
    const GPU2D::Unit& unit = num ? (const GPU2D::Unit&)gpu.GPU2D_B : gpu.GPU2D_A;
    if (!(unit.DispCnt & 0x1000))
        return;

    const u16* oam = (const u16*)&gpu.OAM[num ? 0x400 : 0];
    u8* objvram;
    u32 objvrammask;
    unit.GetOBJVRAM(objvram, objvrammask);
    const u16* stdPal = (const u16*)&gpu.Palette[num ? 0x600 : 0x200];
    const u16* extPal = const_cast<GPU2D::Unit&>(unit).GetOBJExtPal();
    const char screen = num ? 'B' : 'A';

    // The replacement overlay draws art as-is; it doesn't reproduce the colour special
    // effects. While an effect applies to the OBJ layer (a fade or blend in progress) the
    // native sprites stay on screen and carry it, and the renderer adds only the art's
    // detail at the share of the sprite's colour that reaches the screen. Drawing the art
    // as-is showed a fading layer at full strength: Lufia fades its opaque white logo
    // sheet out over the logo this way.
    const u32 effect = (unit.BlendCnt >> 6) & 0x3;
    const bool objEffect = (unit.BlendCnt & 0x10) && effect != 0
        && !(effect == 1 && unit.EVA >= 16 && unit.EVB == 0)
        && !(effect >= 2 && unit.EVY == 0);
    // an alpha blend at EVA 16 with EVB above 0 still adds the second target: not as-is
    const u8 alphaWeight = (u8)((unit.EVA >= 16 && unit.EVB > 0) ? 15 : std::min<u32>(16, unit.EVA));
    u8 objEffectWeight = 16;
    if (objEffect)
        objEffectWeight = effect == 1 ? alphaWeight : (u8)(16 - std::min<u32>(16, unit.EVY));

    const size_t spriteStart = Instances.size();
    HDFontSet* fonts = load ? pack->Fonts() : nullptr;
    Missed.clear();

    static const u8 spritewidth[16] =
    {
        8, 16, 8, 8,
        16, 32, 8, 8,
        32, 32, 16, 8,
        64, 64, 32, 8
    };
    static const u8 spriteheight[16] =
    {
        8, 8, 16, 8,
        16, 8, 32, 8,
        32, 16, 32, 8,
        64, 32, 64, 8
    };

    // the drawing order: lower OBJ priority value first, then lower OAM index. Window
    // sprites draw nothing and don't get a rank.
    u8 rankOf[128];
    {
        u16 order[128];
        int n = 0;
        for (int i = 0; i < 128; i++)
        {
            const u16* attrib = &oam[i * 4];
            rankOf[i] = kNoObjRank;
            if (((attrib[0] >> 8) & 0x3) == 2 || ((attrib[0] >> 10) & 0x3) == 2)
                continue;
            order[n++] = (u16)((((attrib[2] >> 10) & 0x3) << 7) | i);
        }
        std::sort(order, order + n);
        for (int r = 0; r < n; r++)
            rankOf[order[r] & 0x7F] = (u8)r;
    }
    u8* rankMap = &ObjRank[(size_t)num * kObjRankEngineSize];

    for (int i = 0; i < 128; i++)
    {
        const u16* attrib = &oam[i * 4];

        u32 sprtype = (attrib[0] >> 8) & 0x3;
        if (sprtype == 2) // sprite disabled
            continue;

        u32 sprmode = (attrib[0] >> 10) & 0x3;

        // sign-extend the 9-bit X / 8-bit Y fields without shifting other
        // attribute bits into the sign position (undefined behaviour)
        s32 xpos = (s32)(attrib[1] & 0x1FF) - (s32)((attrib[1] & 0x100) << 1);
        s32 ypos = (s32)(attrib[0] & 0xFF) - (s32)((attrib[0] & 0x80) << 1);

        u32 sizeparam = (attrib[0] >> 14) | ((attrib[1] & 0xC000) >> 12);
        s32 width = spritewidth[sizeparam];
        s32 height = spriteheight[sizeparam];
        s32 boundwidth = width;
        s32 boundheight = height;
        if (sprtype == 3)
        {
            boundwidth <<= 1;
            boundheight <<= 1;
        }

        if (xpos <= -boundwidth)
            continue;
        bool yc0 = ((ypos + boundheight) > 0) && (ypos < 192);
        bool yc1 = (((ypos & 0xFF) + boundheight) > 0) && ((ypos & 0xFF) < 192);
        if (!(yc0 || yc1))
            continue;

        if (sprmode == 3)
        {
            if ((unit.DispCnt & 0x60) == 0x60)
                continue;
            if ((attrib[2] >> 12) == 0)
                continue;
        }

        u32 tilenum = attrib[2] & 0x3FF;
        int type, tileOffset, tileStride, palOffset;
        if (sprmode == 3)
        {
            // direct-color bitmap
            type = 2;
            if (unit.DispCnt & (1 << 6))
            {
                tileOffset = (int)(tilenum << (7 + ((unit.DispCnt >> 22) & 0x1)));
                tileStride = width * 2;
            }
            else if (unit.DispCnt & (1 << 5))
            {
                tileOffset = (int)(((tilenum & 0x01F) << 4) + ((tilenum & 0x3E0) << 7));
                tileStride = 256 * 2;
            }
            else
            {
                tileOffset = (int)(((tilenum & 0x00F) << 4) + ((tilenum & 0x3F0) << 7));
                tileStride = 128 * 2;
            }
            palOffset = 1 + (attrib[2] >> 12);
        }
        else
        {
            if (unit.DispCnt & (1 << 4))
            {
                tileOffset = (int)(tilenum << (5 + ((unit.DispCnt >> 20) & 0x3)));
                tileStride = (width >> 3) * 32;
                if (attrib[0] & (1 << 13))
                    tileStride <<= 1;
            }
            else
            {
                tileOffset = (int)(tilenum << 5);
                tileStride = 32 * 32;
            }

            if (attrib[0] & (1 << 13))
            {
                type = 1;
                palOffset = (unit.DispCnt & (1u << 31)) ? (int)(1 + (attrib[2] >> 12)) : 0;
            }
            else
            {
                type = 0;
                palOffset = (int)((attrib[2] >> 12) << 4);
            }
        }

        if (width <= 0 || height <= 0 || width > 64 || height > 64)
            continue;

        if (sprmode != 2)
            MarkSpriteCoverage(rankMap, rankOf[i], objvram, objvrammask, type, tileOffset,
                               tileStride, xpos, ypos, width, height,
                               (sprtype & 1) != 0, boundwidth, boundheight,
                               (attrib[1] & (1 << 12)) != 0, (attrib[1] & (1 << 13)) != 0);

        u64 tileHash = SpriteTileHash(objvram, objvrammask, type,
                                      tileOffset, tileStride, width, height);
        bool hasPal = false;
        u64 palHash = SpritePalHash(stdPal, extPal, type, palOffset, hasPal);
        const char* bppTag = (type == 0) ? "4" : (type == 1) ? "8" : "bmp";

        if (dump)
        {
            bool stable = true;
            if (type == 2)
            {
                u64 k = tileHash ^ ((u64)width << 32) ^ (u64)height;
                stable = PrevBitmapKeys.count(k) != 0;
                CurBitmapKeys.insert(k);
            }
            if (stable)
            {
                DecodeSprite(objvram, objvrammask, stdPal, extPal, type, tileOffset, tileStride,
                             palOffset, width, height, PixelScratch);
                pack->DumpSprite((u32)width, (u32)height, tileHash, palHash, hasPal, bppTag,
                                 PixelScratch.data(), WalkBatch, screen, i, xpos, ypos);
            }
        }

        // replacement v1 skips rotscale sprites and window OBJs. Semi-transparent OBJs blend
        // at EVA whatever effect BLDCNT selects.
        const u8 blendWeight = sprmode == 1 ? alphaWeight : objEffectWeight;
        if (load && !(sprtype & 1) && sprmode != 2 && blendWeight > 0)
        {
            bool logMiss = true;
            if (type == 2)
            {
                u64 k = tileHash ^ ((u64)width << 32) ^ (u64)height;
                logMiss = PrevLookedUpBitmaps.count(k) != 0;
                CurLookedUpBitmaps.insert(k);
            }
            const bool colorKeys = type != 2 && pack->HasSpriteColorKeys();
            const HDTexPackImage* img = pack->LookupSprite((u32)width, (u32)height, tileHash,
                                                           palHash, hasPal, bppTag, logMiss && !colorKeys);
            if (!img && colorKeys)
            {
                // the byte key missed: try the colours the sprite shows (HDTexPack::SpriteColorHash),
                // decoded once per byte key while the pack stays the same
                if (ColorKeyPack != pack->Id() || ColorKeyCache.size() > 16384)
                {
                    ColorKeyCache.clear();
                    ColorKeyPack = pack->Id();
                }
                const u64 byteKey = XXH64(&palHash, sizeof(palHash),
                                          tileHash ^ ((u64)width << 48) ^ ((u64)height << 40) ^ (u64)type);
                auto cached = ColorKeyCache.find(byteKey);
                if (cached != ColorKeyCache.end())
                    img = cached->second;
                else
                {
                    DecodeSprite(objvram, objvrammask, stdPal, extPal, type, tileOffset, tileStride,
                                 palOffset, width, height, PixelScratch);
                    const u64 colorHash = HDTexPack::SpriteColorHash(PixelScratch.data(), PixelScratch.size());
                    img = pack->LookupSpriteColors((u32)width, (u32)height, colorHash, type == 1 ? 8u : 4u);
                    ColorKeyCache.emplace(byteKey, img);
                    if (!img && logMiss)
                        pack->ReportSpriteMiss((u32)width, (u32)height, tileHash, palHash, hasPal,
                                               bppTag, colorHash);
                }
            }
            u8 flip = (u8)(((attrib[1] & (1 << 12)) ? 1 : 0)
                           | ((attrib[1] & (1 << 13)) ? 2 : 0));
            if (img)
            {
                // under an effect the renderer needs the sprite's own colours to swap them
                // for the art's (see HDPack2DInstance::Native)
                std::shared_ptr<const std::vector<u32>> native;
                if (blendWeight < 16)
                {
                    auto pixels = std::make_shared<std::vector<u32>>();
                    DecodeSprite(objvram, objvrammask, stdPal, extPal, type, tileOffset, tileStride,
                                 palOffset, width, height, *pixels);
                    native = std::move(pixels);
                }
                EmitSpriteInstance(img, num, flip, xpos, ypos, width, height, rankOf[i], blendWeight,
                                   std::move(native));
            }
            else if (fonts && type != 2 && blendWeight == 16)
                Missed.push_back({ xpos, ypos, width, height, type, tileOffset, tileStride,
                                   palOffset, flip, tileHash });
        }
    }

    if (!Missed.empty())
        ReplaceText(gpu, num, pack, spriteStart);
}

void HDPack2D::ReplaceText(GPU& gpu, int num, HDTexPack* pack, size_t spriteStart)
{
    HDFontSet* fonts = pack->Fonts();
    const GPU2D::Unit& unit = num ? (const GPU2D::Unit&)gpu.GPU2D_B : gpu.GPU2D_A;
    u8* objvram;
    u32 objvrammask;
    unit.GetOBJVRAM(objvram, objvrammask);
    const u16* stdPal = (const u16*)&gpu.Palette[num ? 0x600 : 0x200];
    const u16* extPal = const_cast<GPU2D::Unit&>(unit).GetOBJExtPal();

    // Text is drawn into a group of sprites sharing one palette, and a glyph can straddle two
    // of them, so each group is assembled into one screen-space canvas of palette indices
    std::vector<HDPack2DInstance> glyphs;
    std::vector<bool> done(Missed.size(), false);
    for (size_t first = 0; first < Missed.size(); first++)
    {
        if (done[first]) continue;
        const int type = Missed[first].Type, palOffset = Missed[first].PalOffset;
        std::vector<size_t> members;
        u64 groupKey = XXH64(&num, sizeof(num), (u64)type * 1000 + (u64)palOffset);
        s32 x0 = 256, y0 = 192, x1 = 0, y1 = 0;
        for (size_t i = first; i < Missed.size(); i++)
        {
            const MissedSprite& m = Missed[i];
            if (done[i] || m.Type != type || m.PalOffset != palOffset) continue;
            done[i] = true;
            members.push_back(i);
            // rows wrap at 256 like the renderer's scanline test; the canvas only needs the
            // part on screen
            const s32 y = m.Y & 0xFF;
            const s32 top = (y + m.Height > 256) ? 0 : y;
            const s32 bottom = (y + m.Height > 256) ? 192 : y + m.Height;
            x0 = std::min(x0, std::max<s32>(m.X, 0));
            x1 = std::max(x1, std::min<s32>(m.X + m.Width, 256));
            y0 = std::min(y0, std::min<s32>(top, 192));
            y1 = std::max(y1, std::min<s32>(bottom, 192));
            const s32 fields[5] = { m.X, m.Y, m.Width, m.Height, m.Flip };
            groupKey = XXH64(fields, sizeof(fields), groupKey ^ m.TileHash);
        }
        if (x1 <= x0 || y1 <= y0) continue;

        auto cached = TextGroups.find(groupKey);
        if (cached == TextGroups.end())
        {
            if (TextGroups.size() >= kMaxTextGroups)
                TextGroups.clear();
            const int w = x1 - x0, h = y1 - y0;
            TextCanvas.assign((size_t)w * h, HDFontSet::kEmpty);
            // lower OAM slots win where sprites overlap, as on hardware: paint them last
            for (auto it = members.rbegin(); it != members.rend(); ++it)
            {
                const MissedSprite& m = Missed[*it];
                for (int sy = 0; sy < m.Height; sy++)
                {
                    const s32 py = (m.Y + sy) & 0xFF;
                    if (py < y0 || py >= y1) continue;
                    const int ty = (m.Flip & 2) ? m.Height - 1 - sy : sy;
                    for (int sx = 0; sx < m.Width; sx++)
                    {
                        const s32 px = m.X + sx;
                        if (px < x0 || px >= x1) continue;
                        const int tx = (m.Flip & 1) ? m.Width - 1 - sx : sx;
                        u32 col;
                        if (m.Type == 0)
                        {
                            u32 addr = (u32)(m.TileOffset + ((tx >> 3) * 32) + ((ty >> 3) * m.TileStride)
                                             + ((tx & 0x7) >> 1) + ((ty & 0x7) << 2));
                            u8 byte = objvram[addr & objvrammask];
                            col = (tx & 1) ? (byte >> 4) : (byte & 0xF);
                        }
                        else
                        {
                            u32 addr = (u32)(m.TileOffset + ((tx >> 3) * 64) + ((ty >> 3) * m.TileStride)
                                             + (tx & 0x7) + ((ty & 0x7) << 3));
                            col = objvram[addr & objvrammask];
                        }
                        if (col)
                            TextCanvas[(size_t)(py - y0) * w + (px - x0)] = (u16)col;
                    }
                }
            }

            // an opaque text box is filled with one index; ink rarely covers much of the area
            u16 bg = 0;
            {
                std::unordered_map<u16, u32> counts;
                for (u16 v : TextCanvas)
                    if (v != HDFontSet::kEmpty) counts[v]++;
                for (const auto& entry : counts)
                    if (entry.second * 5 > (u32)(w * h) * 2) bg = entry.first;
            }
            TextGroup group{ x0, y0, bg, {} };
            fonts->Recognize(TextCanvas.data(), w, h, bg, group.Placements);
            cached = TextGroups.emplace(groupKey, std::move(group)).first;
        }

        const TextGroup& group = cached->second;
        auto colour = [&](u32 index) -> u32 {
            u16 entry;
            if (type == 0) entry = stdPal[(palOffset + index) & 0xFF];
            else if (palOffset == 0) entry = stdPal[index & 0xFF];
            else entry = extPal[(palOffset - 1) * 256 + (index & 0xFF)];
            return Pal555ToRGBA8(entry, true);
        };
        for (const HDFontSet::Placement& p : group.Placements)
        {
            u32 shades[16] = {};
            const int maxShade = fonts->MaxShade(p);
            for (int s = 1; s <= maxShade; s++)
                shades[s] = colour(p.Base + (u32)s);
            const HDTexPackImage* img = fonts->GlyphImage(p, shades, group.Bg != 0, colour(group.Bg));
            if (!img) continue;
            int bx, by, bw, bh;
            fonts->GlyphBox(p, bx, by, bw, bh);
            HDPack2DInstance inst;
            inst.Image = img;
            inst.Engine = (u8)num;
            inst.RequireMask = 0x90;
            inst.RejectMask = 0;
            inst.Flip = 0;
            inst.X = (s16)(group.X0 + p.X + bx);
            inst.Y = (s16)(group.Y0 + p.Y + by);
            inst.W = (u16)bw;
            inst.H = (u16)bh;
            glyphs.push_back(inst);
        }
    }

    // the presenter draws sprite instances last to first, so glyphs placed ahead of this
    // engine's sprites are drawn over them: text sits on top of what it is printed on
    const size_t room = kMaxInstances > Instances.size() ? kMaxInstances - Instances.size() : 0;
    if (glyphs.size() > room) glyphs.resize(room);
    Instances.insert(Instances.begin() + (std::ptrdiff_t)spriteStart, glyphs.begin(), glyphs.end());
}

void HDPack2D::WalkBGLayers(GPU& gpu, int num, HDTexPack* pack, bool dump, bool load)
{
    const GPU2D::Unit& unit = num ? (const GPU2D::Unit&)gpu.GPU2D_B : gpu.GPU2D_A;
    u32 dispcnt = unit.DispCnt;
    u32 mode = dispcnt & 0x7;
    const char screen = num ? 'B' : 'A';

    u8* bgvram;
    u32 bgvrammask;
    unit.GetBGVRAM(bgvram, bgvrammask);

    for (int layer = 0; layer < 4; layer++)
    {
        if (!(dispcnt & (0x100u << layer)))
            continue;

        // v1 covers text BGs only; the layer type table mirrors
        // DrawScanlineBGMode in the software renderer
        bool text;
        switch (layer)
        {
        case 0: text = (mode != 6) && !(num == 0 && (dispcnt & 0x8)); break;
        case 1: text = (mode != 6); break;
        case 2: text = (mode == 0 || mode == 1 || mode == 3); break;
        default: text = (mode == 0); break;
        }
        if (!text)
            continue;

        u16 bgcnt = unit.BGCnt[layer];
        u32 tilesetaddr, tilemapaddr;
        const u16* pal;
        if (num)
        {
            tilesetaddr = (u32)(bgcnt & 0x003C) << 12;
            tilemapaddr = (u32)(bgcnt & 0x1F00) << 3;
            pal = (const u16*)&gpu.Palette[0x400];
        }
        else
        {
            tilesetaddr = ((dispcnt & 0x07000000) >> 8) + ((u32)(bgcnt & 0x003C) << 12);
            tilemapaddr = ((dispcnt & 0x38000000) >> 11) + ((u32)(bgcnt & 0x1F00) << 3);
            pal = (const u16*)&gpu.Palette[0];
        }

        const bool eightbpp = (bgcnt & 0x0080) != 0;
        const u32 bpp = eightbpp ? 8 : 4;
        const bool extpal = (dispcnt & 0x40000000) != 0;
        const u32 extpalslot = ((layer < 2) && (bgcnt & 0x2000)) ? (u32)(2 + layer) : (u32)layer;
        const u32 widexmask = (bgcnt & 0x4000) ? 0x100 : 0;
        const int mapTilesX = (bgcnt & 0x4000) ? 64 : 32;
        const int mapTilesY = (bgcnt & 0x8000) ? 64 : 32;

        auto mapEntry = [&](int tx, int ty) -> u16
        {
            u32 yoff = (u32)(ty * 8);
            u32 xoff = (u32)(tx * 8);
            u32 addr = tilemapaddr;
            if (bgcnt & 0x8000)
            {
                addr += (yoff & 0x1F8) << 3;
                if (bgcnt & 0x4000)
                    addr += (yoff & 0x100) << 3;
            }
            else
            {
                addr += (yoff & 0xF8) << 3;
            }
            addr += ((xoff & 0xF8) >> 2) + ((xoff & widexmask) << 3);
            addr &= bgvrammask & ~1u;
            return (u16)(bgvram[addr] | (bgvram[addr + 1] << 8));
        };

        auto tilePalette = [&](u16 curtile, u64& palHash) -> const u16*
        {
            if (!eightbpp)
            {
                const u16* curpal = pal + ((curtile & 0xF000) >> 8);
                palHash = XXH64(curpal, 16 * 2, 0);
                return curpal;
            }
            if (extpal)
            {
                const u16* curpal =
                    const_cast<GPU2D::Unit&>(unit).GetBGExtPal(extpalslot, curtile >> 12);
                palHash = XXH64(curpal, 256 * 2, 0);
                return curpal;
            }
            palHash = XXH64(pal, 256 * 2, 0);
            return pal;
        };

        auto tileHashOf = [&](u16 curtile) -> u64
        {
            u32 tilenum = curtile & 0x03FF;
            u32 addr = tilesetaddr + (tilenum << (eightbpp ? 6 : 5));
            return HashVRAMRange(bgvram, bgvrammask, 0, addr, eightbpp ? 64 : 32);
        };

        if (dump)
        {
            PixelScratch.resize(64);
            for (int ty = 0; ty < mapTilesY; ty++)
            {
                for (int tx = 0; tx < mapTilesX; tx++)
                {
                    u16 curtile = mapEntry(tx, ty);
                    u64 palHash = 0;
                    const u16* curpal = tilePalette(curtile, palHash);
                    u64 tileHash = tileHashOf(curtile);

                    u32 tilenum = curtile & 0x03FF;
                    u32 base = tilesetaddr + (tilenum << (eightbpp ? 6 : 5));
                    for (int y = 0; y < 8; y++)
                    {
                        for (int x = 0; x < 8; x++)
                        {
                            int col;
                            if (eightbpp)
                            {
                                col = bgvram[(base + (u32)(y * 8 + x)) & bgvrammask];
                            }
                            else
                            {
                                u8 byte = bgvram[(base + (u32)(y * 4 + (x >> 1))) & bgvrammask];
                                col = (x & 1) ? (byte >> 4) : (byte & 0xF);
                            }
                            PixelScratch[(size_t)y * 8 + x] =
                                Pal555ToRGBA8(curpal[col], col != 0);
                        }
                    }
                    pack->DumpBGTile(tileHash, palHash, true, bpp, PixelScratch.data(),
                                     WalkBatch, screen, layer, tx, ty);
                }
            }
        }

        // as for sprites: while a colour effect targets this layer, keep the native tiles
        const u32 effect = (unit.BlendCnt >> 6) & 0x3;
        const bool layerEffect = (unit.BlendCnt & (1u << layer)) && effect != 0
            && !(effect == 1 && unit.EVA >= 16 && unit.EVB == 0)
            && !(effect >= 2 && unit.EVY == 0);

        if (load && !layerEffect)
        {
            u16 xoff = unit.BGXPos[layer];
            u16 yoff = unit.BGYPos[layer];
            const int fineX = xoff & 7;
            const int fineY = yoff & 7;
            const int cols = fineX ? 33 : 32;
            const int rows = fineY ? 25 : 24;

            for (int r = 0; r < rows; r++)
            {
                for (int c = 0; c < cols; c++)
                {
                    if (Instances.size() >= kMaxInstances)
                        return;

                    int tx = (xoff >> 3) + c;
                    int ty = (yoff >> 3) + r;
                    u16 curtile = mapEntry(tx, ty);

                    u64 palHash = 0;
                    tilePalette(curtile, palHash);
                    u64 tileHash = tileHashOf(curtile);

                    const HDTexPackImage* img = pack->LookupBGTile(tileHash, palHash, true, bpp);
                    if (!img)
                        continue;

                    HDPack2DInstance inst;
                    inst.Image = img;
                    inst.Engine = (u8)num;
                    inst.RequireMask = (u8)(1u << layer);
                    inst.RejectMask = 0x90;
                    inst.Flip = (u8)(((curtile & 0x0400) ? 1 : 0) | ((curtile & 0x0800) ? 2 : 0));
                    inst.X = (s16)(c * 8 - fineX);
                    inst.Y = (s16)(r * 8 - fineY);
                    inst.W = 8;
                    inst.H = 8;
                    Instances.push_back(inst);
                }
            }
        }
    }
}

}
