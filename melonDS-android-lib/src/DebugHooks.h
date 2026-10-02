#ifndef MELONDS_DEBUGHOOKS_H
#define MELONDS_DEBUGHOOKS_H

#include <atomic>
#include <optional>
#include <vector>

#include "types.h"

namespace melonDS
{
class ARM;

/**
 * Reverse-engineering hooks for the frontend's debug tools: memory watchpoints (which instruction
 * reads or writes an address range) and a call trace (which functions call which). Both only see
 * what the interpreter executes; the JIT turns memory accesses and branches into native code, so a
 * session runs with the JIT off. Cheap when idle: one relaxed atomic load per access or call.
 */
namespace DebugHooks
{
struct AccessEvent
{
    u32 frame;
    u32 pc;     // address of the instruction (DMA: whatever the CPU was at)
    u32 lr;
    u32 addr;
    u32 value;
    u8 size;    // bytes
    u8 write;
    u8 cpu;     // 0 = ARM9, 1 = ARM7
    u8 thumb;
    u32 r[4];   // R0-R3: usually the arguments and the object pointer
    u32 sp;
};

struct CallEvent
{
    u32 frame;
    u32 from;   // address of the call instruction
    u32 to;     // target, bit 0 set for Thumb
    u8 cpu;
};

struct CallCount
{
    u32 from;
    u32 to;
    u32 count;
};

struct DisplayListStat
{
    u64 hash;       // XXH64 of the bytes sent (a model shape's display list: tools/hd_remaster/models3d.py)
    u32 size;       // bytes
    u32 count;      // transfers since the last take
    u32 lastSrc;    // where the last one was read from
    u32 firstFrame;
    u32 lastFrame;
};

extern std::atomic<bool> WatchActive;
extern std::atomic<bool> CallTraceActive;
extern std::atomic<bool> DisplayListTraceActive;

/**
 * Record accesses to [start, end) by the ARM9 (and ARM7 when arm7 is set); keeps the newest maxEvents.
 * With a value, only accesses of exactly that value count ("who writes 0x1555 anywhere").
 */
void StartWatch(u32 start, u32 end, bool reads, bool writes, bool arm7, u32 maxEvents, std::optional<u32> value = std::nullopt);
void StopWatch();
/** The events recorded since the last call (or since StartWatch), oldest first. */
std::vector<AccessEvent> TakeAccessEvents(u32* dropped = nullptr);

/** Record calls (BL/BLX) whose target lies in [targetStart, targetEnd); end 0 = everything. */
void StartCallTrace(u32 targetStart, u32 targetEnd, bool arm7, u32 maxEvents);
void StopCallTrace();
std::vector<CallEvent> TakeCallEvents(u32* dropped = nullptr);
/** How often each (call site, target) pair ran since the last call, most frequent first. */
std::vector<CallCount> TakeCallCounts();

/**
 * Record display lists: every ARM9 DMA from main RAM into the geometry FIFO, by content (the SDK
 * sends a model shape's display list this way, byte for byte). Display lists the CPU writes
 * itself are not seen.
 */
void StartDisplayListTrace();
void StopDisplayListTrace();
/** Display lists sent since the last call, most frequent first. */
std::vector<DisplayListStat> TakeDisplayListStats();

/** The frame number stamped on events (the frontend's frame counter). */
void SetFrame(u32 frame);

void RecordAccess(const ARM& cpu, u32 addr, u32 value, u8 size, bool write);
void RecordCall(const ARM& cpu, u32 from, u32 to);
void RecordDisplayList(const u8* ram, u32 ramMask, u32 src, u32 size);

inline void OnAccess(const ARM& cpu, u32 addr, u32 value, u8 size, bool write)
{
    if (WatchActive.load(std::memory_order_relaxed)) [[unlikely]]
        RecordAccess(cpu, addr, value, size, write);
}

inline void OnCall(const ARM& cpu, u32 from, u32 to)
{
    if (CallTraceActive.load(std::memory_order_relaxed)) [[unlikely]]
        RecordCall(cpu, from, to);
}

inline void OnDisplayList(const u8* ram, u32 ramMask, u32 src, u32 size)
{
    if (DisplayListTraceActive.load(std::memory_order_relaxed)) [[unlikely]]
        RecordDisplayList(ram, ramMask, src, size);
}

/** Reports a CPU read once it has its value, on every return path of the read function. */
struct ReadWatch
{
    const ARM& cpu;
    u32 addr;
    const u32* val;
    u8 size;

    ~ReadWatch() { OnAccess(cpu, addr, *val, size, false); }
};
}
}

#endif
