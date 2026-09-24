#include "FrameQueue.h"

#include <algorithm>

#include <Platform.h>
#include "VulkanPerfStats.h"

FrameQueue::FrameQueue()
{
    for (auto& frame : frames)
    {
        freeQueue.push(&frame);
    }
}

FrameQueuePolicy FrameQueue::sanitizePolicy(FrameQueuePolicy policy)
{
    policy.MaxBacklogDepth = std::max<u64>(1u, std::min<u64>(policy.MaxBacklogDepth, FRAME_QUEUE_SIZE - 1));
    return policy;
}

// Retires a frame the queue no longer tracks (pending/previous slot). If the
// presentation thread still holds it, park it as orphaned instead of recycling:
// re-entering freeQueue while the presenter is recording or presenting it lets
// the render thread overwrite a frame that is still in flight.
void FrameQueue::retireTrackedFrameLocked(Frame*& slot)
{
    Frame* frame = slot;
    slot = nullptr;
    if (frame == nullptr)
        return;

    frame->queuedAtNs = 0;
    if (frame == presenterHeldFrame)
    {
        orphanedHeldFrame = frame;
        return;
    }
    freeQueue.push(frame);
}

// Returns true when the released frame was orphaned by a resync; it only
// re-enters freeQueue here, once the presenter is done with it.
bool FrameQueue::releaseOrphanedHeldFrameLocked(Frame* frame)
{
    if (frame == nullptr || frame != orphanedHeldFrame)
        return false;

    orphanedHeldFrame = nullptr;
    frame->queuedAtNs = 0;
    freeQueue.push(frame);
    return true;
}

Frame* FrameQueue::getRenderFrame(const FrameQueuePolicy& requestedPolicy)
{
    std::unique_lock lock(frameLock);
    stats.RenderFramesAcquired++;
    const FrameQueuePolicy policy = sanitizePolicy(requestedPolicy);

    if (policy.BlockRenderWhenBacklogged)
    {
        freeFrameReadyCondition.wait(lock, [&] {
            const u64 pendingDepth = static_cast<u64>(presentQueue.size()) + (pendingPresentFrame != nullptr ? 1u : 0u);
            return !freeQueue.empty() && pendingDepth < policy.MaxBacklogDepth;
        });

        Frame* frame = freeQueue.front();
        freeQueue.pop();
        frame->frameId = nextFrameId++;
        frame->queuedAtNs = 0;
        frame->presentTimelineValue = 0;
        return frame;
    }

    if (!freeQueue.empty())
    {
        Frame* frame = freeQueue.front();
        freeQueue.pop();
        frame->frameId = nextFrameId++;
        frame->queuedAtNs = 0;
        frame->presentTimelineValue = 0;
        return frame;
    }

    if (policy.UseLegacyOpenGlQueue)
    {
        if (presentQueue.empty())
        {
            stats.RenderFramesDroppedByPolicy++;
            return nullptr;
        }

        Frame* frame = presentQueue.back();
        presentQueue.pop_back();
        frame->frameId = nextFrameId++;
        frame->queuedAtNs = 0;
        frame->presentTimelineValue = 0;
        stats.PendingFramesStolenForRender++;
        updateBacklogStatsLocked();
        return frame;
    }

    if (policy.AllowStealPending && !presentQueue.empty())
    {
        const u64 nowNs = MelonDSAndroid::PerfNowNs();
        Frame* frame = presentQueue.back();
        presentQueue.pop_back();
        frame->frameId = nextFrameId++;
        frame->presentTimelineValue = 0;
        stats.PendingFramesStolenForRender++;
        stats.PresentFramesDroppedByPolicy++;
        recordDroppedFrameLocked(frame, PresentDropCause::StealForRender, nowNs);
        updateBacklogStatsLocked();
        return frame;
    }

    stats.RenderFramesDroppedByPolicy++;
    return nullptr;
}

Frame* FrameQueue::getPresentFrame(
    const FrameQueuePolicy& requestedPolicy,
    std::optional<std::chrono::time_point<std::chrono::steady_clock>> deadline)
{
    std::unique_lock lock(frameLock);
    const FrameQueuePolicy policy = sanitizePolicy(requestedPolicy);

    // a new acquisition by the presentation thread abandons any prior hold
    if (orphanedHeldFrame != nullptr && orphanedHeldFrame == presenterHeldFrame)
        releaseOrphanedHeldFrameLocked(orphanedHeldFrame);
    presenterHeldFrame = nullptr;

    if (policy.UseLegacyOpenGlQueue)
    {
        if (presentQueue.empty())
        {
            bool hasNewFrame = false;
            if (deadline.has_value())
                hasNewFrame = presentFrameReadyCondition.wait_until(lock, *deadline, [&]{ return !presentQueue.empty(); });

            if (!hasNewFrame)
            {
                if (previousFrame != nullptr)
                {
                    stats.PreviousFrameReused++;
                    presenterHeldFrame = previousFrame;
                }
                return previousFrame;
            }
        }

        if (previousFrame)
        {
            freeQueue.push(previousFrame);
            previousFrame->queuedAtNs = 0;
            previousFrame = nullptr;
        }

        const u64 nowNs = MelonDSAndroid::PerfNowNs();
        Frame* frame = presentQueue.front();
        presentQueue.pop_front();
        stats.PresentFramesReturned++;

        const u64 staleFrameCount = static_cast<u64>(presentQueue.size());
        for (auto f : presentQueue)
        {
            freeQueue.push(f);
            recordDroppedFrameLocked(f, PresentDropCause::Stale, nowNs);
        }
        stats.StaleFramesDropped += staleFrameCount;
        stats.PresentFramesDroppedByPolicy += staleFrameCount;

        presentQueue.clear();
        freeFrameReadyCondition.notify_all();
        previousFrame = frame;
        presenterHeldFrame = frame;
        recordPresentedFrameAgeLocked(frame, nowNs);
        updateBacklogStatsLocked();
        return frame;
    }

    if (presentQueue.empty()) {
        bool hasNewFrame = false;
        if (deadline.has_value())
            hasNewFrame = presentFrameReadyCondition.wait_until(lock, *deadline, [&]{ return !presentQueue.empty(); });

        if (!hasNewFrame)
        {
            if (suppressPreviousFrameReuse || !policy.AllowPreviousFrameReuse)
                return nullptr;
            if (previousFrame != nullptr)
            {
                stats.PreviousFrameReused++;
                presenterHeldFrame = previousFrame;
            }
            return previousFrame;
        }
    }

    if (previousFrame)
    {
        freeQueue.push(previousFrame);
        previousFrame->queuedAtNs = 0;
        previousFrame = nullptr;
    }

    const u64 nowNs = MelonDSAndroid::PerfNowNs();
    Frame* frame = presentQueue.front();
    presentQueue.pop_front();
    stats.PresentFramesReturned++;
    suppressPreviousFrameReuse = false;

    const u64 staleFrameCount = static_cast<u64>(presentQueue.size());
    for (auto f : presentQueue)
    {
        freeQueue.push(f);
        recordDroppedFrameLocked(f, PresentDropCause::Stale, nowNs);
    }
    stats.StaleFramesDropped += staleFrameCount;
    stats.PresentFramesDroppedByPolicy += staleFrameCount;

    presentQueue.clear();
    freeFrameReadyCondition.notify_all();
    previousFrame = frame;
    presenterHeldFrame = frame;
    recordPresentedFrameAgeLocked(frame, nowNs);
    updateBacklogStatsLocked();
    return frame;
}

Frame* FrameQueue::getPresentCandidate(
    const FrameQueuePolicy& requestedPolicy,
    std::optional<std::chrono::time_point<std::chrono::steady_clock>> deadline)
{
    std::unique_lock lock(frameLock);
    const FrameQueuePolicy policy = sanitizePolicy(requestedPolicy);

    // a new acquisition by the presentation thread abandons any prior hold
    if (orphanedHeldFrame != nullptr && orphanedHeldFrame == presenterHeldFrame)
        releaseOrphanedHeldFrameLocked(orphanedHeldFrame);
    presenterHeldFrame = nullptr;

    if (pendingPresentFrame != nullptr)
    {
        presenterHeldFrame = pendingPresentFrame;
        return pendingPresentFrame;
    }

    if (presentQueue.empty())
    {
        bool hasNewFrame = false;
        if (deadline.has_value())
        {
            hasNewFrame = presentFrameReadyCondition.wait_until(lock, *deadline, [&] {
                return !presentQueue.empty() || pendingPresentFrame != nullptr;
            });
        }

        if (pendingPresentFrame != nullptr)
        {
            presenterHeldFrame = pendingPresentFrame;
            return pendingPresentFrame;
        }

        if (!hasNewFrame)
        {
            if (suppressPreviousFrameReuse || !policy.AllowPreviousFrameReuse)
                return nullptr;
            // a newer candidate may already be on one display
            if (previousFrame != nullptr && previousFrame->frameId < highestCandidateFrameId)
                return nullptr;
            if (previousFrame != nullptr)
            {
                stats.PreviousFrameReused++;
                presenterHeldFrame = previousFrame;
            }
            return previousFrame;
        }
    }

    // Never present backwards. A deferred candidate is requeued, and with the
    // backlog preserved across presents it can sit behind newer frames until
    // the queue briefly holds nothing else; presenting it then flashed a
    // picture 10-30 frames old between two current ones (Lufia's intro).
    const u64 dropNowNs = MelonDSAndroid::PerfNowNs();
    Frame* frame = nullptr;
    while (!presentQueue.empty())
    {
        Frame* next = nullptr;
        if (policy.PreferOldestFrame)
        {
            next = presentQueue.back();
            presentQueue.pop_back();
        }
        else
        {
            next = presentQueue.front();
            presentQueue.pop_front();
        }
        if (next->frameId >= highestCandidateFrameId)
        {
            frame = next;
            break;
        }
        freeQueue.push(next);
        recordDroppedFrameLocked(next, PresentDropCause::Stale, dropNowNs);
        stats.StaleFramesDropped++;
        stats.PresentFramesDroppedByPolicy++;
    }
    if (frame == nullptr)
    {
        updateBacklogStatsLocked();
        freeFrameReadyCondition.notify_all();
        return nullptr;
    }
    highestCandidateFrameId = frame->frameId;
    pendingPresentFrame = frame;
    presenterHeldFrame = frame;
    stats.PresentFramesReturned++;
    suppressPreviousFrameReuse = false;

    if (!policy.AllowDropForDeadline && !policy.PreserveBacklogOnPresent)
    {
        const u64 nowNs = MelonDSAndroid::PerfNowNs();
        const u64 staleFrameCount = static_cast<u64>(presentQueue.size());
        for (auto f : presentQueue)
        {
            freeQueue.push(f);
            recordDroppedFrameLocked(f, PresentDropCause::Stale, nowNs);
        }
        stats.StaleFramesDropped += staleFrameCount;
        stats.PresentFramesDroppedByPolicy += staleFrameCount;
        presentQueue.clear();
        freeFrameReadyCondition.notify_all();
    }
    updateBacklogStatsLocked();
    freeFrameReadyCondition.notify_all();
    return frame;
}

Frame* FrameQueue::getReusablePreviousFrame(const FrameQueuePolicy& requestedPolicy)
{
    std::unique_lock lock(frameLock);
    const FrameQueuePolicy policy = sanitizePolicy(requestedPolicy);
    if (suppressPreviousFrameReuse || !policy.AllowPreviousFrameReuse || previousFrame == nullptr)
        return nullptr;

    stats.PreviousFrameReused++;
    presenterHeldFrame = previousFrame;
    return previousFrame;
}

void FrameQueue::recycleRenderFrame(Frame* frame)
{
    std::unique_lock lock(frameLock);
    if (frame == nullptr)
        return;

    frame->queuedAtNs = 0;
    freeQueue.push(frame);
    freeFrameReadyCondition.notify_one();
}

void FrameQueue::commitPresentedFrame(Frame* frame, const FrameQueuePolicy& requestedPolicy)
{
    std::unique_lock lock(frameLock);
    if (frame == nullptr)
        return;

    if (frame == presenterHeldFrame)
        presenterHeldFrame = nullptr;
    if (releaseOrphanedHeldFrameLocked(frame))
        return;

    const FrameQueuePolicy policy = sanitizePolicy(requestedPolicy);
    if (frame != pendingPresentFrame)
    {
        if (frame == previousFrame)
            suppressPreviousFrameReuse = false;
        return;
    }

    if (previousFrame != nullptr && previousFrame != frame)
    {
        freeQueue.push(previousFrame);
        previousFrame->queuedAtNs = 0;
        freeFrameReadyCondition.notify_one();
    }

    previousFrame = frame;
    pendingPresentFrame = nullptr;
    suppressPreviousFrameReuse = false;
    const u64 nowNs = MelonDSAndroid::PerfNowNs();
    recordPresentedFrameAgeLocked(frame, nowNs);

    if (!policy.PreserveBacklogOnPresent)
    {
        for (auto f : presentQueue)
        {
            freeQueue.push(f);
            if (policy.TreatBacklogTrimAsFastForwardSkip)
            {
                f->queuedAtNs = 0;
                stats.FastForwardFramesSkipped++;
            }
            else
            {
                recordDroppedFrameLocked(f, PresentDropCause::Stale, nowNs);
            }
        }
        const u64 staleFrameCount = static_cast<u64>(presentQueue.size());
        if (!policy.TreatBacklogTrimAsFastForwardSkip)
        {
            stats.StaleFramesDropped += staleFrameCount;
            stats.PresentFramesDroppedByPolicy += staleFrameCount;
        }
        presentQueue.clear();
        freeFrameReadyCondition.notify_all();
    }

    dropPendingFramesToBacklogLocked(
        policy.ExpandPreservedBacklogToQueueCapacity && policy.PreserveBacklogOnPresent
            ? FRAME_QUEUE_SIZE - 1
            : policy.MaxBacklogDepth,
        policy.TreatBacklogTrimAsFastForwardSkip);
    updateBacklogStatsLocked();
}

void FrameQueue::deferPresentedFrame(Frame* frame, const FrameQueuePolicy& requestedPolicy)
{
    std::unique_lock lock(frameLock);
    if (frame == nullptr)
        return;

    if (frame == presenterHeldFrame)
        presenterHeldFrame = nullptr;
    if (releaseOrphanedHeldFrameLocked(frame))
        return;

    if (frame != pendingPresentFrame)
        return;

    const FrameQueuePolicy policy = sanitizePolicy(requestedPolicy);
    if (!policy.AllowDropForDeadline)
    {
        if (policy.ReclaimDeferredRealtimeFrameAfterTimeout)
        {
            constexpr u64 kRealtimeDeferredFrameMaxAgeNs = 250'000'000ull;
            const u64 nowNs = MelonDSAndroid::PerfNowNs();
            if (pendingPresentFrame->queuedAtNs != 0
                && nowNs - pendingPresentFrame->queuedAtNs > kRealtimeDeferredFrameMaxAgeNs)
            {
                recordDroppedFrameLocked(pendingPresentFrame, PresentDropCause::Stale, nowNs);
                freeQueue.push(pendingPresentFrame);
                stats.StaleFramesDropped++;
                stats.PresentFramesDroppedByPolicy++;
                pendingPresentFrame = nullptr;
                updateBacklogStatsLocked();
                freeFrameReadyCondition.notify_all();
                return;
            }
        }

        // In realtime mode, don't keep a failed candidate pinned as pending.
        // Requeue it so the next present attempt can pick a fresher frame.
        if (policy.PreferOldestFrame)
            presentQueue.push_front(pendingPresentFrame);
        else
            presentQueue.push_back(pendingPresentFrame);

        pendingPresentFrame = nullptr;
        stats.PresentDeferredByDeadline++;
        updateBacklogStatsLocked();
        return;
    }

    if (policy.AllowDropForDeadline)
    {
        const u64 nowNs = MelonDSAndroid::PerfNowNs();
        if (policy.PreferOldestFrame)
        {
            presentQueue.push_back(pendingPresentFrame);
            stats.PresentDeferredByDeadline++;
        }
        else
        {
            freeQueue.push(pendingPresentFrame);
            stats.PresentFramesDroppedByPolicy++;
            recordDroppedFrameLocked(pendingPresentFrame, PresentDropCause::Deadline, nowNs);
            freeFrameReadyCondition.notify_one();
        }
        pendingPresentFrame = nullptr;
        updateBacklogStatsLocked();
    }
}

void FrameQueue::validateRenderFrame(Frame* frame, int requiredWidth, int requiredHeight, FrameBackend backend)
{
    const EGLDisplay currentDisplay = eglGetCurrentDisplay();
    const EGLContext currentContext = eglGetCurrentContext();
    const bool hasCurrentOpenGlContext = currentDisplay != EGL_NO_DISPLAY && currentContext != EGL_NO_CONTEXT;

    if (frame->backend != backend)
    {
        if (frame->backend == FrameBackend::OpenGlTexture && frame->frameTexture != 0)
        {
            if (hasCurrentOpenGlContext)
                glDeleteTextures(1, &frame->frameTexture);
            frame->frameTexture = 0;
        }

        // keep the frame id: it identifies the acquisition that just handed
        // this frame out. Zeroing it here gave the first frames after a
        // backend switch id 0, which downstream flowed into the packed plane
        // snapshots and made unrelated frames compare as identical.
        frame->backend = backend;
        frame->width = 0;
        frame->height = 0;
        frame->renderTimelineValue = 0;
        frame->presentTimelineValue = 0;
        frame->queuedAtNs = 0;
    }

    if (frame->width != requiredWidth || frame->height != requiredHeight)
    {
        if (backend == FrameBackend::OpenGlTexture)
        {
            if (!hasCurrentOpenGlContext)
            {
                frame->width = 0;
                frame->height = 0;
                return;
            }

            // Update frame texture to have the required size
            if (!frame->frameTexture)
            {
                glGenTextures(1, &frame->frameTexture);
                glBindTexture(GL_TEXTURE_2D, frame->frameTexture);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
            }
            else
            {
                glBindTexture(GL_TEXTURE_2D, frame->frameTexture);
            }
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, requiredWidth, requiredHeight, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
            glBindTexture(GL_TEXTURE_2D, 0);
        }

        frame->width = requiredWidth;
        frame->height = requiredHeight;
    }

    if (backend == FrameBackend::VulkanImage && frame->frameTexture != 0)
    {
        if (hasCurrentOpenGlContext)
            glDeleteTextures(1, &frame->frameTexture);
        frame->frameTexture = 0;
    }

    if (backend == FrameBackend::OpenGlTexture)
        frame->renderTimelineValue = 0;
}

void FrameQueue::pushRenderedFrame(Frame* frame, const FrameQueuePolicy& requestedPolicy)
{
    std::unique_lock lock(frameLock);
    const FrameQueuePolicy policy = sanitizePolicy(requestedPolicy);
    frame->queuedAtNs = MelonDSAndroid::PerfNowNs();
    if (policy.UseLegacyOpenGlQueue)
    {
        presentQueue.push_front(frame);
        stats.RenderFramesQueued++;
        updateBacklogStatsLocked();
        presentFrameReadyCondition.notify_one();
        return;
    }

    dropPendingFramesToBacklogLocked(
        policy.ExpandPreservedBacklogToQueueCapacity && policy.PreserveBacklogOnPresent
            ? FRAME_QUEUE_SIZE - 1
            : (policy.MaxBacklogDepth > 0 ? policy.MaxBacklogDepth - 1 : 0),
        policy.TreatBacklogTrimAsFastForwardSkip);
    presentQueue.push_front(frame);
    stats.RenderFramesQueued++;
    updateBacklogStatsLocked();
    presentFrameReadyCondition.notify_one();
}

void FrameQueue::discardRenderedFrame(Frame* frame)
{
    std::unique_lock lock(frameLock);
    frame->queuedAtNs = 0;
    freeQueue.push(frame);
    stats.RenderFramesDiscarded++;
    freeFrameReadyCondition.notify_one();
}

void FrameQueue::requestPresentationResync()
{
    std::unique_lock lock(frameLock);

    for (auto f : presentQueue)
    {
        f->queuedAtNs = 0;
        freeQueue.push(f);
    }

    presentQueue.clear();
    retireTrackedFrameLocked(pendingPresentFrame);
    retireTrackedFrameLocked(previousFrame);

    // A resync invalidates the presentation contract for all in-flight frames:
    // scale, backend, packed buffers, and 3D source image may all have changed.
    // Reusing the previous frame after this point mixes old frame ownership with
    // the new configuration and reopens flicker/corruption on IR changes.
    suppressPreviousFrameReuse = true;
    updateBacklogStatsLocked();
    freeFrameReadyCondition.notify_all();
}

void FrameQueue::requestFastForwardPresentationTransition()
{
    std::unique_lock lock(frameLock);

    for (auto f : presentQueue)
    {
        f->queuedAtNs = 0;
        freeQueue.push(f);
    }

    presentQueue.clear();
    retireTrackedFrameLocked(pendingPresentFrame);

    suppressPreviousFrameReuse = false;
    updateBacklogStatsLocked();
    freeFrameReadyCondition.notify_all();
}

void FrameQueue::clear()
{
    std::unique_lock lock(frameLock);

    for (auto f : presentQueue)
    {
        f->queuedAtNs = 0;
        freeQueue.push(f);
    }

    presentQueue.clear();
    previousFrame = nullptr;
    pendingPresentFrame = nullptr;
    presenterHeldFrame = nullptr;
    orphanedHeldFrame = nullptr;
    suppressPreviousFrameReuse = false;
    highestCandidateFrameId = 0;
    stats = FrameQueueStats{};
    rebuildFreeQueueLocked();
    freeFrameReadyCondition.notify_all();

    EGLDisplay currentDisplay = eglGetCurrentDisplay();
    const EGLContext currentContext = eglGetCurrentContext();
    const bool hasCurrentOpenGlContext = currentDisplay != EGL_NO_DISPLAY && currentContext != EGL_NO_CONTEXT;
    for (auto& frame : frames)
    {
        if (frame.frameTexture != 0)
        {
            if (hasCurrentOpenGlContext)
                glDeleteTextures(1, &frame.frameTexture);
            frame.frameTexture = 0;
        }

        if (frame.renderFence && currentDisplay != EGL_NO_DISPLAY)
            eglDestroySyncKHR(currentDisplay, frame.renderFence);
        if (frame.presentFence && currentDisplay != EGL_NO_DISPLAY)
            eglDestroySyncKHR(currentDisplay, frame.presentFence);

        frame.backend = FrameBackend::OpenGlTexture;
        frame.frameTexture = 0;
        frame.width = 0;
        frame.height = 0;
        frame.frameId = 0;
        frame.renderFence = 0;
        frame.presentFence = 0;
        frame.renderTimelineValue = 0;
        frame.presentTimelineValue = 0;
        frame.queuedAtNs = 0;
    }
}

FrameQueueStats FrameQueue::takeStatsSnapshotAndReset()
{
    std::unique_lock lock(frameLock);
    stats.CurrentBacklogDepth = static_cast<u64>(presentQueue.size());
    FrameQueueStats snapshot = stats;
    stats = FrameQueueStats{};
    return snapshot;
}

void FrameQueue::updateBacklogStatsLocked()
{
    const u64 backlogDepth = static_cast<u64>(presentQueue.size());
    stats.CurrentBacklogDepth = backlogDepth;
    stats.MaxBacklogDepth = std::max(stats.MaxBacklogDepth, backlogDepth);
}

void FrameQueue::rebuildFreeQueueLocked()
{
    std::queue<Frame*> emptyQueue;
    std::swap(freeQueue, emptyQueue);

    for (auto& frame : frames)
        freeQueue.push(&frame);
}

void FrameQueue::dropPendingFramesToBacklogLocked(u64 maxBacklogDepth, bool treatAsFastForwardSkip)
{
    const u64 nowNs = MelonDSAndroid::PerfNowNs();
    while (static_cast<u64>(presentQueue.size()) > maxBacklogDepth && !presentQueue.empty())
    {
        Frame* frame = presentQueue.back();
        presentQueue.pop_back();
        freeQueue.push(frame);
        if (treatAsFastForwardSkip)
        {
            frame->queuedAtNs = 0;
            stats.FastForwardFramesSkipped++;
        }
        else
        {
            stats.PresentFramesDroppedByPolicy++;
            recordDroppedFrameLocked(frame, PresentDropCause::BacklogTrim, nowNs);
        }
    }
    updateBacklogStatsLocked();
    freeFrameReadyCondition.notify_all();
}

void FrameQueue::recordPresentedFrameAgeLocked(Frame* frame, u64 nowNs)
{
    if (frame == nullptr || frame->queuedAtNs == 0 || nowNs < frame->queuedAtNs)
        return;

    const u64 ageNs = nowNs - frame->queuedAtNs;
    stats.PresentedFrameAgeTotalNs += ageNs;
    stats.PresentedFrameAgeMaxNs = std::max(stats.PresentedFrameAgeMaxNs, ageNs);
    stats.PresentedFrameAgeSamples++;
}

void FrameQueue::recordDroppedFrameLocked(Frame* frame, PresentDropCause cause, u64 nowNs)
{
    if (frame == nullptr)
        return;

    switch (cause)
    {
        case PresentDropCause::Stale:
            stats.PresentDroppedByStale++;
            break;
        case PresentDropCause::StealForRender:
            stats.PresentDroppedBySteal++;
            break;
        case PresentDropCause::Deadline:
            stats.PresentDroppedByDeadline++;
            break;
        case PresentDropCause::BacklogTrim:
            stats.PresentDroppedByBacklogTrim++;
            break;
    }

    if (frame->queuedAtNs != 0 && nowNs >= frame->queuedAtNs)
    {
        const u64 ageNs = nowNs - frame->queuedAtNs;
        stats.DroppedFrameAgeTotalNs += ageNs;
        stats.DroppedFrameAgeMaxNs = std::max(stats.DroppedFrameAgeMaxNs, ageNs);
        stats.DroppedFrameAgeSamples++;
    }

    frame->queuedAtNs = 0;
}
