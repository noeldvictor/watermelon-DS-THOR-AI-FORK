/*
    HD model replacement for the geometry engine.

    A model shape reaches the GX FIFO as a display list the SDK DMAs byte for byte from the model
    file, so a pack can key a replacement by the XXH64 of those bytes and their size (the
    `mdl1_<size>_<hash>` names tools/hd_remaster/models3d.py gives shapes). A replacement is a
    display list too: the same commands (matrix-stack restores, normals or colours, texture
    coordinates, vertices) for a finer mesh, so it goes through this engine's own transform,
    lighting, clipping and viewport code and comes out as ordinary polygons.

    The original still runs: its commands keep their emulated timing and its polygons still count
    against the hardware limits, so the game sees no difference. Its polygons are only left out
    of the render list. The replacement runs when the original's last command has run, from the
    state saved before its first one, into separate buffers with their own limits, and the live
    state is put back afterwards; it takes no emulated time.
*/

#include "GPU3D.h"

#include <algorithm>
#include <cstring>
#include <type_traits>

#include "NDS.h"
#include "Platform.h"
#include "xxhash/xxhash.h"

namespace melonDS
{
using Platform::Log;
using Platform::LogLevel;

extern const u8 CmdNumParams[256];

void GPU3D::SetModelSource(const HDModelSource* source) noexcept
{
    if (source == ModelSource)
        return;
    ModelSource = source;
    ClearHDModelState();
    if (source)
    {
        HDDraws.resize(HDDrawSlots);
        HDPlaced.assign(HDDrawSlots + 1, 0);
        for (auto& bank : HDVertexRAM)
            bank.resize(HDVertexCapacity);
        for (auto& bank : HDPolygonRAM)
            bank.resize(HDPolygonCapacity);
        RenderPolygonRAM.resize(2048 + HDPolygonCapacity, nullptr);
    }
    else
    {
        // keep what a frame being rendered may still point at until the next flush has run
        RenderNumPolygons = std::min<u32>(RenderNumPolygons, 2048);
    }
}

void GPU3D::ClearHDModelState() noexcept
{
    HDTaggedWordsLeft = 0;
    HDTaggedWriteTag = 0;
    HDExecTag = 0;
    for (auto& draw : HDDraws)
        draw = HDModelDraw{};
    HDNumVertices = 0;
    HDNumPolygons = 0;

    // FIFO entries from a savestate may hold anything where the tag lives
    auto clearTags = [](auto& fifo) {
        auto* entries = fifo.RawEntries();
        for (u32 i = 0; i < std::remove_reference_t<decltype(fifo)>::Capacity; i++)
            entries[i].Tag = 0;
    };
    clearTags(CmdFIFO);
    clearTags(CmdPIPE);
    clearTags(CmdStallQueue);
}

void GPU3D::OnDisplayListDMA(u32 src, u32 bytes) noexcept
{
    if (!ModelSource || bytes == 0 || bytes > 0x100000)
        return;

    const u8* ram = NDS.MainRAM;
    const u32 mask = NDS.MainRAMMask;
    const u32 start = src & mask;
    u64 hash;
    if (start + bytes <= mask + 1)
    {
        hash = XXH64(&ram[start], bytes, 0);
    }
    else
    {
        std::vector<u8> copy(bytes);
        for (u32 i = 0; i < bytes; i++)
            copy[i] = ram[(start + i) & mask];
        hash = XXH64(copy.data(), bytes, 0);
    }

    const std::vector<u32>* replacement = ModelSource->LookupModel(hash, bytes);
    if (!replacement)
    {
        HDTaggedWordsLeft = 0;
        return;
    }

    // tags cycle through the draw slots; a draw lives until its frame is flushed, a few
    // frames at most, so a slot is long free when its tag comes round again
    HDNextTag = (HDNextTag % HDDrawSlots) + 1;
    HDModelDraw& draw = HDDraws[HDNextTag - 1];
    draw = HDModelDraw{};
    draw.Replacement = replacement;
    HDTaggedWriteTag = static_cast<u16>(HDNextTag);
    HDTaggedWordsLeft = bytes / 4;
}

void GPU3D::HDModelTagChange(u16 next) noexcept
{
    // the display list that was running has finished: run its replacement
    if (HDExecTag != 0 && HDExecTag <= HDDraws.size())
    {
        HDModelDraw& done = HDDraws[HDExecTag - 1];
        if (done.Started && !done.Replayed && done.Replacement)
            ReplayHDModel(done);
    }

    HDExecTag = next;
    if (next != 0)
    {
        if (next > HDDraws.size() || !ModelSource)
        {
            HDExecTag = 0;
            return;
        }
        HDModelDraw& draw = HDDraws[next - 1];
        if (!draw.Started)
        {
            draw.Started = true;
            SaveGeometry(draw.Start);
        }
    }
}

void GPU3D::SaveGeometry(GeometrySnapshot& s) const noexcept
{
    memcpy(s.ExecParams, ExecParams, sizeof(ExecParams));
    s.ExecParamCount = ExecParamCount;
    s.CycleCount = CycleCount;
    s.VertexPipeline = VertexPipeline;
    s.NormalPipeline = NormalPipeline;
    s.PolygonPipeline = PolygonPipeline;
    s.VertexSlotCounter = VertexSlotCounter;
    s.VertexSlotsFree = VertexSlotsFree;
    s.MatrixMode = MatrixMode;
    memcpy(s.PosMatrix, PosMatrix, sizeof(PosMatrix));
    memcpy(s.VecMatrix, VecMatrix, sizeof(VecMatrix));
    memcpy(s.ClipMatrix, ClipMatrix, sizeof(ClipMatrix));
    s.ClipMatrixDirty = ClipMatrixDirty;
    s.PolygonMode = PolygonMode;
    memcpy(s.CurVertex, CurVertex, sizeof(CurVertex));
    memcpy(s.VertexColor, VertexColor, sizeof(VertexColor));
    memcpy(s.TexCoords, TexCoords, sizeof(TexCoords));
    memcpy(s.RawTexCoords, RawTexCoords, sizeof(RawTexCoords));
    memcpy(s.Normal, Normal, sizeof(Normal));
    s.PolygonAttr = PolygonAttr;
    s.CurPolygonAttr = CurPolygonAttr;
    s.TexParam = TexParam;
    s.TexPalette = TexPalette;
    memcpy(s.MatDiffuse, MatDiffuse, 3);
    memcpy(s.MatAmbient, MatAmbient, 3);
    memcpy(s.MatSpecular, MatSpecular, 3);
    memcpy(s.MatEmission, MatEmission, 3);
    for (int i = 0; i < 4; i++)
        s.TempVertexBuffer[i] = TempVertexBuffer[i];
    s.VertexNum = VertexNum;
    s.VertexNumInPoly = VertexNumInPoly;
    s.NumConsecutivePolygons = NumConsecutivePolygons;
    s.NumOpaquePolygons = NumOpaquePolygons;
    s.LastStripPolygon = LastStripPolygon;
    s.CurVertexRAM = CurVertexRAM;
    s.CurPolygonRAM = CurPolygonRAM;
    s.NumVertices = NumVertices;
    s.NumPolygons = NumPolygons;
    s.VertexLimit = VertexLimit;
    s.PolygonLimit = PolygonLimit;
    s.GXStat = GXStat;
    s.DispCnt = DispCnt;
}

void GPU3D::LoadGeometry(const GeometrySnapshot& s) noexcept
{
    memcpy(ExecParams, s.ExecParams, sizeof(ExecParams));
    ExecParamCount = s.ExecParamCount;
    CycleCount = s.CycleCount;
    VertexPipeline = s.VertexPipeline;
    NormalPipeline = s.NormalPipeline;
    PolygonPipeline = s.PolygonPipeline;
    VertexSlotCounter = s.VertexSlotCounter;
    VertexSlotsFree = s.VertexSlotsFree;
    MatrixMode = s.MatrixMode;
    memcpy(PosMatrix, s.PosMatrix, sizeof(PosMatrix));
    memcpy(VecMatrix, s.VecMatrix, sizeof(VecMatrix));
    memcpy(ClipMatrix, s.ClipMatrix, sizeof(ClipMatrix));
    ClipMatrixDirty = s.ClipMatrixDirty;
    PolygonMode = s.PolygonMode;
    memcpy(CurVertex, s.CurVertex, sizeof(CurVertex));
    memcpy(VertexColor, s.VertexColor, sizeof(VertexColor));
    memcpy(TexCoords, s.TexCoords, sizeof(TexCoords));
    memcpy(RawTexCoords, s.RawTexCoords, sizeof(RawTexCoords));
    memcpy(Normal, s.Normal, sizeof(Normal));
    PolygonAttr = s.PolygonAttr;
    CurPolygonAttr = s.CurPolygonAttr;
    TexParam = s.TexParam;
    TexPalette = s.TexPalette;
    memcpy(MatDiffuse, s.MatDiffuse, 3);
    memcpy(MatAmbient, s.MatAmbient, 3);
    memcpy(MatSpecular, s.MatSpecular, 3);
    memcpy(MatEmission, s.MatEmission, 3);
    for (int i = 0; i < 4; i++)
        TempVertexBuffer[i] = s.TempVertexBuffer[i];
    VertexNum = s.VertexNum;
    VertexNumInPoly = s.VertexNumInPoly;
    NumConsecutivePolygons = s.NumConsecutivePolygons;
    NumOpaquePolygons = s.NumOpaquePolygons;
    LastStripPolygon = s.LastStripPolygon;
    CurVertexRAM = s.CurVertexRAM;
    CurPolygonRAM = s.CurPolygonRAM;
    NumVertices = s.NumVertices;
    NumPolygons = s.NumPolygons;
    VertexLimit = s.VertexLimit;
    PolygonLimit = s.PolygonLimit;
    GXStat = s.GXStat;
    DispCnt = s.DispCnt;
}

void GPU3D::ReplayHDModel(HDModelDraw& draw) noexcept
{
    draw.Replayed = true;

    GeometrySnapshot live;
    SaveGeometry(live);
    LoadGeometry(draw.Start);

    // into the replacement buffers, with their own limits, as no display list
    CurVertexRAM = HDVertexRAM[HDBank].data();
    CurPolygonRAM = HDPolygonRAM[HDBank].data();
    NumVertices = HDNumVertices;
    NumPolygons = HDNumPolygons;
    VertexLimit = HDVertexCapacity;
    PolygonLimit = HDPolygonCapacity;
    LastStripPolygon = nullptr;
    VertexNum = 0;
    VertexNumInPoly = 0;
    NumConsecutivePolygons = 0;
    ExecParamCount = 0;
    // the replacement's polygons carry the draw's tag too: the render list puts them where the
    // original's polygons were
    const u16 tag = HDExecTag;

    // unpack the replacement like WriteToGXFIFO does and run each command
    const std::vector<u32>& words = *draw.Replacement;
    const u32 before = NumPolygons;
    size_t i = 0;
    while (i < words.size())
    {
        u32 packed = words[i++];
        for (int k = 0; k < 4; k++)
        {
            // a zero byte is a NOP: no parameters, nothing to run
            const u8 command = packed & 0xFF;
            packed >>= 8;
            if (command == 0)
                continue;
            const u32 params = CmdNumParams[command];
            CmdFIFOEntry entry;
            entry._contents = 0;
            entry.Command = command;
            if (params == 0)
            {
                entry.Param = 0;
                ExecuteEntry(entry);
                continue;
            }
            for (u32 p = 0; p < params && i < words.size(); p++)
            {
                entry.Param = words[i++];
                ExecuteEntry(entry);
            }
        }
    }

    HDNumVertices = NumVertices;
    HDNumPolygons = NumPolygons;
    draw.FirstHDPolygon = before;
    draw.NumHDPolygons = NumPolygons - before;
    HDStatPolygons += NumPolygons - before;
    HDStatDraws++;

    HDExecTag = tag;
    LoadGeometry(live);
}

void GPU3D::BuildRenderListWithHDModels() noexcept
{
    // a display list still running at the flush has not been replaced yet: leave it be
    auto replaced = [this](const Polygon& poly) {
        return poly.HDTag != 0 && poly.HDTag <= HDDraws.size() && HDDraws[poly.HDTag - 1].Replayed;
    };

    u32 opaque = 0, total = 0;
    for (u32 i = 0; i < NumPolygons; i++)
    {
        if (replaced(CurPolygonRAM[i]))
            continue;
        total++;
        if (!CurPolygonRAM[i].Translucent)
            opaque++;
    }
    const std::vector<Polygon>& extra = HDPolygonRAM[HDBank];
    for (u32 i = 0; i < HDNumPolygons; i++)
    {
        total++;
        if (!extra[i].Translucent)
            opaque++;
    }

    u32 io = 0, it = opaque;
    auto place = [&](Polygon* poly) {
        if (poly->Translucent)
            RenderPolygonRAM[it++] = poly;
        else
            RenderPolygonRAM[io++] = poly;
    };
    // a replacement takes its original's place in the order (coplanar polygons resolve by order)
    std::fill(HDPlaced.begin(), HDPlaced.end(), 0);
    std::vector<Polygon>& hd = HDPolygonRAM[HDBank];
    for (u32 i = 0; i < NumPolygons; i++)
    {
        Polygon& poly = CurPolygonRAM[i];
        if (!replaced(poly))
        {
            place(&poly);
            continue;
        }
        if (HDPlaced[poly.HDTag])
            continue;
        HDPlaced[poly.HDTag] = 1;
        const HDModelDraw& draw = HDDraws[poly.HDTag - 1];
        for (u32 k = draw.FirstHDPolygon; k < draw.FirstHDPolygon + draw.NumHDPolygons && k < HDNumPolygons; k++)
            place(&hd[k]);
    }
    // replacements whose original made no polygons (all culled or clipped) go last
    for (u32 k = 0; k < HDNumPolygons; k++)
    {
        if (!HDPlaced[hd[k].HDTag])
            place(&hd[k]);
    }

    std::stable_sort(RenderPolygonRAM.begin(),
        RenderPolygonRAM.begin() + ((FlushAttributes & 0x1) ? opaque : total),
        [](Polygon* a, Polygon* b) { return a->SortKey < b->SortKey; });
    RenderNumPolygons = total;

    // Warn, so release builds show whether a pack's models are found at all
    if (++HDStatFrames >= 60)
    {
        Log(LogLevel::Warn, "HDModels[Stats]: %u replaced display lists, %u replacement polygons in 60 frames\n",
            HDStatDraws, HDStatPolygons);
        HDStatFrames = 0;
        HDStatDraws = 0;
        HDStatPolygons = 0;
    }
}
}
