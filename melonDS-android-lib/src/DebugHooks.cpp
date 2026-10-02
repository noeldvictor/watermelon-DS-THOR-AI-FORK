#include "DebugHooks.h"

#include <algorithm>
#include <deque>
#include <mutex>
#include <unordered_map>

#include "ARM.h"
#include "xxhash/xxhash.h"

namespace melonDS::DebugHooks
{
std::atomic<bool> WatchActive = false;
std::atomic<bool> CallTraceActive = false;
std::atomic<bool> DisplayListTraceActive = false;

namespace
{
std::mutex Lock;
std::atomic<u32> Frame = 0;

struct WatchConfig
{
    u32 start = 0;
    u32 end = 0;
    bool reads = false;
    bool writes = false;
    bool arm7 = false;
    u32 maxEvents = 0;
} Watch;
// bit 32 set = only this value (low 32 bits); read outside the lock, so it is its own atomic
std::atomic<u64> WatchValue = 0;
std::deque<AccessEvent> AccessEvents;
u32 AccessDropped = 0;

struct CallConfig
{
    u32 targetStart = 0;
    u32 targetEnd = 0;
    bool arm7 = false;
    u32 maxEvents = 0;
} Calls;
std::deque<CallEvent> CallEvents;
std::unordered_map<u64, u32> CallCounts;
u32 CallDropped = 0;

// keyed by hash; a different size with the same hash is not worth telling apart
std::unordered_map<u64, DisplayListStat> DisplayLists;
constexpr size_t MaxDisplayLists = 20000;
std::vector<u8> DisplayListCopy;
// the CPU's GX FIFO words, the newest MaxGxCpuWords kept
std::deque<u32> GxCpuWords;
u32 GxCpuDropped = 0;
constexpr size_t MaxGxCpuWords = 1u << 20;

u32 InstructionAddress(const ARM& cpu)
{
    // R15 runs two instructions ahead of the one executing
    return cpu.R[15] - ((cpu.CPSR & 0x20) ? 4 : 8);
}
}

void SetFrame(u32 frame)
{
    Frame.store(frame, std::memory_order_relaxed);
}

void StartWatch(u32 start, u32 end, bool reads, bool writes, bool arm7, u32 maxEvents, std::optional<u32> value)
{
    std::lock_guard guard(Lock);
    Watch = {start, end, reads, writes, arm7, std::max(maxEvents, 1u)};
    WatchValue.store(value ? ((1ull << 32) | *value) : 0, std::memory_order_relaxed);
    AccessEvents.clear();
    AccessDropped = 0;
    WatchActive.store(reads || writes, std::memory_order_release);
}

void StopWatch()
{
    WatchActive.store(false, std::memory_order_release);
}

std::vector<AccessEvent> TakeAccessEvents(u32* dropped)
{
    std::lock_guard guard(Lock);
    std::vector<AccessEvent> events(AccessEvents.begin(), AccessEvents.end());
    AccessEvents.clear();
    if (dropped) *dropped = AccessDropped;
    AccessDropped = 0;
    return events;
}

void StartCallTrace(u32 targetStart, u32 targetEnd, bool arm7, u32 maxEvents)
{
    std::lock_guard guard(Lock);
    Calls = {targetStart, targetEnd, arm7, std::max(maxEvents, 1u)};
    CallEvents.clear();
    CallCounts.clear();
    CallDropped = 0;
    CallTraceActive.store(true, std::memory_order_release);
}

void StopCallTrace()
{
    CallTraceActive.store(false, std::memory_order_release);
}

std::vector<CallEvent> TakeCallEvents(u32* dropped)
{
    std::lock_guard guard(Lock);
    std::vector<CallEvent> events(CallEvents.begin(), CallEvents.end());
    CallEvents.clear();
    if (dropped) *dropped = CallDropped;
    CallDropped = 0;
    return events;
}

std::vector<CallCount> TakeCallCounts()
{
    std::lock_guard guard(Lock);
    std::vector<CallCount> counts;
    counts.reserve(CallCounts.size());
    for (const auto& [key, count] : CallCounts)
        counts.push_back({(u32)(key >> 32), (u32)key, count});
    CallCounts.clear();
    std::sort(counts.begin(), counts.end(), [](const CallCount& a, const CallCount& b) { return a.count > b.count; });
    return counts;
}

void StartDisplayListTrace()
{
    std::lock_guard guard(Lock);
    DisplayLists.clear();
    GxCpuWords.clear();
    GxCpuDropped = 0;
    DisplayListTraceActive.store(true, std::memory_order_relaxed);
}

void StopDisplayListTrace()
{
    DisplayListTraceActive.store(false, std::memory_order_relaxed);
}

std::vector<DisplayListStat> TakeDisplayListStats()
{
    std::lock_guard guard(Lock);
    std::vector<DisplayListStat> stats;
    stats.reserve(DisplayLists.size());
    for (const auto& [hash, stat] : DisplayLists)
        stats.push_back(stat);
    DisplayLists.clear();
    std::sort(stats.begin(), stats.end(), [](const DisplayListStat& a, const DisplayListStat& b) { return a.count > b.count; });
    return stats;
}

std::vector<u32> TakeGxCpuWords(u32* dropped)
{
    std::lock_guard guard(Lock);
    std::vector<u32> words(GxCpuWords.begin(), GxCpuWords.end());
    GxCpuWords.clear();
    if (dropped) *dropped = GxCpuDropped;
    GxCpuDropped = 0;
    return words;
}

void RecordGxCpuWord(u32 word)
{
    std::lock_guard guard(Lock);
    if (!DisplayListTraceActive.load(std::memory_order_relaxed))
        return;
    if (GxCpuWords.size() >= MaxGxCpuWords)
    {
        GxCpuWords.pop_front();
        GxCpuDropped++;
    }
    GxCpuWords.push_back(word);
}

void RecordDisplayList(const u8* ram, u32 ramMask, u32 src, u32 size)
{
    if (size == 0 || size > 0x100000)
        return;
    std::lock_guard guard(Lock);
    if (!DisplayListTraceActive.load(std::memory_order_relaxed))
        return;
    // main RAM mirrors: the transfer may run past the end of the mask and wrap
    const u32 start = src & ramMask;
    u64 hash;
    if (start + size <= ramMask + 1)
    {
        hash = XXH64(&ram[start], size, 0);
    }
    else
    {
        DisplayListCopy.resize(size);
        for (u32 i = 0; i < size; i++)
            DisplayListCopy[i] = ram[(start + i) & ramMask];
        hash = XXH64(DisplayListCopy.data(), size, 0);
    }
    const u32 frame = Frame.load(std::memory_order_relaxed);
    auto it = DisplayLists.find(hash);
    if (it == DisplayLists.end())
    {
        if (DisplayLists.size() >= MaxDisplayLists)
            return;
        DisplayLists.emplace(hash, DisplayListStat{hash, size, 1, src, frame, frame});
        return;
    }
    it->second.count++;
    it->second.lastSrc = src;
    it->second.lastFrame = frame;
}

void RecordAccess(const ARM& cpu, u32 addr, u32 value, u8 size, bool write)
{
    // the value test first: a whole-memory value watch sees every access, so it must stay cheap
    const u64 filter = WatchValue.load(std::memory_order_relaxed);
    if (filter && (value & (size == 4 ? 0xFFFFFFFFu : (1u << (size * 8)) - 1)) != (u32)filter)
        return;
    std::lock_guard guard(Lock);
    if (!WatchActive.load(std::memory_order_relaxed))
        return;
    if (cpu.Num == 1 && !Watch.arm7)
        return;
    if (write ? !Watch.writes : !Watch.reads)
        return;
    if (addr >= Watch.end || addr + size <= Watch.start)
        return;

    if (AccessEvents.size() >= Watch.maxEvents)
    {
        AccessEvents.pop_front();
        AccessDropped++;
    }
    AccessEvents.push_back({
        Frame.load(std::memory_order_relaxed),
        InstructionAddress(cpu),
        cpu.R[14],
        addr,
        value,
        size,
        (u8)write,
        (u8)cpu.Num,
        (u8)((cpu.CPSR & 0x20) ? 1 : 0),
        {cpu.R[0], cpu.R[1], cpu.R[2], cpu.R[3]},
        cpu.R[13],
    });
}

void RecordCall(const ARM& cpu, u32 from, u32 to)
{
    std::lock_guard guard(Lock);
    if (!CallTraceActive.load(std::memory_order_relaxed))
        return;
    if (cpu.Num == 1 && !Calls.arm7)
        return;
    u32 target = to & ~1u;
    if (Calls.targetEnd != 0 && (target < Calls.targetStart || target >= Calls.targetEnd))
        return;

    // the key leaves out the CPU; ARM7 and ARM9 code never share addresses in practice
    CallCounts[((u64)from << 32) | to]++;
    if (CallEvents.size() >= Calls.maxEvents)
    {
        CallEvents.pop_front();
        CallDropped++;
    }
    CallEvents.push_back({Frame.load(std::memory_order_relaxed), from, to, (u8)cpu.Num});
}
}
