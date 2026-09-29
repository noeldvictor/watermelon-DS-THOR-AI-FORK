#include "OboeCallback.h"
#include "MelonDS.h"
#include "types.h"
#include "Platform.h"
#include "SPU.h"

using namespace melonDS;

#define INTERNAL_FRAME_RATE 59.8260982880808f

namespace
{
    // the stream is opened at 48 kHz, the rate the SPU resamples to
    constexpr int kOutputSampleRate = 48000;
    // more than this queued in the stretcher (a stalled output, a burst) is dropped
    constexpr int kMaxQueuedFastForwardFrames = kOutputSampleRate / 2;
}

OboeCallback::OboeCallback(int volume, void (*onErrorCallback)(void), std::ostream* recordingStream) :
    _volume(volume),
    onErrorCallback(onErrorCallback),
    _recordingStream(recordingStream),
    fastForwardStretcher(kOutputSampleRate)
{
    audioSampleFrac = 0;
}

oboe::DataCallbackResult
OboeCallback::onAudioReady(oboe::AudioStream *stream, void *audioData, int32_t numFrames) {
    auto currentInstance = activeInstance.lock();

    if (!currentInstance)
    {
        memset(audioData, 0, numFrames * sizeof(u16) * 2);
        return oboe::DataCallbackResult::Continue;
    }

    int len = numFrames;

    double skew = std::clamp(60.0 / INTERNAL_FRAME_RATE, 0.995, 1.005);
    currentInstance->setAudioOutputSkew(skew);

    if (MelonDSAndroid::isFastForwardActive())
    {
        renderFastForward(*currentInstance, (int16_t*) audioData, numFrames);
        return oboe::DataCallbackResult::Continue;
    }
    if (fastForwardAudioActive)
    {
        fastForwardAudioActive = false;
        fastForwardStretcher.reset();
        currentInstance->setLargeAudioOutputBuffer(false);
    }

    int len_in = getNumSamplesOut(len);
    if (len_in > numFrames) len_in = numFrames;

    int num_in = currentInstance->readAudioOutput((s16*) audioData, len_in);

    if (num_in < 1)
    {
        memset(audioData, 0, len * sizeof(s16) * 2);
        return oboe::DataCallbackResult::Continue;
    }

    if (_volume < 256)
    {
        s16* samples = (s16*) audioData;
        for (int i = 0; i < num_in * 2; i++)
            samples[i] = ((s32) samples[i] * _volume) >> 8;
    }

    if (num_in < len_in)
    {
        int last = num_in - 1;

        for (int i = num_in; i < len_in; i++)
            ((u32*)audioData)[i] = ((u32*)audioData)[last];
    }

    if (_recordingStream) [[unlikely]]
        _recordingStream->write((char*) audioData, numFrames * sizeof(s16) * 2);

    return oboe::DataCallbackResult::Continue;
}

/**
 * Fast-forward: the core produces several times more audio than the output plays. Before, the SPU
 * buffer overwrote what didn't fit, so fast-forward sounded choppy. Now everything is drained each
 * callback and time-compressed to the output length at the original pitch; the tempo follows the
 * rate audio arrives at, nudged to keep enough queued for the stretcher.
 */
void OboeCallback::renderFastForward(MelonDSAndroid::MelonInstance& instance, int16_t* audioData, int32_t numFrames)
{
    if (!fastForwardAudioActive)
    {
        fastForwardAudioActive = true;
        fastForwardStretcher.reset();
        fastForwardArrivalRate = 1.0;
        instance.setLargeAudioOutputBuffer(true);
    }

    const int available = std::min(instance.getAudioOutputAvailable(), numFrames * 8);
    int read = 0;
    if (available > 0)
    {
        fastForwardInput.resize(static_cast<size_t>(available) * 2);
        read = instance.readAudioOutput(fastForwardInput.data(), available);
        if (read > 0)
            fastForwardStretcher.putInput(fastForwardInput.data(), read);
    }

    fastForwardArrivalRate += 0.05 * (static_cast<double>(read) / numFrames - fastForwardArrivalRate);
    const double rate = std::clamp(fastForwardArrivalRate, 1.0, 8.0);
    // enough input for the next sequence at this tempo, plus half a sequence of slack
    const double target = rate * kOutputSampleRate * 0.034 + kOutputSampleRate * 0.020;
    const int queued = fastForwardStretcher.queuedInputFrames();
    if (queued > kMaxQueuedFastForwardFrames)
        fastForwardStretcher.reset();
    const double tempo = rate * (1.0 + 0.5 * (queued - target) / target);

    const int written = fastForwardStretcher.takeOutput(audioData, numFrames, tempo);
    if (written < numFrames)
        memset(audioData + written * 2, 0, static_cast<size_t>(numFrames - written) * sizeof(int16_t) * 2);

    if (_volume < 256)
    {
        for (int i = 0; i < numFrames * 2; i++)
            audioData[i] = static_cast<int16_t>((static_cast<s32>(audioData[i]) * _volume) >> 8);
    }

    if (_recordingStream) [[unlikely]]
        _recordingStream->write((char*) audioData, numFrames * sizeof(s16) * 2);
}

void OboeCallback::onErrorAfterClose(oboe::AudioStream* stream, oboe::Result result)
{
    if (result == oboe::Result::ErrorDisconnected && onErrorCallback != nullptr) {
        onErrorCallback();
    }
}

int OboeCallback::getNumSamplesOut(int len)
{
    // TODO: adjust to game speed
    float f_len_in = len /* * (curFPS/60.0)*/;
    f_len_in += audioSampleFrac;
    int len_in = (int) floor(f_len_in);
    audioSampleFrac = f_len_in - len_in;

    return len_in;
}