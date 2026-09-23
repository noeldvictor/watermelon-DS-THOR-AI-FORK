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

#ifndef HDFONT_H
#define HDFONT_H

#include "types.h"

#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace melonDS
{

struct HDTexPackImage;

// HD text for games that draw it at runtime from Nitro (NFTR) fonts.
//
// A game renders text into sprite tiles one glyph at a time, so text never exists as a
// fixed image a pack key could match. The font does: a pack's fonts/ folder holds each
// font file the game uses (<name>.nftr, copied from the ROM) and an upscaled shade atlas
// (<name>.png, written by tools/hd_remaster). Glyphs are found in the sprites a pack
// missed by exact pixel match against the font's bitmaps, and each is drawn from the
// atlas, coloured with the palette entries the game drew it with.
//
// Atlas layout: grey levels, black = no ink, white = the font's top shade. Glyph i sits in
// slot (i % 32, i / 32); slots are (cellW + 4) x (cellH + 4) native pixels with the cell
// 2 pixels in, so an edge the upscaler rounds past the cell has room.
class HDFontSet
{
public:
    static constexpr u16 kEmpty = 0xFFFF;   // canvas pixel no sprite of the group drew

    struct Placement
    {
        u16 Font, Glyph;
        s16 X, Y;      // glyph cell origin in canvas coordinates
        u16 Base;      // canvas value = Base + shade for every ink pixel
    };

    // every <name>.nftr with a matching <name>.png atlas; the atlas scale may differ from the
    // pack's (the presenter resamples), which is only logged
    void Load(const std::string& dir, u32 packScale);
    bool Empty() const { return Fonts.empty(); }
    size_t Count() const { return Fonts.size(); }

    // Finds glyphs in a canvas of palette indices (kEmpty where nothing drew). bg is the
    // index of an opaque canvas background, or 0. Glyphs are matched left to right from the
    // leftmost unexplained ink pixel; every ink pixel must equal Base + shade and every
    // empty pixel inside the glyph's ink box must be background.
    void Recognize(const u16* canvas, int w, int h, u16 bg, std::vector<Placement>& out) const;

    // The glyph's ink box widened by one pixel on each side, in cell coordinates: the area
    // its HD image covers. W/H 0 for a glyph without ink.
    void GlyphBox(const Placement& p, int& x0, int& y0, int& w, int& h) const;
    int MaxShade(const Placement& p) const { return Fonts[p.Font].MaxShade; }

    // HD image for a placed glyph: shade s takes colour shadeRGBA[s] (packed r | g<<8 |
    // b<<16), shades in between blend, and coverage below the first shade fades out. With
    // an opaque canvas background the image is composited over bgRGBA and fully opaque.
    // Cached per glyph and colours; nullptr once the cache is full.
    const HDTexPackImage* GlyphImage(const Placement& p, const u32* shadeRGBA,
                                     bool opaqueBg, u32 bgRGBA);

private:
    struct Glyph
    {
        std::vector<u8> Shades;              // CellW * CellH
        int InkX0 = 0, InkY0 = 0, InkX1 = -1, InkY1 = -1;
        int FirstX = 0, FirstY = 0;          // first ink pixel, column-major
        u8 FirstShade = 0;
        u32 InkCount = 0;
    };

    struct Font
    {
        std::string Name;
        int CellW = 0, CellH = 0, Bpp = 0, MaxShade = 0;
        std::vector<Glyph> Glyphs;
        std::vector<u8> Atlas;               // grey, AtlasW * AtlasH
        u32 AtlasW = 0, AtlasH = 0, Scale = 1;
    };

    bool LoadFont(const std::string& nftrPath, const std::string& pngPath, u32 packScale);
    bool Verify(const Font& f, const Glyph& g, const u16* canvas, int w, int h,
                int ox, int oy, int base, u16 bg) const;

    std::vector<Font> Fonts;
    // signature of the pixels around a glyph's first ink pixel -> (font << 16 | glyph)
    std::unordered_map<u32, std::vector<u32>> FirstInkIndex;

    std::mutex ImageLock;
    std::unordered_map<u64, HDTexPackImage> Images;
};

}

#endif
