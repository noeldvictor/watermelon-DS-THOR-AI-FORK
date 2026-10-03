/*
    Copyright 2016-2025 melonDS team

    This file is part of melonDS.

    melonDS is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.

    melonDS is distributed in the hope that it will be useful, but WITHOUT ANY
    WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
    FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

    You should have received a copy of the GNU General Public License along
    with melonDS. If not, see http://www.gnu.org/licenses/.
*/

#ifndef GPU3D_H
#define GPU3D_H

#include <array>
#include <vector>
#include <memory>

#include "Savestate.h"
#include "FIFO.h"
#include "HDModelSource.h"
#include "VulkanPipelineProfile.h"

namespace melonDS
{
class GPU;

struct Vertex
{
    s32 Position[4];
    s32 Color[3];
    s16 TexCoords[2];

    bool Clipped;

    // final vertex attributes.
    // allows them to be reused in polygon strips.

    s32 FinalPosition[2];
    s32 FinalColor[3];

    // hi-res position (4-bit fractional part)
    // TODO maybe: hi-res color? (that survives clipping)
    s32 HiresPosition[2];

    void DoSavestate(Savestate* file) noexcept;
};

struct Polygon
{
    Vertex* Vertices[10];
    u32 NumVertices;

    s32 FinalZ[10];
    s32 FinalW[10];
    bool WBuffer;

    u32 Attr;
    u32 TexParam;
    u32 TexPalette;

    bool Degenerate;

    bool FacingView;
    bool Translucent;

    bool IsShadowMask;
    bool IsShadow;

    int Type; // 0=regular 1=line

    u32 VTop, VBottom; // vertex indices
    s32 YTop, YBottom; // Y coords
    s32 XTop, XBottom; // associated X coords

    u32 SortKey;

    // HD model replacement: the replaced display list that made this polygon (1-based draw tag),
    // 0 for none. Not hardware state, not saved.
    u16 HDTag;

    void DoSavestate(Savestate* file) noexcept;
};

class Renderer3D;
class NDS;

struct CaptureSourceIdentity
{
    bool Valid = false;
    u64 Sequence = 0;
    u32 PolygonCount = 0;
    u32 CaptureCnt = 0;
    bool ScreenSwap = false;
};

class GPU3D
{
public:
    GPU3D(melonDS::NDS& nds, std::unique_ptr<Renderer3D>&& renderer = nullptr) noexcept;
    ~GPU3D() noexcept = default;
    void Reset() noexcept;

    void DoSavestate(Savestate* file) noexcept;

    void SetEnabled(bool geometry, bool rendering) noexcept;

    void ExecuteCommand() noexcept;

    s32 CyclesToRunFor() const noexcept;
    void Run() noexcept;
    void CheckFIFOIRQ() noexcept;
    void CheckFIFODMA() noexcept;

    void VCount144(GPU& gpu) noexcept;
    void VBlank() noexcept;
    void VCount215(GPU& gpu) noexcept;

    void RestartFrame(GPU& gpu) noexcept;
    void Stop(const GPU& gpu) noexcept;

    void SetRenderXPos(u16 xpos) noexcept;
    [[nodiscard]] u16 GetRenderXPos() const noexcept { return RenderXPos; }
    u32* GetLine(int line) noexcept;
    [[nodiscard]] bool GetLastServedCaptureSourceIdentity(
        CaptureSourceIdentity& outIdentity) const noexcept;

    void WriteToGXFIFO(u32 val) noexcept;

    /**
     * HD model replacement (GPU3D_HDModels.cpp). A display list the source replaces is recognised
     * when its DMA into the GX FIFO starts; its commands are tagged on their way through the FIFO,
     * the polygons they make are left out of the render list, and the replacement display list
     * runs instead, from the state the original started with, into separate buffers that count
     * neither against the hardware's polygon/vertex limits nor as emulated time. Only for
     * renderers that take more than 2048 polygons (Vulkan); nullptr turns it off.
     */
    void SetModelSource(const HDModelSource* source) noexcept;
    void OnDisplayListDMA(u32 src, u32 bytes) noexcept;

    /**
     * Free camera (the frontend's right stick): an orbit of the DRAWN picture around what sits at
     * the screen centre. Vertices go through Pos x F x Proj instead of Pos x Proj, F being a
     * view-space turn about the game camera's world-up axis (yaw) and its own x axis (pitch)
     * around that point, plus a dolly (zoom, a fraction of the point's distance, positive =
     * farther). Only vertex submission sees it: BOX_TEST, POS_TEST and the lights keep the game's
     * camera, so what the game culls for its own view stays missing. Perspective projections
     * only (3D drawn with an orthographic projection, like menus, stays put). Called once per
     * frame; all zero turns it off and rendering is identical to no free camera.
     */
    void SetFreeCamera(float yaw, float pitch, float zoom) noexcept;

    [[nodiscard]] bool IsRendererAccelerated() const noexcept;
    [[nodiscard]] Renderer3D& GetCurrentRenderer() noexcept { return *CurrentRenderer; }
    [[nodiscard]] const Renderer3D& GetCurrentRenderer() const noexcept { return *CurrentRenderer; }
    void SetCurrentRenderer(std::unique_ptr<Renderer3D>&& renderer) noexcept;

    u8 Read8(u32 addr) noexcept;
    u16 Read16(u32 addr) noexcept;
    u32 Read32(u32 addr) noexcept;
    void Write8(u32 addr, u8 val) noexcept;
    void Write16(u32 addr, u16 val) noexcept;
    void Write32(u32 addr, u32 val) noexcept;
    void Blit(const GPU& gpu) noexcept;
private:
    melonDS::NDS& NDS;
    typedef union
    {
        u64 _contents;
        struct
        {
            u32 Param;
            u8 Command;
            u8 Unused;
            u16 Tag;    // HD model replacement: draw tag of the display list it came from, 0 = none
        };

    } CmdFIFOEntry;

    // what running a display list can change, saved and put back around a replacement's replay
    struct GeometrySnapshot
    {
        u32 ExecParams[32];
        u32 ExecParamCount;
        s32 CycleCount, VertexPipeline, NormalPipeline, PolygonPipeline, VertexSlotCounter;
        u32 VertexSlotsFree;
        u32 MatrixMode;
        s32 PosMatrix[16], VecMatrix[16], ClipMatrix[16], RenderClipMatrix[16];
        bool ClipMatrixDirty;
        u32 PolygonMode;
        s16 CurVertex[3];
        u8 VertexColor[3];
        s16 TexCoords[2], RawTexCoords[2], Normal[3];
        u32 PolygonAttr, CurPolygonAttr, TexParam, TexPalette;
        u8 MatDiffuse[3], MatAmbient[3], MatSpecular[3], MatEmission[3];
        Vertex TempVertexBuffer[4];
        u32 VertexNum, VertexNumInPoly, NumConsecutivePolygons, NumOpaquePolygons;
        Polygon* LastStripPolygon;
        Vertex* CurVertexRAM;
        Polygon* CurPolygonRAM;
        u32 NumVertices, NumPolygons, VertexLimit, PolygonLimit;
        u32 GXStat, DispCnt;
    };

    struct HDModelDraw
    {
        const std::vector<u32>* Replacement = nullptr;
        bool Started = false;
        bool Replayed = false;
        u32 FirstHDPolygon = 0;     // its polygons in this frame's replacement buffer
        u32 NumHDPolygons = 0;
        GeometrySnapshot Start {};
    };

    void ExecuteEntry(CmdFIFOEntry entry) noexcept;
    void SaveGeometry(GeometrySnapshot& s) const noexcept;
    void LoadGeometry(const GeometrySnapshot& s) noexcept;
    void HDModelTagChange(u16 next) noexcept;
    void ReplayHDModel(HDModelDraw& draw) noexcept;
    void BuildRenderListWithHDModels() noexcept;
    void ClearHDModelState() noexcept;
    void HDTagDraw(const std::vector<u32>* replacement, u32 words) noexcept;
    u16 HDSpeculate(u32 word, bool commandStart) noexcept;

    void UpdateClipMatrix() noexcept;
    void ResetRenderingState() noexcept;
    void AddCycles(s32 num) noexcept;
    void NextVertexSlot() noexcept;
    void StallPolygonPipeline(s32 delay, s32 nonstalldelay) noexcept;
    void SubmitPolygon() noexcept;
    void SubmitVertex() noexcept;
    void CalculateLighting() noexcept;
    void BoxTest(const u32* params) noexcept;
    void PosTest() noexcept;
    void VecTest(u32 param) noexcept;
    void CmdFIFOWrite(const CmdFIFOEntry& entry) noexcept;
    CmdFIFOEntry CmdFIFORead() noexcept;
    void FinishWork(s32 cycles) noexcept;
    void VertexPipelineSubmitCmd() noexcept
    {
        // vertex commands 0x24, 0x25, 0x26, 0x27, 0x28
        if (!(VertexSlotsFree & 0x1)) NextVertexSlot();
        else                          AddCycles(1);
        NormalPipeline = 0;
    }

    void VertexPipelineCmdDelayed6() noexcept
    {
        // commands 0x20, 0x30, 0x31, 0x72 that can run 6 cycles after a vertex
        if (VertexPipeline > 2) AddCycles((VertexPipeline - 2) + 1);
        else                    AddCycles(NormalPipeline + 1);
        NormalPipeline = 0;
    }

    void VertexPipelineCmdDelayed8() noexcept
    {
        // commands 0x29, 0x2A, 0x2B, 0x33, 0x34, 0x41, 0x60, 0x71 that can run 8 cycles after a vertex
        if (VertexPipeline > 0) AddCycles(VertexPipeline + 1);
        else                    AddCycles(NormalPipeline + 1);
        NormalPipeline = 0;
    }

    void VertexPipelineCmdDelayed4() noexcept
    {
        // all other commands can run 4 cycles after a vertex
        // no need to do much here since that is the minimum
        AddCycles(NormalPipeline + 1);
        NormalPipeline = 0;
    }

    std::unique_ptr<Renderer3D> CurrentRenderer = nullptr;

    u16 RenderXPos = 0;

public:
    FIFO<CmdFIFOEntry, 256> CmdFIFO {};
    FIFO<CmdFIFOEntry, 4> CmdPIPE {};

    FIFO<CmdFIFOEntry, 64> CmdStallQueue {};

    u32 ZeroDotWLimit = 0xFFFFFF;

    u32 GXStat = 0;

    u32 ExecParams[32] {};
    u32 ExecParamCount = 0;

    s32 CycleCount = 0;
    s32 VertexPipeline = 0;
    s32 NormalPipeline = 0;
    s32 PolygonPipeline = 0;
    s32 VertexSlotCounter = 0;
    u32 VertexSlotsFree = 0;

    u32 NumPushPopCommands = 0;
    u32 NumTestCommands = 0;


    u32 MatrixMode = 0;

    s32 ProjMatrix[16] {};
    s32 PosMatrix[16] {};
    s32 VecMatrix[16] {};
    s32 TexMatrix[16] {};

    s32 ClipMatrix[16] {};
    bool ClipMatrixDirty = false;
    // what vertices are drawn with: ClipMatrix, or Pos x F x Proj with the free camera on
    s32 RenderClipMatrix[16] {};

    // free camera (see SetFreeCamera)
    bool FreeCamOn = false;
    float FreeCamYaw = 0, FreeCamPitch = 0, FreeCamZoom = 0;
    s32 FreeCamMatrix[16] {};
    float FreeCamPivotW = 0;                  // clip w at the screen centre (game camera)
    std::vector<float> FreeCamSamples;        // this frame's centre depths while it is on
    float FreeCamUp[3] {0, 1, 0};             // world up in the game camera's view space
    bool FreeCamAwaitCamera = false;          // a projection was set: the next position load is the camera
    void FreeCamCaptureCamera() noexcept;
    void FreeCamMeasurePivot() noexcept;
    void FreeCamBuildMatrix() noexcept;

    u32 Viewport[6] {};

    s32 ProjMatrixStack[16] {};
    s32 PosMatrixStack[32][16] {};
    s32 VecMatrixStack[32][16] {};
    s32 TexMatrixStack[16] {};
    s32 ProjMatrixStackPointer = 0;
    s32 PosMatrixStackPointer = 0;
    s32 TexMatrixStackPointer = 0;

    u32 NumCommands = 0;
    u32 CurCommand = 0;
    u32 ParamCount = 0;
    u32 TotalParams = 0;

    bool GeometryEnabled = false;
    bool RenderingEnabled = false;

    u32 DispCnt = 0;
    u8 AlphaRefVal = 0;
    u8 AlphaRef = 0;

    u16 ToonTable[32] {};
    u16 EdgeTable[8] {};

    u32 FogColor = 0;
    u32 FogOffset = 0;
    u8 FogDensityTable[32] {};

    u32 ClearAttr1 = 0;
    u32 ClearAttr2 = 0;

    u32 RenderDispCnt = 0;
    u8 RenderAlphaRef = 0;

    u16 RenderToonTable[32] {};
    u16 RenderEdgeTable[8] {};

    u32 RenderFogColor = 0;
    u32 RenderFogOffset = 0;
    u32 RenderFogShift = 0;
    u8 RenderFogDensityTable[34] {};

    u32 RenderClearAttr1 = 0;
    u32 RenderClearAttr2 = 0;

    bool RenderFrameIdentical = false; // not part of the hardware state, don't serialize

    bool RenderScreenSwapAt3D = false;

    bool AbortFrame = false;

    u64 Timestamp = 0;


    u32 PolygonMode = 0;
    s16 CurVertex[3] {};
    u8 VertexColor[3] {};
    s16 TexCoords[2] {};
    s16 RawTexCoords[2] {};
    s16 Normal[3] {};

    s16 LightDirection[4][3] {};
    s32 SpecRecip[4] {};
    u8 LightColor[4][3] {};
    u8 MatDiffuse[3] {};
    u8 MatAmbient[3] {};
    u8 MatSpecular[3] {};
    u8 MatEmission[3] {};

    bool UseShininessTable = false;
    u8 ShininessTable[128] {};

    u32 PolygonAttr = 0;
    u32 CurPolygonAttr = 0;

    u32 TexParam = 0;
    u32 TexPalette = 0;

    s32 PosTestResult[4] {};
    s16 VecTestResult[3] {};

    Vertex TempVertexBuffer[4] {};
    u32 VertexNum = 0;
    u32 VertexNumInPoly = 0;
    u32 NumConsecutivePolygons = 0;
    Polygon* LastStripPolygon = nullptr;
    u32 NumOpaquePolygons = 0;

    Vertex VertexRAM[6144 * 2] {};
    Polygon PolygonRAM[2048 * 2] {};
    // the hardware's limits; raised while a replacement replays into its own buffers
    u32 VertexLimit = 6144;
    u32 PolygonLimit = 2048;

    Vertex* CurVertexRAM = nullptr;
    Polygon* CurPolygonRAM = nullptr;
    u32 NumVertices = 0;
    u32 NumPolygons = 0;
    u32 CurRAMBank = 0;

    // the first 2048 entries are the hardware's (and all a savestate keeps); replacement
    // geometry from HD models follows when that is on
    std::vector<Polygon*> RenderPolygonRAM = std::vector<Polygon*>(2048, nullptr);
    u32 RenderNumPolygons = 0;

    // HD model replacement (GPU3D_HDModels.cpp)
    static constexpr u32 HDDrawSlots = 4096;            // tags cycle through these
    static constexpr u32 HDVertexCapacity = 6144 * 16;
    static constexpr u32 HDPolygonCapacity = 2048 * 16;
    const HDModelSource* ModelSource = nullptr;
    std::vector<HDModelDraw> HDDraws;
    u32 HDNextTag = 0;
    u32 HDTaggedWordsLeft = 0;
    bool HDWritingFromDma = false;                      // set by the ARM9 DMA while it writes the GX FIFO
    // a display list the CPU is writing word by word, while it may be a replaced one
    std::vector<u32> HDSpecWords;
    std::vector<const HDModelSource::Original*> HDSpecCandidates;
    u16 HDSpecTag = 0;                                  // the draw its entries are tagged with meanwhile
    u32 HDStatCpuLists = 0;
    u32 HDStatSpecStarts = 0;
    u16 HDTaggedWriteTag = 0;
    u16 HDExecTag = 0;
    std::vector<Vertex> HDVertexRAM[2];
    std::vector<Polygon> HDPolygonRAM[2];
    u32 HDBank = 0;
    u32 HDNumVertices = 0;
    u32 HDNumPolygons = 0;
    std::vector<u8> HDPlaced;                           // by tag, while building the render list
    u32 HDStatDraws = 0;
    u32 HDStatPolygons = 0;
    u32 HDStatFrames = 0;

    u32 FlushRequest = 0;
    u32 FlushAttributes = 0;
    u32 ScrolledLine[256]; // not part of the hardware state, don't serialize
};

class Renderer3D
{
public:
    virtual ~Renderer3D() = default;

    Renderer3D(const Renderer3D&) = delete;
    Renderer3D& operator=(const Renderer3D&) = delete;

    virtual void Reset(GPU& gpu) = 0;

    // This "Accelerated" flag currently communicates if the framebuffer should
    // be allocated differently and other little misc handlers. Ideally there
    // are more detailed "traits" that we can ask of the Renderer3D type
    const bool Accelerated;

    virtual void VCount144(GPU& gpu) {};
    virtual void Stop(const GPU& gpu) {}
    virtual void RenderFrame(GPU& gpu) = 0;
    virtual void RestartFrame(GPU& gpu) {};
    virtual u32* GetLine(int line) = 0;
    virtual void Blit(const GPU& gpu) {};

    virtual void SetupAccelFrame() {}
    virtual void PrepareCaptureFrame() {}
    virtual void BeginCaptureFrame() {}
    virtual void SetCaptureScreenSwapHint(bool screenSwap, u32 captureCnt, u32 displayCnt)
    {
        (void)screenSwap;
        (void)captureCnt;
        (void)displayCnt;
    }
    [[nodiscard]] virtual bool GetLastServedCaptureSourceIdentity(
        CaptureSourceIdentity& outIdentity) const noexcept
    {
        outIdentity = {};
        return false;
    }
    [[nodiscard]] virtual bool UsesStructured2DMetadata() const noexcept { return false; }
    [[nodiscard]] virtual VulkanPipelineProfile GetVulkanPipelineProfile() const noexcept
    {
        return VulkanPipelineProfile::Compatibility;
    }
    virtual void SetOutputTexture(int buffer, u32 texture) {}
    virtual void BindOutputTexture(int buffer) {}

    virtual bool NeedsShaderCompile() { return false; }
    virtual void ShaderCompileStep(int& current, int& count) {}

protected:
    Renderer3D(bool Accelerated);
};

}

#endif
