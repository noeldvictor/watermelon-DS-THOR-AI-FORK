// Reverse-engineering tools: memory, registers, the interpreter switch and the GDB stub. Driven by the
// debug build's MCP server (DevTools.kt) through MelonEmulator's debug* JNI functions.

#include "MelonInstance.h"

#include <algorithm>

#include "ARM.h"
#include "ARMJIT.h"
#include "MemConstants.h"
#include "NDSCart.h"
#include "Platform.h"

namespace MelonDSAndroid
{

namespace
{
bool IsIoAddress(u32 addr)
{
    return (addr >> 24) == 0x04;
}

// I/O registers that only hold state: the 2D engines' display, BG, window and blend
// registers, VRAMCNT and POWCNT. Reading them has no side effect (unlike FIFOs and IRQ flags).
bool IsReadableIo(u32 addr)
{
    return (addr >= 0x04000000 && addr < 0x04000070)
        || (addr >= 0x04001000 && addr < 0x04001070)
        || (addr >= 0x04000240 && addr < 0x0400024A)
        || (addr >= 0x04000304 && addr < 0x04000308);
}

bool IsHalfwordOnlyRegion(u32 addr)
{
    // palette, VRAM and OAM drop byte writes
    const u32 region = addr >> 24;
    return region >= 0x05 && region <= 0x07;
}
}

void MelonInstance::readMemoryForDebug(int cpu, u32 address, u32 length, u8* out)
{
    for (u32 i = 0; i < length; i++)
    {
        const u32 addr = address + i;
        // I/O reads have side effects (FIFOs, IRQ flags): reported as 0, except plain state
        if (IsIoAddress(addr))
        {
            out[i] = (cpu == 0 && IsReadableIo(addr)) ? nds->ARM9Read8(addr) : 0;
            continue;
        }

        if (cpu == 0)
        {
            ARMv5& arm9 = nds->ARM9;
            if (addr < arm9.ITCMSize)
                out[i] = arm9.ITCM[addr & (ITCMPhysicalSize - 1)];
            else if ((addr & arm9.DTCMMask) == arm9.DTCMBase)
                out[i] = arm9.DTCM[addr & (DTCMPhysicalSize - 1)];
            else if ((addr >> 24) == 0x02)
                out[i] = nds->MainRAM[addr & nds->MainRAMMask];
            else
                out[i] = nds->ARM9Read8(addr);
        }
        else
        {
            out[i] = nds->ARM7Read8(addr);
        }
    }
}

void MelonInstance::writeMemoryForDebug(int cpu, u32 address, const u8* data, u32 length)
{
    for (u32 i = 0; i < length; i++)
    {
        const u32 addr = address + i;
        const u8 value = data[i];
        if (IsIoAddress(addr))
            continue;

        if (cpu == 0)
        {
            ARMv5& arm9 = nds->ARM9;
            if (addr < arm9.ITCMSize)
            {
                arm9.ITCM[addr & (ITCMPhysicalSize - 1)] = value;
#ifdef JIT_ENABLED
                nds->JIT.CheckAndInvalidate<0, ARMJIT_Memory::memregion_ITCM>(addr);
#endif
                continue;
            }
            if ((addr & arm9.DTCMMask) == arm9.DTCMBase)
            {
                arm9.DTCM[addr & (DTCMPhysicalSize - 1)] = value;
                continue;
            }
        }

        if (IsHalfwordOnlyRegion(addr))
        {
            const u32 halfword = addr & ~1u;
            u16 current = cpu == 0 ? nds->ARM9Read16(halfword) : nds->ARM7Read16(halfword);
            current = (addr & 1) ? (u16)((current & 0x00FF) | (value << 8)) : (u16)((current & 0xFF00) | value);
            if (cpu == 0)
                nds->ARM9Write16(halfword, current);
            else
                nds->ARM7Write16(halfword, current);
        }
        else if (cpu == 0)
        {
            // the bus write invalidates JIT blocks compiled from this address
            nds->ARM9Write8(addr, value);
        }
        else
        {
            nds->ARM7Write8(addr, value);
        }
    }
}

void MelonInstance::queueDebugAction(std::function<void(NDS&)> action)
{
    std::lock_guard lock(debugActionMutex);
    debugActions.push_back(std::move(action));
    debugActionsPending.store(true, std::memory_order_release);
}

void MelonInstance::runPendingDebugActions()
{
    if (!debugActionsPending.load(std::memory_order_acquire))
        return;

    std::vector<std::function<void(NDS&)>> actions;
    {
        std::lock_guard lock(debugActionMutex);
        actions.swap(debugActions);
        debugActionsPending.store(false, std::memory_order_release);
    }
    for (auto& action : actions)
        action(*nds);
}

std::array<u32, 21> MelonInstance::getRegistersForDebug(int cpu)
{
    std::array<u32, 21> values {};
    ARM& arm = cpu == 0 ? static_cast<ARM&>(nds->ARM9) : static_cast<ARM&>(nds->ARM7);
    for (int i = 0; i < 15; i++)
        values[i] = arm.R[i];
    const bool thumb = (arm.CPSR & 0x20) != 0;
    // between frames both the JIT and the interpreter hold R15 one instruction past the next one
    values[15] = arm.R[15] - (thumb ? 2 : 4);
    values[16] = arm.CPSR;
    values[17] = (isJitActiveForDebug() ? 1u : 0u) | (arm.Halted ? 2u : 0u) | (thumb ? 4u : 0u);
    values[18] = nds->ARM9.ITCMSize;
    values[19] = nds->ARM9.DTCMBase;
    values[20] = nds->ARM9.DTCMMask;
    return values;
}

bool MelonInstance::isJitActiveForDebug() const
{
#ifdef JIT_ENABLED
    return nds->IsJITEnabled();
#else
    return false;
#endif
}

void MelonInstance::setInterpreterForDebug(bool enabled)
{
    queueDebugAction([this, enabled](NDS& ds) {
#ifdef GDBSTUB_ENABLED
        if (!enabled && debugGdbActive)
        {
            ds.SetGdbArgs(std::nullopt);
            debugGdbActive = false;
        }
#endif
#ifdef JIT_ENABLED
        if (enabled && ds.IsJITEnabled())
        {
            const auto configuration = configurationSnapshot();
            debugSavedJit = JITArgs {
                static_cast<unsigned>(ds.JIT.GetMaxBlockSize()),
                ds.JIT.LiteralOptimizationsEnabled(),
                ds.JIT.BranchOptimizationsEnabled(),
                ds.JIT.FastMemoryEnabled(),
                configuration && configuration->hgEngineFixEnabled,
            };
            ds.SetJITArgs(std::nullopt);
            // the JIT keeps no pipeline; the interpreter needs the next two instructions fetched
            // (what a save state made under the JIT does before an interpreter loads it)
            ds.ARM9.FillPipeline();
            ds.ARM7.FillPipeline();
            Platform::Log(Platform::LogLevel::Warn, "DebugTools: interpreter on (JIT off)\n");
        }
        else if (!enabled && debugSavedJit)
        {
            // the interpreter leaves R15 where the JIT expects it; only stale blocks must go
            ds.JIT.Reset();
            ds.SetJITArgs(*debugSavedJit);
            debugSavedJit.reset();
            Platform::Log(Platform::LogLevel::Warn, "DebugTools: JIT back on\n");
        }
#endif
    });
}

void MelonInstance::startGdbStubForDebug(int portArm9, int portArm7)
{
    setInterpreterForDebug(true);
    queueDebugAction([this, portArm9, portArm7](NDS& ds) {
#ifdef GDBSTUB_ENABLED
        GDBArgs args {};
        args.PortARM9 = static_cast<u16>(portArm9);
        args.PortARM7 = static_cast<u16>(portArm7);
        ds.SetGdbArgs(args);
        debugGdbActive = true;
        Platform::Log(Platform::LogLevel::Warn, "DebugTools: GDB stub on 127.0.0.1 ARM9 %d ARM7 %d\n", portArm9, portArm7);
#else
        (void)ds;
        (void)portArm9;
        (void)portArm7;
#endif
    });
}

void MelonInstance::stopGdbStubForDebug()
{
    queueDebugAction([this](NDS& ds) {
#ifdef GDBSTUB_ENABLED
        if (debugGdbActive)
        {
            ds.SetGdbArgs(std::nullopt);
            debugGdbActive = false;
        }
#else
        (void)ds;
#endif
    });
}

std::vector<u8> MelonInstance::readRomForDebug(u32 offset, u32 length)
{
    NDSCart::CartCommon* cart = nds->GetNDSCart();
    if (!cart || !cart->GetROM() || offset >= cart->GetROMLength())
        return {};

    const u32 available = std::min(length, cart->GetROMLength() - offset);
    const u8* rom = cart->GetROM();
    return std::vector<u8>(rom + offset, rom + offset + available);
}

}
