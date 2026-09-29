#ifndef MELONDS_OBOECALLBACK_H
#define MELONDS_OBOECALLBACK_H

#include <oboe/Oboe.h>
#include <fstream>
#include <vector>
#include "MelonInstance.h"
#include "TimeStretcher.h"

class OboeCallback : public oboe::AudioStreamCallback {
private:
    int _volume;
    void (*onErrorCallback)(void);
    std::ostream* _recordingStream;
    float audioSampleFrac;

    // fast-forward: everything the core produced, played faster at the original pitch
    TimeStretcher fastForwardStretcher;
    std::vector<int16_t> fastForwardInput;
    bool fastForwardAudioActive = false;
    double fastForwardArrivalRate = 1.0;

public:
    std::weak_ptr<MelonDSAndroid::MelonInstance> activeInstance;

    OboeCallback(int volume, void (*onErrorCallback)(void)) : OboeCallback(volume, onErrorCallback, nullptr) { };
    OboeCallback(int volume, void (*onErrorCallback)(void), std::ostream* recordingStream);
    oboe::DataCallbackResult onAudioReady(oboe::AudioStream *stream, void *audioData, int32_t numFrames) override;
    void onErrorAfterClose(oboe::AudioStream* stream, oboe::Result result) override;
    
private:
    int getNumSamplesOut(int len);
    void renderFastForward(MelonDSAndroid::MelonInstance& instance, int16_t* audioData, int32_t numFrames);
};


#endif //MELONDS_OBOECALLBACK_H
