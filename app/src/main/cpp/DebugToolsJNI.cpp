// JNI side of the reverse-engineering tools (MelonEmulator.debug*). Debug builds only: in release
// builds every function reports "no game".

#include <jni.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <pthread.h>
#include <vector>

#include "DebugHooks.h"
#include "MelonDS.h"
#include "MelonInstance.h"

#ifndef MELONDS_ANDROID_DEBUG_BUILD
#define MELONDS_ANDROID_DEBUG_BUILD 0
#endif

// emulator thread state (MelonDSAndroidJNI.cpp)
extern pthread_mutex_t emuThreadMutex;
extern bool started;
extern bool stop;
extern bool paused;
extern std::atomic_bool isThreadReallyPaused;

namespace
{
std::shared_ptr<MelonDSAndroid::MelonInstance> DebugInstance()
{
    if (MELONDS_ANDROID_DEBUG_BUILD == 0 || !started)
        return nullptr;
    return MelonDSAndroid::getInstanceForDebug();
}

/**
 * Runs body while the emulator thread is parked in its pause wait, if it is: holding the mutex keeps
 * it there. Returns whether it ran.
 */
template <typename Body>
bool WhileParked(Body&& body)
{
    pthread_mutex_lock(&emuThreadMutex);
    const bool parked = !stop && paused && isThreadReallyPaused;
    if (parked)
        body();
    pthread_mutex_unlock(&emuThreadMutex);
    return parked;
}

jintArray ToIntArray(JNIEnv* env, const std::vector<jint>& values)
{
    jintArray result = env->NewIntArray(static_cast<jsize>(values.size()));
    if (result && !values.empty())
        env->SetIntArrayRegion(result, 0, static_cast<jsize>(values.size()), values.data());
    return result;
}

jbyteArray ToByteArray(JNIEnv* env, const std::vector<u8>& values)
{
    jbyteArray result = env->NewByteArray(static_cast<jsize>(values.size()));
    if (result && !values.empty())
        env->SetByteArrayRegion(result, 0, static_cast<jsize>(values.size()), reinterpret_cast<const jbyte*>(values.data()));
    return result;
}

/** Queued actions apply at once when the game is paused, otherwise at the next frame start. */
void FlushIfParked(MelonDSAndroid::MelonInstance& instance)
{
    WhileParked([&] { instance.runPendingDebugActions(); });
}
}

extern "C"
{

JNIEXPORT jboolean JNICALL
Java_me_magnum_melonds_MelonEmulator_debugIsGameRunning(JNIEnv*, jobject)
{
    return DebugInstance() != nullptr ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jboolean JNICALL
Java_me_magnum_melonds_MelonEmulator_debugIsPaused(JNIEnv*, jobject)
{
    return WhileParked([] {}) ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jint JNICALL
Java_me_magnum_melonds_MelonEmulator_debugGetFrame(JNIEnv*, jobject)
{
    auto instance = DebugInstance();
    return instance ? instance->getFrameForDebug() : -1;
}

JNIEXPORT jbyteArray JNICALL
Java_me_magnum_melonds_MelonEmulator_debugReadMemory(JNIEnv* env, jobject, jint cpu, jint address, jint length)
{
    auto instance = DebugInstance();
    if (!instance || length < 0 || length > 64 * 1024 * 1024)
        return nullptr;

    std::vector<u8> data(static_cast<size_t>(length));
    instance->readMemoryForDebug(cpu, static_cast<u32>(address), static_cast<u32>(length), data.data());
    return ToByteArray(env, data);
}

/** 0 = no game, 1 = written (the game is paused), 2 = queued for the next frame start */
JNIEXPORT jint JNICALL
Java_me_magnum_melonds_MelonEmulator_debugWriteMemory(JNIEnv* env, jobject, jint cpu, jint address, jbyteArray data)
{
    auto instance = DebugInstance();
    if (!instance || !data)
        return 0;

    const jsize length = env->GetArrayLength(data);
    std::vector<u8> bytes(static_cast<size_t>(length));
    env->GetByteArrayRegion(data, 0, length, reinterpret_cast<jbyte*>(bytes.data()));

    if (WhileParked([&] { instance->writeMemoryForDebug(cpu, static_cast<u32>(address), bytes.data(), bytes.size()); }))
        return 1;

    // the action is stored in the instance, so it holds a plain pointer (a shared_ptr would be a cycle)
    MelonDSAndroid::MelonInstance* target = instance.get();
    instance->queueDebugAction([target, cpu, address, bytes = std::move(bytes)](NDS&) {
        target->writeMemoryForDebug(cpu, static_cast<u32>(address), bytes.data(), bytes.size());
    });
    return 2;
}

/** R0-R14, next instruction, CPSR, flags (bit 0 JIT, bit 1 halted, bit 2 Thumb), ITCM size, DTCM base,
 * DTCM mask, then 1 if paused */
JNIEXPORT jintArray JNICALL
Java_me_magnum_melonds_MelonEmulator_debugGetRegisters(JNIEnv* env, jobject, jint cpu)
{
    auto instance = DebugInstance();
    if (!instance)
        return nullptr;

    std::array<u32, 21> registers {};
    const bool parked = WhileParked([&] { registers = instance->getRegistersForDebug(cpu); });
    if (!parked)
        registers = instance->getRegistersForDebug(cpu);

    std::vector<jint> values(registers.begin(), registers.end());
    values.push_back(parked ? 1 : 0);
    return ToIntArray(env, values);
}

JNIEXPORT jboolean JNICALL
Java_me_magnum_melonds_MelonEmulator_debugSetInterpreter(JNIEnv*, jobject, jboolean enabled)
{
    auto instance = DebugInstance();
    if (!instance)
        return JNI_FALSE;

    instance->setInterpreterForDebug(enabled == JNI_TRUE);
    FlushIfParked(*instance);
    return JNI_TRUE;
}

JNIEXPORT jboolean JNICALL
Java_me_magnum_melonds_MelonEmulator_debugIsJitActive(JNIEnv*, jobject)
{
    auto instance = DebugInstance();
    return instance && instance->isJitActiveForDebug() ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jboolean JNICALL
Java_me_magnum_melonds_MelonEmulator_debugStartGdbStub(JNIEnv*, jobject, jint portArm9, jint portArm7)
{
    auto instance = DebugInstance();
    if (!instance)
        return JNI_FALSE;

    instance->startGdbStubForDebug(portArm9, portArm7);
    FlushIfParked(*instance);
    return JNI_TRUE;
}

JNIEXPORT jboolean JNICALL
Java_me_magnum_melonds_MelonEmulator_debugStopGdbStub(JNIEnv*, jobject)
{
    auto instance = DebugInstance();
    if (!instance)
        return JNI_FALSE;

    instance->stopGdbStubForDebug();
    FlushIfParked(*instance);
    return JNI_TRUE;
}

JNIEXPORT jbyteArray JNICALL
Java_me_magnum_melonds_MelonEmulator_debugReadRom(JNIEnv* env, jobject, jint offset, jint length)
{
    auto instance = DebugInstance();
    if (!instance || offset < 0 || length < 0)
        return nullptr;

    return ToByteArray(env, instance->readRomForDebug(static_cast<u32>(offset), static_cast<u32>(length)));
}

JNIEXPORT jboolean JNICALL
Java_me_magnum_melonds_MelonEmulator_debugWatchStart(JNIEnv*, jobject, jint start, jint end, jboolean reads, jboolean writes, jboolean arm7, jint maxEvents, jboolean matchValue, jint value)
{
    if (!DebugInstance())
        return JNI_FALSE;

    melonDS::DebugHooks::StartWatch(
        static_cast<u32>(start), static_cast<u32>(end), reads == JNI_TRUE, writes == JNI_TRUE, arm7 == JNI_TRUE,
        static_cast<u32>(std::max(maxEvents, 1)),
        matchValue == JNI_TRUE ? std::optional<u32>(static_cast<u32>(value)) : std::nullopt);
    return JNI_TRUE;
}

JNIEXPORT void JNICALL
Java_me_magnum_melonds_MelonEmulator_debugWatchStop(JNIEnv*, jobject)
{
    melonDS::DebugHooks::StopWatch();
}

/** [dropped, then per event: frame, pc, lr, address, value, size, write, cpu, thumb, r0, r1, r2, r3, sp] */
JNIEXPORT jintArray JNICALL
Java_me_magnum_melonds_MelonEmulator_debugWatchTake(JNIEnv* env, jobject)
{
    u32 dropped = 0;
    const auto events = melonDS::DebugHooks::TakeAccessEvents(&dropped);
    std::vector<jint> values;
    values.reserve(1 + events.size() * 14);
    values.push_back(static_cast<jint>(dropped));
    for (const auto& event : events)
    {
        values.insert(values.end(), {
            static_cast<jint>(event.frame), static_cast<jint>(event.pc), static_cast<jint>(event.lr),
            static_cast<jint>(event.addr), static_cast<jint>(event.value), event.size, event.write, event.cpu, event.thumb,
            static_cast<jint>(event.r[0]), static_cast<jint>(event.r[1]), static_cast<jint>(event.r[2]),
            static_cast<jint>(event.r[3]), static_cast<jint>(event.sp),
        });
    }
    return ToIntArray(env, values);
}

JNIEXPORT jboolean JNICALL
Java_me_magnum_melonds_MelonEmulator_debugTraceStart(JNIEnv*, jobject, jint targetStart, jint targetEnd, jboolean arm7, jint maxEvents)
{
    if (!DebugInstance())
        return JNI_FALSE;

    melonDS::DebugHooks::StartCallTrace(
        static_cast<u32>(targetStart), static_cast<u32>(targetEnd), arm7 == JNI_TRUE, static_cast<u32>(std::max(maxEvents, 1)));
    return JNI_TRUE;
}

JNIEXPORT void JNICALL
Java_me_magnum_melonds_MelonEmulator_debugTraceStop(JNIEnv*, jobject)
{
    melonDS::DebugHooks::StopCallTrace();
}

/** [dropped, then per call: frame, from, to, cpu] */
JNIEXPORT jintArray JNICALL
Java_me_magnum_melonds_MelonEmulator_debugTraceTakeEvents(JNIEnv* env, jobject)
{
    u32 dropped = 0;
    const auto events = melonDS::DebugHooks::TakeCallEvents(&dropped);
    std::vector<jint> values;
    values.reserve(1 + events.size() * 4);
    values.push_back(static_cast<jint>(dropped));
    for (const auto& event : events)
    {
        values.insert(values.end(), {
            static_cast<jint>(event.frame), static_cast<jint>(event.from), static_cast<jint>(event.to), event.cpu,
        });
    }
    return ToIntArray(env, values);
}

/** per (call site, target): from, to, count; most frequent first */
JNIEXPORT jintArray JNICALL
Java_me_magnum_melonds_MelonEmulator_debugTraceTakeCounts(JNIEnv* env, jobject)
{
    const auto counts = melonDS::DebugHooks::TakeCallCounts();
    std::vector<jint> values;
    values.reserve(counts.size() * 3);
    for (const auto& count : counts)
        values.insert(values.end(), {static_cast<jint>(count.from), static_cast<jint>(count.to), static_cast<jint>(count.count)});
    return ToIntArray(env, values);
}

}
