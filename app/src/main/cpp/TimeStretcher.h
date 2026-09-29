#ifndef MELONDS_TIMESTRETCHER_H
#define MELONDS_TIMESTRETCHER_H

#include <cstdint>
#include <vector>

/**
 * Pitch-preserving time compression (WSOLA) for fast-forward audio.
 *
 * While fast-forwarding, the core produces audio faster than it can be played. Instead of letting
 * the SPU buffer overwrite what doesn't fit (choppy, skipping audio), the output callback feeds
 * everything to this stretcher, which plays it faster at the original pitch: overlapping
 * sequences of the input, each placed where it best matches the previous one, cross-faded.
 *
 * Stereo, interleaved 16-bit frames in and out. Not thread-safe: owned by the audio callback.
 */
class TimeStretcher
{
public:
    explicit TimeStretcher(int sampleRate);

    void reset();
    void putInput(const int16_t* frames, int frameCount);
    int queuedInputFrames() const;

    /**
     * Writes up to frameCount frames, consuming `tempo` input frames per output frame (1..8).
     * Returns how many were written; fewer while the stretcher is still filling up.
     */
    int takeOutput(int16_t* frames, int frameCount, double tempo);

private:
    void processSequences(double tempo);
    int findBestOffset(const float* input) const;
    void compactInput();

    const int sequenceFrames;
    const int overlapFrames;
    const int seekFrames;

    std::vector<float> input;       // interleaved stereo, frames from inputStart on are pending
    size_t inputStart = 0;
    std::vector<float> output;      // interleaved stereo, frames from outputStart on are ready
    size_t outputStart = 0;
    std::vector<float> overlapTail; // interleaved stereo, the end of the previous sequence
    std::vector<float> tailMono;    // the same tail summed to mono, for matching
    bool haveTail = false;
    double skipRemainder = 0.0;
};

#endif //MELONDS_TIMESTRETCHER_H
