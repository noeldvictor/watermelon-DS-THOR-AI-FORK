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

#include "HDFont.h"
#include "HDTexPack.h"
#include "Platform.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>

#define XXH_STATIC_LINKING_ONLY
#include "xxhash/xxhash.h"

#include "stb/stb_image.h"

namespace melonDS
{

namespace fs = std::filesystem;

namespace
{

constexpr int kAtlasColumns = 32;
constexpr int kAtlasPad = 2;
constexpr size_t kMaxGlyphImages = 8192;

u16 Rd16(const std::vector<u8>& b, size_t o) { return (u16)(b[o] | (b[o + 1] << 8)); }
u32 Rd32(const std::vector<u8>& b, size_t o)
{
    return (u32)b[o] | ((u32)b[o + 1] << 8) | ((u32)b[o + 2] << 16) | ((u32)b[o + 3] << 24);
}

// Signature of the pixels below and right of a glyph's first ink pixel, relative to that
// pixel's value, so it doesn't depend on which palette entries the game drew the text with.
// value(x, y) returns -1 for no ink.
template <typename F>
u32 FirstInkSignature(int x, int y, int v, F value)
{
    static const int offsets[4][2] = { {0, 1}, {0, 2}, {0, 3}, {1, 0} };
    u32 sig = 0;
    for (int k = 0; k < 4; k++)
    {
        int c = value(x + offsets[k][0], y + offsets[k][1]);
        u32 e = (c < 0) ? 0 : (0x10u | ((u32)(c - v) & 0xFu));
        sig |= e << (5 * k);
    }
    return sig;
}

u32 Lerp8(u32 a, u32 b, float t)
{
    u32 out = 0;
    for (int sh = 0; sh < 24; sh += 8)
    {
        float ca = (float)((a >> sh) & 0xFF), cb = (float)((b >> sh) & 0xFF);
        out |= ((u32)std::lround(ca + (cb - ca) * t) & 0xFF) << sh;
    }
    return out;
}

}

void HDFontSet::Load(const std::string& dir, u32 packScale)
{
    std::error_code ec;
    if (!fs::is_directory(fs::u8path(dir), ec))
        return;
    for (auto it = fs::directory_iterator(fs::u8path(dir), ec); it != fs::directory_iterator(); it.increment(ec))
    {
        if (ec) break;
        const fs::path& p = it->path();
        if (p.extension() != ".nftr") continue;
        fs::path png = p; png.replace_extension(".png");
        if (!fs::exists(png, ec)) continue;
        if (!LoadFont(p.u8string(), png.u8string(), packScale))
            Platform::Log(Platform::LogLevel::Warn, "HDFont: skipping %s\n", p.u8string().c_str());
    }
    if (!Fonts.empty())
    {
        size_t glyphs = 0;
        for (const Font& f : Fonts) glyphs += f.Glyphs.size();
        Platform::Log(Platform::LogLevel::Warn, "HDFont: %zu fonts, %zu glyphs from %s\n",
                      Fonts.size(), glyphs, dir.c_str());
    }
}

bool HDFontSet::LoadFont(const std::string& nftrPath, const std::string& pngPath, u32 packScale)
{
    std::ifstream in(fs::u8path(nftrPath), std::ios::binary);
    std::vector<u8> b((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    if (b.size() < 0x30 || memcmp(b.data(), "RTFN", 4) || memcmp(&b[0x10], "FNIF", 4))
        return false;

    const size_t finf = 0x10;
    const u32 pGlyph = Rd32(b, finf + 0x10), pWidth = Rd32(b, finf + 0x14);
    if (pGlyph < 8 || pGlyph + 8 > b.size())
        return false;

    Font f;
    f.Name = fs::u8path(nftrPath).stem().u8string();
    f.CellW = b[pGlyph];
    f.CellH = b[pGlyph + 1];
    const u32 cellSize = Rd16(b, pGlyph + 2);
    f.Bpp = b[pGlyph + 6];
    if (!f.CellW || !f.CellH || !cellSize || (f.Bpp != 1 && f.Bpp != 2 && f.Bpp != 4)
        || (u32)(f.CellW * f.CellH * f.Bpp + 7) / 8 > cellSize)
        return false;
    f.MaxShade = (1 << f.Bpp) - 1;

    const size_t blockEnd = std::min(b.size(), (size_t)(pGlyph - 8) + Rd32(b, pGlyph - 4));
    const size_t data = pGlyph + 8;
    const size_t count = blockEnd > data ? (blockEnd - data) / cellSize : 0;
    f.Glyphs.resize(count);
    for (size_t i = 0; i < count; i++)
    {
        Glyph& g = f.Glyphs[i];
        g.Shades.resize((size_t)f.CellW * f.CellH);
        const u8* cell = &b[data + i * cellSize];
        for (int px = 0; px < f.CellW * f.CellH; px++)
        {
            u32 v = 0;
            for (int k = 0; k < f.Bpp; k++)
            {
                const int bit = px * f.Bpp + k;
                v = (v << 1) | ((cell[bit >> 3] >> (7 - (bit & 7))) & 1);
            }
            g.Shades[px] = (u8)v;
        }
        g.InkX0 = f.CellW; g.InkY0 = f.CellH;
        bool first = true;
        for (int x = 0; x < f.CellW; x++)
        {
            for (int y = 0; y < f.CellH; y++)
            {
                const u8 s = g.Shades[(size_t)y * f.CellW + x];
                if (!s) continue;
                if (first) { g.FirstX = x; g.FirstY = y; g.FirstShade = s; first = false; }
                g.InkX0 = std::min(g.InkX0, x); g.InkX1 = std::max(g.InkX1, x);
                g.InkY0 = std::min(g.InkY0, y); g.InkY1 = std::max(g.InkY1, y);
                g.InkCount++;
            }
        }
        if (first) { g.InkX0 = 0; g.InkY0 = 0; g.InkX1 = -1; g.InkY1 = -1; }
    }
    (void)pWidth;   // placement comes from the pixels; advances aren't needed

    int w = 0, h = 0, c = 0;
    stbi_uc* grey = stbi_load(pngPath.c_str(), &w, &h, &c, 1);
    if (!grey)
        return false;
    const int slotW = f.CellW + 2 * kAtlasPad, slotH = f.CellH + 2 * kAtlasPad;
    const int rows = (int)((count + kAtlasColumns - 1) / kAtlasColumns);
    const bool fits = w > 0 && w % (kAtlasColumns * slotW) == 0 && rows > 0
        && h == (w / (kAtlasColumns * slotW)) * rows * slotH;
    if (!fits)
    {
        stbi_image_free(grey);
        Platform::Log(Platform::LogLevel::Warn, "HDFont: %s atlas is %dx%d, not a %d-column grid of %dx%d slots\n",
                      pngPath.c_str(), w, h, kAtlasColumns, slotW, slotH);
        return false;
    }
    f.Scale = (u32)(w / (kAtlasColumns * slotW));
    f.AtlasW = (u32)w; f.AtlasH = (u32)h;
    f.Atlas.assign(grey, grey + (size_t)w * h);
    stbi_image_free(grey);
    if (f.Scale != packScale && packScale > 1)
        Platform::Log(Platform::LogLevel::Warn, "HDFont: %s is %ux, pack is %ux\n",
                      f.Name.c_str(), f.Scale, packScale);

    const u32 fontIndex = (u32)Fonts.size();
    for (size_t i = 0; i < count; i++)
    {
        const Glyph& g = f.Glyphs[i];
        // a glyph too small to identify reliably ('.' and friends) is still indexed: the
        // match needs its whole ink box and the ring around it to agree
        if (!g.InkCount) continue;
        auto value = [&](int x, int y) -> int {
            if (x < 0 || y < 0 || x >= f.CellW || y >= f.CellH) return -1;
            const u8 s = g.Shades[(size_t)y * f.CellW + x];
            return s ? s : -1;
        };
        const u32 sig = FirstInkSignature(g.FirstX, g.FirstY, g.FirstShade, value);
        FirstInkIndex[sig].push_back((fontIndex << 16) | (u32)i);
    }
    Fonts.push_back(std::move(f));
    return true;
}

bool HDFontSet::Verify(const Font& f, const Glyph& g, const u16* canvas, int w, int h,
                       int ox, int oy, int base, u16 bg) const
{
    auto isBg = [&](int x, int y) {
        if (x < 0 || y < 0 || x >= w || y >= h) return true;
        const u16 c = canvas[(size_t)y * w + x];
        return c == kEmpty || c == 0 || c == bg;
    };
    if (ox + g.InkX0 < 0 || oy + g.InkY0 < 0 || ox + g.InkX1 >= w || oy + g.InkY1 >= h)
        return false;
    for (int cy = g.InkY0; cy <= g.InkY1; cy++)
    {
        for (int cx = g.InkX0; cx <= g.InkX1; cx++)
        {
            const int x = ox + cx, y = oy + cy;
            const u8 s = g.Shades[(size_t)cy * f.CellW + cx];
            if (!s)
            {
                if (!isBg(x, y)) return false;
                continue;
            }
            if (isBg(x, y) || canvas[(size_t)y * w + x] != base + s) return false;
        }
    }
    // the ring above, below and right of the ink box is background too, so a small glyph
    // ('.', 'l') doesn't match inside a bigger shape; the left side may touch the glyph before
    for (int cx = g.InkX0; cx <= g.InkX1 + 1; cx++)
        if (!isBg(ox + cx, oy + g.InkY0 - 1) || !isBg(ox + cx, oy + g.InkY1 + 1)) return false;
    for (int cy = g.InkY0; cy <= g.InkY1; cy++)
        if (!isBg(ox + g.InkX1 + 1, oy + cy)) return false;
    return true;
}

void HDFontSet::Recognize(const u16* canvas, int w, int h, u16 bg, std::vector<Placement>& out) const
{
    if (Fonts.empty() || w <= 0 || h <= 0)
        return;
    auto inkValue = [&](int x, int y) -> int {
        if (x < 0 || y < 0 || x >= w || y >= h) return -1;
        const u16 c = canvas[(size_t)y * w + x];
        return (c == kEmpty || c == 0 || c == bg) ? -1 : (int)c;
    };
    std::vector<u8> explained((size_t)w * h, 0);
    for (int x = 0; x < w; x++)
    {
        for (int y = 0; y < h; y++)
        {
            const int v = inkValue(x, y);
            if (v < 0 || explained[(size_t)y * w + x]) continue;
            auto cands = FirstInkIndex.find(FirstInkSignature(x, y, v, inkValue));
            if (cands == FirstInkIndex.end()) continue;

            const Font* bestFont = nullptr;
            const Glyph* best = nullptr;
            Placement bestPlacement{};
            for (u32 id : cands->second)
            {
                const Font& f = Fonts[id >> 16];
                const Glyph& g = f.Glyphs[id & 0xFFFF];
                const int base = v - g.FirstShade;
                if (base < 0 || (best && g.InkCount <= best->InkCount)) continue;
                const int ox = x - g.FirstX, oy = y - g.FirstY;
                if (!Verify(f, g, canvas, w, h, ox, oy, base, bg)) continue;
                best = &g; bestFont = &f;
                bestPlacement = { (u16)(id >> 16), (u16)(id & 0xFFFF), (s16)ox, (s16)oy, (u16)base };
            }
            if (!best) continue;
            for (int cy = best->InkY0; cy <= best->InkY1; cy++)
                for (int cx = best->InkX0; cx <= best->InkX1; cx++)
                    if (best->Shades[(size_t)cy * bestFont->CellW + cx])
                        explained[(size_t)(bestPlacement.Y + cy) * w + bestPlacement.X + cx] = 1;
            out.push_back(bestPlacement);
        }
    }
}

void HDFontSet::GlyphBox(const Placement& p, int& x0, int& y0, int& w, int& h) const
{
    const Glyph& g = Fonts[p.Font].Glyphs[p.Glyph];
    if (!g.InkCount) { x0 = y0 = w = h = 0; return; }
    x0 = g.InkX0 - 1; y0 = g.InkY0 - 1;
    w = g.InkX1 - g.InkX0 + 3; h = g.InkY1 - g.InkY0 + 3;
}

const HDTexPackImage* HDFontSet::GlyphImage(const Placement& p, const u32* shadeRGBA,
                                            bool opaqueBg, u32 bgRGBA)
{
    const Font& f = Fonts[p.Font];
    struct Key { u16 Font, Glyph; u32 Opaque, Bg; u32 Colors[16]; } key{};
    key.Font = p.Font; key.Glyph = p.Glyph;
    key.Opaque = opaqueBg ? 1 : 0; key.Bg = opaqueBg ? bgRGBA & 0xFFFFFF : 0;
    for (int s = 1; s <= f.MaxShade; s++) key.Colors[s] = shadeRGBA[s] & 0xFFFFFF;
    const u64 hash = XXH64(&key, sizeof(key), 0);

    std::lock_guard<std::mutex> lock(ImageLock);
    auto it = Images.find(hash);
    if (it != Images.end()) return &it->second;
    if (Images.size() >= kMaxGlyphImages) return nullptr;

    int x0, y0, bw, bh;
    GlyphBox(p, x0, y0, bw, bh);
    if (!bw || !bh) return nullptr;
    const u32 S = f.Scale;
    const int slotW = f.CellW + 2 * kAtlasPad, slotH = f.CellH + 2 * kAtlasPad;
    const u32 ax = (u32)(((p.Glyph % kAtlasColumns) * slotW + kAtlasPad + x0)) * S;
    const u32 ay = (u32)(((p.Glyph / kAtlasColumns) * slotH + kAtlasPad + y0)) * S;

    HDTexPackImage& img = Images[hash];
    img.Width = (u32)bw * S; img.Height = (u32)bh * S; img.Scale = S;
    img.RGBA.resize((size_t)img.Width * img.Height);
    for (u32 v = 0; v < img.Height; v++)
    {
        for (u32 u = 0; u < img.Width; u++)
        {
            const u8 grey = f.Atlas[(size_t)(ay + v) * f.AtlasW + ax + u];
            const float t = grey * (float)f.MaxShade / 255.0f;
            u32 color;
            float alpha;
            if (t < 1.0f)
            {
                // below the first shade the grey is coverage of that shade's colour
                color = shadeRGBA[1] & 0xFFFFFF;
                alpha = grey < 4 ? 0.0f : t;
            }
            else
            {
                const int k = std::min((int)t, f.MaxShade);
                const int k1 = std::min(k + 1, f.MaxShade);
                color = Lerp8(shadeRGBA[k], shadeRGBA[k1], t - (float)k);
                alpha = 1.0f;
            }
            if (opaqueBg)
            {
                color = Lerp8(bgRGBA, color, alpha);
                alpha = 1.0f;
            }
            img.RGBA[(size_t)v * img.Width + u] = color | ((u32)std::lround(alpha * 255.0f) << 24);
        }
    }
    return &img;
}

}
