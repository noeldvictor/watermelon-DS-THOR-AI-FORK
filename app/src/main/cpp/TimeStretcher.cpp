#include "TimeStretcher.h"

#include <algorithm>
#include <cmath>

namespace
{
    // Short sequences suit the high tempos of fast-forward (2x-8x); at 48 kHz: 30 ms sequences,
    // 8 ms cross-fades, and a 12 ms window to search for the best splice point.
    constexpr double kSequenceSeconds = 0.030;
    constexpr double kOverlapSeconds = 0.008;
    constexpr double kSeekSeconds = 0.012;

    int16_t clampSample(float value)
    {
        return static_cast<int16_t>(std::clamp(std::lround(value), -32768L, 32767L));
    }
}

TimeStretcher::TimeStretcher(int sampleRate) :
    sequenceFrames(static_cast<int>(sampleRate * kSequenceSeconds)),
    overlapFrames(static_cast<int>(sampleRate * kOverlapSeconds)),
    seekFrames(static_cast<int>(sampleRate * kSeekSeconds))
{
    overlapTail.resize(static_cast<size_t>(overlapFrames) * 2);
    tailMono.resize(static_cast<size_t>(overlapFrames));
}

void TimeStretcher::reset()
{
    input.clear();
    inputStart = 0;
    output.clear();
    outputStart = 0;
    haveTail = false;
    skipRemainder = 0.0;
}

void TimeStretcher::putInput(const int16_t* frames, int frameCount)
{
    compactInput();
    const size_t base = input.size();
    input.resize(base + static_cast<size_t>(frameCount) * 2);
    for (int i = 0; i < frameCount * 2; i++)
        input[base + i] = static_cast<float>(frames[i]);
}

int TimeStretcher::queuedInputFrames() const
{
    return static_cast<int>(input.size() / 2 - inputStart);
}

int TimeStretcher::takeOutput(int16_t* frames, int frameCount, double tempo)
{
    tempo = std::clamp(tempo, 1.0, 8.0);
    if (static_cast<int>(output.size() / 2 - outputStart) < frameCount)
        processSequences(tempo);

    const int ready = static_cast<int>(output.size() / 2 - outputStart);
    const int written = std::min(ready, frameCount);
    const float* source = output.data() + outputStart * 2;
    for (int i = 0; i < written * 2; i++)
        frames[i] = clampSample(source[i]);

    outputStart += static_cast<size_t>(written);
    if (outputStart == output.size() / 2)
    {
        output.clear();
        outputStart = 0;
    }
    return written;
}

void TimeStretcher::processSequences(double tempo)
{
    const double skipPerSequence = tempo * (sequenceFrames - overlapFrames);

    while (true)
    {
        const double advance = skipPerSequence + skipRemainder;
        const int skip = static_cast<int>(advance);
        const int needed = std::max(skip + overlapFrames, sequenceFrames) + seekFrames;
        if (queuedInputFrames() < needed)
            break;

        const float* in = input.data() + inputStart * 2;
        const int offset = haveTail ? findBestOffset(in) : 0;
        const float* sequence = in + static_cast<size_t>(offset) * 2;

        // Cross-fade the previous sequence's tail into this one, then the body up to the new tail
        for (int i = 0; i < overlapFrames; i++)
        {
            const float fadeIn = static_cast<float>(i) / static_cast<float>(overlapFrames);
            for (int channel = 0; channel < 2; channel++)
            {
                const float next = sequence[i * 2 + channel];
                output.push_back(haveTail
                    ? overlapTail[i * 2 + channel] * (1.0f - fadeIn) + next * fadeIn
                    : next);
            }
        }
        for (int i = overlapFrames; i < sequenceFrames - overlapFrames; i++)
        {
            output.push_back(sequence[i * 2]);
            output.push_back(sequence[i * 2 + 1]);
        }

        const float* tail = sequence + static_cast<size_t>(sequenceFrames - overlapFrames) * 2;
        for (int i = 0; i < overlapFrames; i++)
        {
            overlapTail[i * 2] = tail[i * 2];
            overlapTail[i * 2 + 1] = tail[i * 2 + 1];
            tailMono[i] = tail[i * 2] + tail[i * 2 + 1];
        }
        haveTail = true;

        skipRemainder = advance - skip;
        inputStart += static_cast<size_t>(skip);
    }
}

// The offset within the seek window where the input best continues the previous tail
// (normalised cross-correlation on mono).
int TimeStretcher::findBestOffset(const float* in) const
{
    int bestOffset = 0;
    double bestScore = -1e30;

    double energy = 0.0;
    for (int i = 0; i < overlapFrames; i++)
    {
        const double sample = in[i * 2] + in[i * 2 + 1];
        energy += sample * sample;
    }

    for (int offset = 0; offset < seekFrames; offset++)
    {
        const float* candidate = in + static_cast<size_t>(offset) * 2;
        double correlation = 0.0;
        for (int i = 0; i < overlapFrames; i++)
            correlation += static_cast<double>(tailMono[i]) * (candidate[i * 2] + candidate[i * 2 + 1]);

        const double score = correlation / std::sqrt(energy + 1.0);
        if (score > bestScore)
        {
            bestScore = score;
            bestOffset = offset;
        }

        // slide the energy window by one frame
        const double leaving = candidate[0] + candidate[1];
        const double entering = candidate[overlapFrames * 2] + candidate[overlapFrames * 2 + 1];
        energy += entering * entering - leaving * leaving;
    }

    return bestOffset;
}

void TimeStretcher::compactInput()
{
    if (inputStart == 0)
        return;
    if (inputStart * 2 < input.size() / 2 && inputStart < 16384)
        return;

    input.erase(input.begin(), input.begin() + static_cast<std::ptrdiff_t>(inputStart * 2));
    inputStart = 0;
}
