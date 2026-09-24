package me.magnum.melonds.debug

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Log
import androidx.core.content.edit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import me.magnum.melonds.MelonDSAndroidInterface
import me.magnum.melonds.MelonEmulator
import me.magnum.melonds.domain.model.ControllerConfiguration
import me.magnum.melonds.domain.model.Input
import me.magnum.melonds.domain.model.VideoRenderer
import me.magnum.melonds.impl.emulator.debug.RendererDebugCaptureKind
import me.magnum.melonds.impl.emulator.debug.RendererDebugCapturePresets
import me.magnum.melonds.impl.emulator.debug.RendererDebugCaptureLogger
import me.magnum.melonds.impl.emulator.debug.RendererDebugBridge
import me.magnum.melonds.impl.emulator.debug.RendererDebugCaptureResult
import java.io.File
import java.util.LinkedHashSet
import java.util.Locale

internal class DebugCommandReceiver : BroadcastReceiver() {
    // Returned to the caller instead of success=0/1 when a command produces data (a new
    // receiver instance handles each broadcast, so this is per command). `adb shell am
    // broadcast` prints it as the result data; tools/thor_mcp reads it from there.
    private var resultData: String? = null

    override fun onReceive(context: Context, intent: Intent) {
        val pendingResult = goAsync()
        receiverScope.launch {
            try {
                val success = DebugCommandExecutionLock.withLock {
                    handleIntent(context.applicationContext, intent)
                }
                pendingResult.setResultCode(if (success) RESULT_SUCCESS else RESULT_FAILURE)
                pendingResult.setResultData(resultData ?: "success=${if (success) 1 else 0}")
            } catch (error: Exception) {
                Log.w(TAG, "Debug command failed: action=${intent.action}", error)
                pendingResult.setResultCode(RESULT_FAILURE)
                pendingResult.setResultData("success=0 error=${error.javaClass.simpleName}")
            } finally {
                pendingResult.finish()
            }
        }
    }

    private suspend fun handleIntent(context: Context, intent: Intent): Boolean {
        val entryPoint = DebugCommandEntryPoint.resolve(context)
        return when (intent.action) {
            context.debugCommandAction(ACTION_SET_RENDERER_SUFFIX) -> { handleSetRenderer(entryPoint, intent); true }
            context.debugCommandAction(ACTION_SET_IR_SUFFIX) -> { handleSetInternalResolution(entryPoint, intent); true }
            context.debugCommandAction(ACTION_SET_JIT_SUFFIX) -> { handleSetJit(entryPoint, intent); true }
            context.debugCommandAction(ACTION_SET_BGOBJ_LOG_SUFFIX) -> { handleSetBgObjLog(entryPoint, intent); true }
            context.debugCommandAction(ACTION_SET_LATCH_TRACE_SUFFIX) -> { handleSetLatchTrace(entryPoint, intent); true }
            context.debugCommandAction(ACTION_SET_FILTER_TINT_SUFFIX) -> { handleSetFilterTint(entryPoint, intent); true }
            context.debugCommandAction(ACTION_SET_FAST_FORWARD_SUFFIX) -> { handleSetFastForward(intent); true }
            context.debugCommandAction(ACTION_SET_SLOT2_ANALOG_SUFFIX) -> { handleSetSlot2Analog(intent); true }
            context.debugCommandAction(ACTION_SET_SLOT2_ANALOG_MAPPING_SUFFIX) -> { handleSetSlot2AnalogMapping(entryPoint, intent); true }
            context.debugCommandAction(ACTION_SET_VULKAN_FALLBACKS_SUFFIX) -> { handleSetVulkanFallbacks(intent); true }
            context.debugCommandAction(ACTION_TOUCH_SCREEN_SUFFIX) -> { handleTouchScreen(intent); true }
            context.debugCommandAction(ACTION_PRESS_INPUT_SUFFIX) -> { handlePressInput(intent); true }
            context.debugCommandAction(ACTION_LAUNCH_ROM_SUFFIX) -> handleLaunchRom(context, intent)
            context.debugCommandAction(ACTION_WAIT_ROM_READY_SUFFIX) -> handleWaitRomReady(intent)
            context.debugCommandAction(ACTION_SAVE_STATE_SUFFIX) -> handleSaveState(context, entryPoint, intent)
            context.debugCommandAction(ACTION_LOAD_STATE_SUFFIX) -> handleLoadState(context, entryPoint, intent)
            context.debugCommandAction(ACTION_STEP_FRAME_SUFFIX) -> handleStepFrame(entryPoint, intent)
            context.debugCommandAction(ACTION_STEP_FRAMES_SUFFIX) -> handleStepFrame(entryPoint, intent)
            context.debugCommandAction(ACTION_DUMP_RENDERER_CAPTURE_SUFFIX) -> handleDumpRendererCapture(context, entryPoint, intent)
            context.debugCommandAction(ACTION_GET_PREFERENCES_SUFFIX) -> handleGetPreferences(entryPoint, intent)
            context.debugCommandAction(ACTION_SET_PREFERENCE_SUFFIX) -> handleSetPreference(entryPoint, intent)
            context.debugCommandAction(ACTION_LIST_ROMS_SUFFIX) -> handleListRoms(entryPoint, intent)
            context.debugCommandAction(ACTION_GET_FPS_SUFFIX) -> handleGetFps()
            context.debugCommandAction(ACTION_START_DEV_SERVER_SUFFIX) -> handleDevServer(context, intent, start = true)
            context.debugCommandAction(ACTION_STOP_DEV_SERVER_SUFFIX) -> handleDevServer(context, intent, start = false)
            else -> {
                Log.w(TAG, "Ignored unknown action=${intent.action}")
                false
            }
        }
    }

    private fun handleSetRenderer(entryPoint: DebugCommandEntryPoint, intent: Intent) {
        val rendererName = intent.firstStringExtra(EXTRA_RENDERER, EXTRA_VALUE)
            ?: throw IllegalArgumentException("Missing renderer extra")
        val renderer = parseRenderer(rendererName)
            ?: throw IllegalArgumentException("Unsupported renderer=$rendererName")
        entryPoint.sharedPreferences().edit(commit = true) {
            putString(KEY_VIDEO_RENDERER, renderer.name.lowercase(Locale.US))
        }
        val refreshed = DebugCommandStateStore.requestSettingsRefresh()
        Log.w(TAG, "action=set_renderer renderer=${renderer.name.lowercase(Locale.US)} refreshed=${if (refreshed) 1 else 0}")
    }

    private fun handleSetInternalResolution(entryPoint: DebugCommandEntryPoint, intent: Intent) {
        val scale = intent.firstIntExtra(EXTRA_SCALE, EXTRA_IR, EXTRA_VALUE)
        require(scale in 1..8) { "Unsupported internal resolution=$scale" }
        entryPoint.sharedPreferences().edit(commit = true) {
            putString(KEY_VIDEO_INTERNAL_RESOLUTION, scale.toString())
        }
        val refreshed = DebugCommandStateStore.requestSettingsRefresh()
        Log.w(TAG, "action=set_ir scale=$scale refreshed=${if (refreshed) 1 else 0}")
    }

    private fun handleSetJit(entryPoint: DebugCommandEntryPoint, intent: Intent) {
        val enabled = intent.firstBooleanExtra(EXTRA_ENABLED, EXTRA_VALUE)
            ?: throw IllegalArgumentException("Missing enabled extra")
        entryPoint.sharedPreferences().edit(commit = true) {
            putBoolean(KEY_ENABLE_JIT, enabled)
        }
        val refreshed = DebugCommandStateStore.requestSettingsRefresh()
        Log.w(TAG, "action=set_jit enabled=${if (enabled) 1 else 0} refreshed=${if (refreshed) 1 else 0}")
    }

    private fun handleSetBgObjLog(entryPoint: DebugCommandEntryPoint, intent: Intent) {
        val enabled = intent.firstBooleanExtra(EXTRA_ENABLED, EXTRA_VALUE)
            ?: throw IllegalArgumentException("Missing enabled extra")
        entryPoint.sharedPreferences().edit(commit = true) {
            putBoolean(KEY_RENDERER_DEBUG_BGOBJ_ENABLED, enabled)
        }
        val refreshed = DebugCommandStateStore.requestSettingsRefresh()
        Log.w(TAG, "action=set_bgobj_log enabled=${if (enabled) 1 else 0} refreshed=${if (refreshed) 1 else 0}")
    }

    private fun handleSetLatchTrace(entryPoint: DebugCommandEntryPoint, intent: Intent) {
        val enabled = intent.firstBooleanExtra(EXTRA_ENABLED, EXTRA_VALUE)
            ?: throw IllegalArgumentException("Missing enabled extra")
        entryPoint.sharedPreferences().edit(commit = true) {
            putBoolean(KEY_RENDERER_DEBUG_LATCH_TRACE_ENABLED, enabled)
        }
        val refreshed = DebugCommandStateStore.requestSettingsRefresh()
        Log.w(TAG, "action=set_latch_trace enabled=${if (enabled) 1 else 0} refreshed=${if (refreshed) 1 else 0}")
    }

    private fun handleSetFilterTint(entryPoint: DebugCommandEntryPoint, intent: Intent) {
        val enabled = intent.firstBooleanExtra(EXTRA_ENABLED, EXTRA_VALUE)
            ?: throw IllegalArgumentException("Missing enabled extra")
        entryPoint.sharedPreferences().edit(commit = true) {
            putBoolean(KEY_RENDERER_DEBUG_FILTER_TINT_ENABLED, enabled)
        }
        val refreshed = DebugCommandStateStore.requestSettingsRefresh()
        Log.w(TAG, "action=set_filter_tint enabled=${if (enabled) 1 else 0} refreshed=${if (refreshed) 1 else 0}")
    }

    private fun handleSetFastForward(intent: Intent) {
        val enabled = intent.firstBooleanExtra(EXTRA_ENABLED, EXTRA_VALUE)
            ?: throw IllegalArgumentException("Missing enabled extra")
        MelonEmulator.setFastForwardEnabled(enabled)
        Log.w(TAG, "action=set_fast_forward enabled=${if (enabled) 1 else 0}")
    }

    private fun handleSetSlot2Analog(intent: Intent) {
        val x = intent.firstFloatExtra(EXTRA_X, EXTRA_VALUE_X, EXTRA_VALUE)
            ?: throw IllegalArgumentException("Missing x/value extra")
        val y = intent.firstFloatExtra(EXTRA_Y, EXTRA_VALUE_Y, EXTRA_VALUE)
            ?: throw IllegalArgumentException("Missing y/value extra")
        val clampedX = x.coerceIn(-1f, 1f)
        val clampedY = y.coerceIn(-1f, 1f)
        MelonEmulator.setSlot2AnalogInput(clampedX, clampedY)
        Log.w(TAG, "action=set_slot2_analog x=$clampedX y=$clampedY")
    }

    private fun handleSetSlot2AnalogMapping(entryPoint: DebugCommandEntryPoint, intent: Intent) {
        val settingsRepository = entryPoint.settingsRepository()
        val currentConfiguration = settingsRepository.getControllerConfiguration()
        val currentMapping = currentConfiguration.slot2AnalogMapping

        val nextMapping = currentMapping.copy(
            axisXCode = intent.firstNullableIntExtra(EXTRA_AXIS_X, EXTRA_AXIS, EXTRA_X) ?: currentMapping.axisXCode,
            axisYCode = intent.firstNullableIntExtra(EXTRA_AXIS_Y, EXTRA_AXIS, EXTRA_Y) ?: currentMapping.axisYCode,
            invertX = intent.firstBooleanExtra(EXTRA_INVERT_X) ?: currentMapping.invertX,
            invertY = intent.firstBooleanExtra(EXTRA_INVERT_Y) ?: currentMapping.invertY,
            deadzone = (intent.firstFloatExtra(EXTRA_DEADZONE) ?: currentMapping.deadzone).coerceIn(0f, 1f),
            deviceId = if (intent.hasExtra(EXTRA_DEVICE_ID)) {
                intent.firstNullableIntExtra(EXTRA_DEVICE_ID)?.takeIf { it >= 0 }
            } else {
                currentMapping.deviceId
            },
        )

        val updatedConfiguration = ControllerConfiguration(
            configList = currentConfiguration.inputMapper.map { it.copy() },
            slot2AnalogMapping = nextMapping,
        )
        settingsRepository.setControllerConfiguration(updatedConfiguration)
        val refreshed = DebugCommandStateStore.requestSettingsRefresh()
        Log.w(
            TAG,
            "action=set_slot2_analog_mapping axisX=${nextMapping.axisXCode} axisY=${nextMapping.axisYCode} invertX=${if (nextMapping.invertX) 1 else 0} invertY=${if (nextMapping.invertY) 1 else 0} deadzone=${"%.3f".format(Locale.US, nextMapping.deadzone)} deviceId=${nextMapping.deviceId ?: -1} refreshed=${if (refreshed) 1 else 0}",
        )
    }

    private fun handleSetVulkanFallbacks(intent: Intent) {
        val forceTimelineOff = intent.firstBooleanExtra(EXTRA_TIMELINE_OFF)
            ?: intent.firstBooleanExtra(EXTRA_TIMELINE)?.not()
            ?: false
        val forceDynamicIndexingOff = intent.firstBooleanExtra(EXTRA_DYNAMIC_INDEXING_OFF)
            ?: intent.firstBooleanExtra(EXTRA_DYNAMIC_INDEXING)?.not()
            ?: false
        MelonDSAndroidInterface.setVulkanCompatibilityOverrides(
            disableTimelineSemaphores = forceTimelineOff,
            disableDynamicTextureIndexing = forceDynamicIndexingOff,
        )
        val refreshed = DebugCommandStateStore.requestSettingsRefresh()
        Log.w(
            TAG,
            "action=set_vulkan_fallbacks timelineOff=${if (forceTimelineOff) 1 else 0} dynamicIndexingOff=${if (forceDynamicIndexingOff) 1 else 0} refreshed=${if (refreshed) 1 else 0}",
        )
    }

    private suspend fun handleTouchScreen(intent: Intent) {
        val x = intent.firstNullableIntExtra(EXTRA_X, EXTRA_VALUE_X, EXTRA_VALUE)
            ?.coerceIn(0, 255)
            ?: DEFAULT_TOUCH_X
        val y = intent.firstNullableIntExtra(EXTRA_Y, EXTRA_VALUE_Y)
            ?.coerceIn(0, 191)
            ?: DEFAULT_TOUCH_Y
        val durationMs = (intent.firstNullableIntExtra(EXTRA_DURATION_MS) ?: DEFAULT_TOUCH_DURATION_MS)
            .coerceIn(1, 2_000)
        MelonEmulator.onInputDown(Input.TOUCHSCREEN)
        MelonEmulator.onScreenTouch(x, y)
        delay(durationMs.toLong())
        MelonEmulator.onInputUp(Input.TOUCHSCREEN)
        MelonEmulator.onScreenRelease()
        Log.w(TAG, "action=touch_screen x=$x y=$y durationMs=$durationMs")
    }

    // Presses DS buttons in order, each held long enough for the game to poll it: `--es input A`
    // or `--es input DOWN,DOWN,A`, `--ei duration_ms 120`, `--ei gap_ms 250`.
    private suspend fun handlePressInput(intent: Intent) {
        val names = intent.getStringExtra(EXTRA_INPUT)
            ?.split(',')
            ?.map { it.trim().uppercase(Locale.US) }
            ?.filter { it.isNotEmpty() }
            .orEmpty()
        val durationMs = (intent.firstNullableIntExtra(EXTRA_DURATION_MS) ?: DEFAULT_PRESS_DURATION_MS).coerceIn(16, 2_000)
        val gapMs = (intent.firstNullableIntExtra(EXTRA_GAP_MS) ?: DEFAULT_PRESS_GAP_MS).coerceIn(0, 5_000)
        names.forEachIndexed { index, name ->
            val input = Input.entries.firstOrNull { it.name == name && it.isSystemInput }
                ?: throw IllegalArgumentException("Unknown DS input $name")
            MelonEmulator.onInputDown(input)
            delay(durationMs.toLong())
            MelonEmulator.onInputUp(input)
            if (index < names.lastIndex)
                delay(gapMs.toLong())
        }
        Log.w(TAG, "action=press_input inputs=${names.joinToString(",")} durationMs=$durationMs gapMs=$gapMs")
    }

    private fun handleGetPreferences(entryPoint: DebugCommandEntryPoint, intent: Intent): Boolean {
        resultData = DebugCommands.preferences(entryPoint, intent.getStringExtra(EXTRA_FILTER).orEmpty()).toString()
        return true
    }

    private fun handleSetPreference(entryPoint: DebugCommandEntryPoint, intent: Intent): Boolean {
        val key = intent.getStringExtra(EXTRA_KEY) ?: throw IllegalArgumentException("Missing key")
        val raw = intent.getStringExtra(EXTRA_VALUE) ?: throw IllegalArgumentException("Missing value")
        resultData = DebugCommands.setPreference(entryPoint, key, raw, intent.getStringExtra(EXTRA_TYPE)).toString()
        return true
    }

    private fun handleGetFps(): Boolean {
        resultData = DebugCommands.fps().toString()
        return true
    }

    private suspend fun handleListRoms(entryPoint: DebugCommandEntryPoint, intent: Intent): Boolean {
        resultData = DebugCommands.listRoms(entryPoint, intent.getStringExtra(EXTRA_QUERY).orEmpty()).toString()
        return true
    }

    /**
     * Starts the on-device MCP server for this process's lifetime; `persist=true` also turns
     * the settings toggle on so it comes back with the app. Stopping always turns it off.
     */
    private fun handleDevServer(context: Context, intent: Intent, start: Boolean): Boolean {
        val persist = intent.firstBooleanExtra(EXTRA_PERSIST) ?: false
        when {
            start && persist -> DevServer.setEnabled(context, true)
            start -> DevServer.start(context)
            else -> DevServer.setEnabled(context, false)
        }
        resultData = DevServer.statusJson(context).toString()
        return DevServer.isRunning() == start
    }

    private suspend fun handleLaunchRom(context: Context, intent: Intent): Boolean {
        val romUri = intent.data ?: intent.firstStringExtra(EXTRA_ROM_URI, EXTRA_URI, EXTRA_PATH)?.let { Uri.parse(it) }
            ?: throw IllegalArgumentException("Missing ROM URI. Provide intent data or rom_uri.")
        val waitReady = intent.firstBooleanExtra(EXTRA_WAIT_ROM_READY, EXTRA_WAIT_READY)
            ?: false
        val pauseAfterReady = intent.getBooleanExtra(EXTRA_PAUSE_AFTER, false)
        val requestedTimeoutMs = intent.firstNullableIntExtra(EXTRA_WAIT_TIMEOUT_MS, EXTRA_TIMEOUT_MS)
            ?.coerceAtLeast(1)
            ?: DebugCommands.DEFAULT_ROM_READY_TIMEOUT_MS
        return DebugCommands.launchRom(context, romUri, waitReady, pauseAfterReady, requestedTimeoutMs)
    }

    private suspend fun handleWaitRomReady(intent: Intent): Boolean {
        val pauseAfterReady = intent.getBooleanExtra(EXTRA_PAUSE_AFTER, false)
        val requestedTimeoutMs = intent.firstNullableIntExtra(EXTRA_WAIT_TIMEOUT_MS, EXTRA_TIMEOUT_MS)
            ?.coerceAtLeast(1)
            ?: DebugCommands.DEFAULT_ROM_READY_TIMEOUT_MS
        val timeoutMs = requestedTimeoutMs.coerceAtMost(DebugCommands.MAX_RECEIVER_WAIT_TIMEOUT_MS)
        val ready = DebugCommandStateStore.waitForRunningRom(timeoutMs.toLong())
        if (ready) {
            DebugCommands.setDebugPause(pauseAfterReady)
        }
        Log.w(
            TAG,
            "action=wait_rom_ready ready=${if (ready) 1 else 0} pauseAfter=${if (pauseAfterReady) 1 else 0} timeoutMs=$timeoutMs requestedTimeoutMs=$requestedTimeoutMs",
        )
        return ready
    }

    private suspend fun handleLoadState(
        context: Context,
        entryPoint: DebugCommandEntryPoint,
        intent: Intent,
    ): Boolean {
        val waitReady = intent.firstBooleanExtra(EXTRA_WAIT_ROM_READY, EXTRA_WAIT_READY)
            ?: true
        val requestedTimeoutMs = intent.firstNullableIntExtra(EXTRA_WAIT_TIMEOUT_MS, EXTRA_TIMEOUT_MS)
            ?.coerceAtLeast(1)
            ?: DebugCommands.DEFAULT_ROM_READY_TIMEOUT_MS
        val pauseAfterLoad = intent.getBooleanExtra(EXTRA_PAUSE_AFTER, false)
        return DebugCommands.loadState(
            context = context,
            entryPoint = entryPoint,
            target = intent.stateTarget(),
            waitReady = waitReady,
            requestedTimeoutMs = requestedTimeoutMs,
            pauseAfterLoad = pauseAfterLoad,
        ).success
    }

    private suspend fun handleSaveState(
        context: Context,
        entryPoint: DebugCommandEntryPoint,
        intent: Intent,
    ): Boolean {
        val pauseAfterSave = intent.getBooleanExtra(EXTRA_PAUSE_AFTER, false)
        return DebugCommands.saveState(context, entryPoint, intent.stateTarget(), pauseAfterSave).success
    }

    private fun Intent.stateTarget(): DebugCommands.StateTarget {
        return DebugCommands.StateTarget(
            pathOrUri = firstStringExtra(EXTRA_PATH, EXTRA_URI),
            slot = firstNullableIntExtra(EXTRA_SLOT, EXTRA_VALUE),
            romUri = firstStringExtra(EXTRA_ROM_URI),
        )
    }

    private suspend fun handleStepFrame(
        entryPoint: DebugCommandEntryPoint,
        intent: Intent,
    ): Boolean {
        val frames = intent.firstNullableIntExtra(EXTRA_STEP_FRAMES, EXTRA_FRAMES, EXTRA_VALUE)
            ?.coerceAtLeast(1)
            ?: 1
        val timeoutMs = intent.firstNullableIntExtra(EXTRA_TIMEOUT_MS, EXTRA_DURATION_MS, EXTRA_RESUME_MS)
            ?.coerceAtLeast(1)
            ?: 5_000
        val renderer = entryPoint.settingsRepository().getCurrentVideoRenderer()
        val startFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()

        DebugCommandStateStore.setDebugPauseHeld(false)
        MelonEmulator.resumeEmulation()
        waitForRendererFrameOrTimeout(
            renderer = renderer,
            startFrame = startFrame,
            resumeFrames = frames,
            timeoutMs = timeoutMs.toLong(),
        )
        MelonEmulator.pauseEmulation()
        waitForRendererReadyOrTimeout(
            renderer = renderer,
            minFrame = RendererDebugBridge.getCurrentFrameIndexForDebug(),
            timeoutMs = timeoutMs.toLong(),
        )
        DebugCommandStateStore.setDebugPauseHeld(true)

        val endFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
        val ready = renderer != VideoRenderer.VULKAN || RendererDebugBridge.isCurrentFrameReadyForDebug()
        Log.w(
            TAG,
            "action=step_frame renderer=${renderer.name.lowercase(Locale.US)} frames=$frames startFrame=$startFrame endFrame=$endFrame ready=${if (ready) 1 else 0}",
        )
        return ready
    }

    private suspend fun handleDumpRendererCapture(
        context: Context,
        entryPoint: DebugCommandEntryPoint,
        intent: Intent,
    ): Boolean {
        entryPoint.sharedPreferences().edit(commit = true) {
            putBoolean(KEY_RENDERER_DEBUG_TOOLS_ENABLED, true)
        }
        val refreshed = DebugCommandStateStore.requestSettingsRefresh()
        if (refreshed) {
            delay(350L)
        }

        val renderer = entryPoint.settingsRepository().getCurrentVideoRenderer()
        val outputDir = File(context.cacheDir, "renderer-debug-captures")
        val pauseWasHeld = DebugCommandStateStore.isDebugPauseHeld()
        val resumeMs = intent.firstNullableIntExtra(EXTRA_RESUME_MS, EXTRA_DURATION_MS)?.coerceAtLeast(0) ?: 0
        val resumeFrames = intent.firstNullableIntExtra(EXTRA_RESUME_FRAMES, EXTRA_FRAMES)?.coerceAtLeast(0) ?: 0
        val burstCount = intent.firstNullableIntExtra(EXTRA_BURST_COUNT, EXTRA_CAPTURE_COUNT)?.coerceAtLeast(1) ?: 1
        val burstStepMs = intent.firstNullableIntExtra(EXTRA_BURST_STEP_MS, EXTRA_STEP_MS)?.coerceAtLeast(0)
            ?: if (burstCount > 1) 0 else resumeMs
        val burstStepFrames = intent.firstNullableIntExtra(EXTRA_BURST_STEP_FRAMES, EXTRA_STEP_FRAMES)?.coerceAtLeast(0)
            ?: if (burstCount > 1) 1 else resumeFrames
        val burstLive = intent.firstBooleanExtra(EXTRA_BURST_LIVE, EXTRA_LIVE_BURST) ?: false
        val captureIdBase = intent.firstStringExtra(EXTRA_CAPTURE_ID_BASE, EXTRA_CAPTURE_ID)
            ?.takeIf { it.isNotBlank() }
        val captureKinds = parseCaptureKinds(
            rawKinds = intent.firstStringExtra(EXTRA_CAPTURE_KINDS, EXTRA_KINDS),
            defaultKinds = if (burstCount > 1) {
                setOf(RendererDebugCaptureKind.SCREEN_FRAME)
            } else {
                RendererDebugCaptureKind.allKinds
            },
        )
        val captureKindsFirst = parseCaptureKinds(
            rawKinds = intent.firstStringExtra(EXTRA_CAPTURE_KINDS_FIRST, EXTRA_FIRST_KINDS),
            defaultKinds = captureKinds,
        )
        val captureKindsRest = parseCaptureKinds(
            rawKinds = intent.firstStringExtra(EXTRA_CAPTURE_KINDS_REST, EXTRA_REST_KINDS),
            defaultKinds = captureKinds,
        )
        val captureOutputDir = if (burstCount > 1) {
            File(outputDir, "burst_${System.currentTimeMillis()}").apply { mkdirs() }
        } else {
            outputDir
        }
        val results = try {
            if (burstLive) {
                performLiveBurstCapture(
                    renderer = renderer,
                    pauseWasHeld = pauseWasHeld,
                    resumeMs = resumeMs,
                    resumeFrames = resumeFrames,
                    burstCount = burstCount,
                    burstStepMs = burstStepMs,
                burstStepFrames = burstStepFrames,
                captureOutputDir = captureOutputDir,
                captureIdBase = captureIdBase,
                captureKindsFirst = captureKindsFirst,
                captureKindsRest = captureKindsRest,
            )
        } else {
                performPausedBurstCapture(
                    renderer = renderer,
                    pauseWasHeld = pauseWasHeld,
                    resumeMs = resumeMs,
                    resumeFrames = resumeFrames,
                    burstCount = burstCount,
                    burstStepMs = burstStepMs,
                burstStepFrames = burstStepFrames,
                captureOutputDir = captureOutputDir,
                captureIdBase = captureIdBase,
                captureKindsFirst = captureKindsFirst,
                captureKindsRest = captureKindsRest,
            )
            }
        } finally {
            if (pauseWasHeld) {
                DebugCommandStateStore.setDebugPauseHeld(true)
                MelonEmulator.pauseEmulation()
            } else {
                DebugCommandStateStore.setDebugPauseHeld(false)
                MelonEmulator.resumeEmulation()
            }
        }
        if (results.size > 1) {
            File(captureOutputDir, "burst_manifest.txt").writeText(
                buildString {
                    appendLine("renderer=${renderer.name.lowercase(Locale.US)}")
                    appendLine("captures=${results.size}")
                    appendLine("liveBurst=${if (burstLive) 1 else 0}")
                    appendLine("stepFrames=$burstStepFrames")
                    appendLine("stepMs=$burstStepMs")
                    appendLine("captureKindsFirst=${captureKindsFirst.joinToString(separator = ",") { it.name.lowercase(Locale.US) }}")
                    appendLine("captureKindsRest=${captureKindsRest.joinToString(separator = ",") { it.name.lowercase(Locale.US) }}")
                    results.forEachIndexed { index, result ->
                        appendLine("capture[$index]=${result.captureId} success=${if (result.success) 1 else 0}")
                    }
                },
            )
        }
        val successCount = results.count { it.success }
        val firstCaptureId = results.firstOrNull()?.captureId ?: "none"
        Log.w(
            TAG,
            "action=dump_renderer_capture renderer=${renderer.name.lowercase(Locale.US)} refreshed=${if (refreshed) 1 else 0} paused=${if (pauseWasHeld) 1 else 0} liveBurst=${if (burstLive) 1 else 0} resumeMs=$resumeMs resumeFrames=$resumeFrames burstCount=$burstCount burstStepMs=$burstStepMs burstStepFrames=$burstStepFrames captureKindsFirst=${captureKindsFirst.joinToString(separator = ",") { it.name.lowercase(Locale.US) }} captureKindsRest=${captureKindsRest.joinToString(separator = ",") { it.name.lowercase(Locale.US) }} captureId=$firstCaptureId success=$successCount/${results.size} outputDir=${captureOutputDir.absolutePath}",
        )
        return successCount == results.size
    }

    private suspend fun performPausedBurstCapture(
        renderer: VideoRenderer,
        pauseWasHeld: Boolean,
        resumeMs: Int,
        resumeFrames: Int,
        burstCount: Int,
        burstStepMs: Int,
        burstStepFrames: Int,
        captureOutputDir: File,
        captureIdBase: String?,
        captureKindsFirst: Set<RendererDebugCaptureKind>,
        captureKindsRest: Set<RendererDebugCaptureKind>,
    ): List<RendererDebugCaptureResult> {
        if (!pauseWasHeld) {
            MelonEmulator.pauseEmulation()
        } else if (resumeMs > 0 || resumeFrames > 0) {
            DebugCommandStateStore.setDebugPauseHeld(false)
            val startFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
            if (shouldPrepareRendererSnapshot(renderer, captureKindsFirst)) {
                RendererDebugBridge.requestPreparedRendererSnapshot()
            }
            MelonEmulator.resumeEmulation()
            waitForRendererFrameOrTimeout(
                renderer = renderer,
                startFrame = startFrame,
                resumeFrames = resumeFrames,
                timeoutMs = resumeMs.toLong(),
            )
            MelonEmulator.pauseEmulation()
            waitForRendererReadyOrTimeout(
                renderer = renderer,
                minFrame = RendererDebugBridge.getCurrentFrameIndexForDebug(),
                timeoutMs = resumeMs.coerceAtLeast(1_000).toLong(),
            )
        }

        val captureBaseId = captureIdBase ?: java.lang.Long.toHexString(System.currentTimeMillis())
        return buildList<RendererDebugCaptureResult> {
            repeat(burstCount) { index ->
                val captureIdOverride = if (burstCount > 1) {
                    "${captureBaseId}_frame_${index.toString().padStart(4, '0')}"
                } else {
                    captureBaseId.takeIf { captureIdBase != null }
                }
                waitForRendererReadyOrTimeout(
                    renderer = renderer,
                    minFrame = RendererDebugBridge.getCurrentFrameIndexForDebug(),
                    timeoutMs = 1_000L,
                )
                add(
                    RendererDebugCaptureLogger.dumpPauseMenuCapture(
                        configuredRenderer = renderer,
                        outputDir = captureOutputDir,
                        captureIdOverride = captureIdOverride,
                        captureKinds = if (index == 0) captureKindsFirst else captureKindsRest,
                        freezeRendererSnapshot = true,
                    ),
                )
                if (index + 1 < burstCount) {
                    DebugCommandStateStore.setDebugPauseHeld(false)
                    val startFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
                    if (shouldPrepareRendererSnapshot(renderer, captureKindsRest)) {
                        RendererDebugBridge.requestPreparedRendererSnapshot()
                    }
                    MelonEmulator.resumeEmulation()
                    waitForRendererFrameOrTimeout(
                        renderer = renderer,
                        startFrame = startFrame,
                        resumeFrames = burstStepFrames,
                        timeoutMs = burstStepMs.toLong(),
                    )
                    MelonEmulator.pauseEmulation()
                    waitForRendererReadyOrTimeout(
                        renderer = renderer,
                        minFrame = RendererDebugBridge.getCurrentFrameIndexForDebug(),
                        timeoutMs = burstStepMs.coerceAtLeast(1_000).toLong(),
                    )
                }
            }
        }
    }

    private suspend fun performLiveBurstCapture(
        renderer: VideoRenderer,
        pauseWasHeld: Boolean,
        resumeMs: Int,
        resumeFrames: Int,
        burstCount: Int,
        burstStepMs: Int,
        burstStepFrames: Int,
        captureOutputDir: File,
        captureIdBase: String?,
        captureKindsFirst: Set<RendererDebugCaptureKind>,
        captureKindsRest: Set<RendererDebugCaptureKind>,
    ): List<RendererDebugCaptureResult> {
        if (
            captureKindsFirst == captureKindsRest
            && !requiresPausedBurstCapture(captureKindsFirst)
            && !requiresPausedBurstCapture(captureKindsRest)
        ) {
            DebugCommandStateStore.setDebugPauseHeld(false)
            MelonEmulator.resumeEmulation()
            val captureBaseId = captureIdBase ?: java.lang.Long.toHexString(System.currentTimeMillis())
            val timeoutMs = when {
                burstStepMs > 0 -> burstStepMs.toLong() * burstCount.toLong() + 5_000L
                else -> (burstCount.toLong() * maxOf(burstStepFrames, 1).toLong() * 1_000L) / 24L + 5_000L
            }
            return RendererDebugCaptureLogger.dumpDenseScreenBurstCapture(
                configuredRenderer = renderer,
                outputDir = captureOutputDir,
                captureIdBase = captureBaseId,
                burstCount = burstCount,
                burstStepFrames = burstStepFrames,
                timeoutMs = timeoutMs,
                captureKinds = captureKindsFirst,
            )
        }

        DebugCommandStateStore.setDebugPauseHeld(false)
        MelonEmulator.resumeEmulation()

        val initialStepFrames = if (resumeFrames > 0) resumeFrames else 0
        val initialStepMs = if (resumeMs > 0) resumeMs else 0
        if (initialStepFrames > 0 || initialStepMs > 0) {
            val startFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
            if (shouldPrepareRendererSnapshot(renderer, captureKindsFirst)) {
                RendererDebugBridge.requestPreparedRendererSnapshot()
            }
            waitForRendererFrameOrTimeout(
                renderer = renderer,
                startFrame = startFrame,
                resumeFrames = initialStepFrames,
                timeoutMs = initialStepMs.toLong(),
            )
        }

        val captureBaseId = captureIdBase ?: java.lang.Long.toHexString(System.currentTimeMillis())
        return buildList<RendererDebugCaptureResult> {
            var lastObservedFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
            if (burstCount > 0 && requiresPausedBurstCapture(captureKindsFirst)) {
                MelonEmulator.pauseEmulation()
                add(
                    RendererDebugCaptureLogger.dumpPauseMenuCapture(
                        configuredRenderer = renderer,
                        outputDir = captureOutputDir,
                        captureIdOverride = if (burstCount > 1) {
                            "${captureBaseId}_frame_${"0000"}"
                        } else {
                            captureBaseId.takeIf { captureIdBase != null }
                        },
                        captureKinds = captureKindsFirst,
                        freezeRendererSnapshot = true,
                    ),
                )
                DebugCommandStateStore.setDebugPauseHeld(false)
                MelonEmulator.resumeEmulation()
                lastObservedFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
            }

            val startIndex = if (burstCount > 0 && requiresPausedBurstCapture(captureKindsFirst)) 1 else 0
            for (index in startIndex until burstCount) {
                if (index > 0 || startIndex > 0) {
                    if (shouldPrepareRendererSnapshot(
                            renderer = renderer,
                            captureKinds = if (index == 0) captureKindsFirst else captureKindsRest,
                        )
                    ) {
                        RendererDebugBridge.requestPreparedRendererSnapshot()
                    }
                    waitForRendererAdvanceOrTimeout(
                        renderer = renderer,
                        lastObservedFrame = lastObservedFrame,
                        advanceFrames = burstStepFrames,
                        timeoutMs = burstStepMs.toLong(),
                    )
                }
                val captureIdOverride = if (burstCount > 1) {
                    "${captureBaseId}_frame_${index.toString().padStart(4, '0')}"
                } else {
                    captureBaseId.takeIf { captureIdBase != null }
                }
                add(
                    RendererDebugCaptureLogger.dumpPauseMenuCapture(
                        configuredRenderer = renderer,
                        outputDir = captureOutputDir,
                        captureIdOverride = captureIdOverride,
                        captureKinds = if (index == 0) captureKindsFirst else captureKindsRest,
                        freezeRendererSnapshot = shouldFreezeRendererSnapshot(
                            renderer = renderer,
                            captureKinds = if (index == 0) captureKindsFirst else captureKindsRest,
                        ),
                    ),
                )
                lastObservedFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
            }
        }
    }

    private fun parseRenderer(value: String): VideoRenderer? {
        return when (value.trim().lowercase(Locale.US)) {
            "software", "soft" -> VideoRenderer.SOFTWARE
            "opengl", "gl" -> VideoRenderer.OPENGL
            "vulkan", "vk" -> VideoRenderer.VULKAN
            else -> null
        }
    }

    private fun parseCaptureKinds(
        rawKinds: String?,
        defaultKinds: Set<RendererDebugCaptureKind>,
    ): Set<RendererDebugCaptureKind> {
        val value = rawKinds?.trim().orEmpty()
        if (value.isEmpty()) {
            return defaultKinds
        }

        val parsed = LinkedHashSet<RendererDebugCaptureKind>()
        value.split(',')
            .map { it.trim().lowercase(Locale.US) }
            .filter { it.isNotEmpty() }
            .forEach { token ->
                when (token) {
                    "all" -> parsed.addAll(RendererDebugCaptureKind.allKinds)
                    "vulkanexact", "vulkan_exact", "vulkan-exact", "exactframe", "exact_frame", "exact-frame" ->
                        parsed.addAll(RendererDebugCapturePresets.vulkanExactFrame)
                    "screen", "screenframe" -> parsed.add(RendererDebugCaptureKind.SCREEN_FRAME)
                    "packed" -> {
                        parsed.add(RendererDebugCaptureKind.PACKED_TOP_PRIMARY)
                        parsed.add(RendererDebugCaptureKind.PACKED_BOTTOM_PRIMARY)
                    }
                    "packedtop", "packed_top", "packedtopprimary" -> parsed.add(RendererDebugCaptureKind.PACKED_TOP_PRIMARY)
                    "packedbottom", "packed_bottom", "packedbottomprimary" -> parsed.add(RendererDebugCaptureKind.PACKED_BOTTOM_PRIMARY)
                    "packedtopplane1", "packed_top_plane1" -> parsed.add(RendererDebugCaptureKind.PACKED_TOP_PLANE1)
                    "packedtopcontrol", "packed_top_control" -> parsed.add(RendererDebugCaptureKind.PACKED_TOP_CONTROL)
                    "packedbottomplane1", "packed_bottom_plane1" -> parsed.add(RendererDebugCaptureKind.PACKED_BOTTOM_PLANE1)
                    "packedbottomcontrol", "packed_bottom_control" -> parsed.add(RendererDebugCaptureKind.PACKED_BOTTOM_CONTROL)
                    "capture3dsource", "capture3dsource", "capture3dsourceds", "capture3dsourceframe" ->
                        parsed.add(RendererDebugCaptureKind.CAPTURE3D_SOURCE_DS_FRAME)
                    "capturelinemask", "capturelineuses3dmask", "capture_line_uses_3d_mask" ->
                        parsed.add(RendererDebugCaptureKind.CAPTURE_LINE_USES_3D_MASK)
                    "comp4top", "comp4_top", "comp4topplaceholder", "comp4_top_placeholder" ->
                        parsed.add(RendererDebugCaptureKind.COMP4_TOP_PLACEHOLDER)
                    "comp4bottom", "comp4_bottom", "comp4bottomplaceholder", "comp4_bottom_placeholder" ->
                        parsed.add(RendererDebugCaptureKind.COMP4_BOTTOM_PLACEHOLDER)
                    "capturefallback", "capturefallbackmask", "capture_fallback_mask", "fallbackmask" ->
                        parsed.add(RendererDebugCaptureKind.CAPTURE_FALLBACK_MASK)
                    "softpackedmeta", "softpackedframemeta", "soft_packed_frame_meta", "softpackedframejson" ->
                        parsed.add(RendererDebugCaptureKind.SOFT_PACKED_FRAME_META_JSON)
                    "renderer3d", "3d", "renderer3dframe" -> parsed.add(RendererDebugCaptureKind.RENDERER3D_FRAME)
                    "capture3d", "3dcapture", "renderer3dcapture", "renderer3dcaptureframe" -> parsed.add(RendererDebugCaptureKind.RENDERER3D_CAPTURE_FRAME)
                    "depth", "renderer3ddepth" -> parsed.add(RendererDebugCaptureKind.RENDERER3D_DEPTH)
                    "attr", "attributes", "renderer3dattr" -> parsed.add(RendererDebugCaptureKind.RENDERER3D_ATTR)
                    "coverage", "renderer3dcoverage" -> parsed.add(RendererDebugCaptureKind.RENDERER3D_COVERAGE)
                    else -> throw IllegalArgumentException("Unsupported capture kind=$token")
                }
            }
        return if (parsed.isEmpty()) defaultKinds else parsed
    }

    private suspend fun waitForRendererFrameOrTimeout(
        renderer: VideoRenderer,
        startFrame: Int,
        resumeFrames: Int,
        timeoutMs: Long,
    ) {
        val effectiveTimeoutMs = when {
            timeoutMs > 0L -> timeoutMs
            resumeFrames > 0 -> 5_000L
            else -> 0L
        }

        if (effectiveTimeoutMs <= 0L) {
            return
        }

        val targetFrame = if (resumeFrames > 0 && startFrame >= 0) {
            startFrame + resumeFrames
        } else {
            Int.MIN_VALUE
        }
        if (targetFrame == Int.MIN_VALUE) {
            delay(effectiveTimeoutMs)
            return
        }
        val deadlineAt = System.nanoTime() + effectiveTimeoutMs * 1_000_000L
        while (System.nanoTime() < deadlineAt) {
            val currentFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
            val hasReachedTargetFrame = currentFrame >= targetFrame
            val rendererReady = renderer != VideoRenderer.VULKAN || RendererDebugBridge.isCurrentFrameReadyForDebug()
            if (hasReachedTargetFrame && rendererReady) {
                return
            }
            delay(8L)
        }
    }

    private suspend fun waitForRendererReadyOrTimeout(
        renderer: VideoRenderer,
        minFrame: Int,
        timeoutMs: Long,
    ) {
        if (renderer != VideoRenderer.VULKAN)
            return

        val effectiveTimeoutMs = timeoutMs.coerceAtLeast(1L)
        val deadlineAt = System.nanoTime() + effectiveTimeoutMs * 1_000_000L
        while (System.nanoTime() < deadlineAt) {
            val currentFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
            if (currentFrame >= minFrame && RendererDebugBridge.isCurrentFrameReadyForDebug()) {
                return
            }
            delay(8L)
        }
    }

    private suspend fun waitForRendererAdvanceOrTimeout(
        renderer: VideoRenderer,
        lastObservedFrame: Int,
        advanceFrames: Int,
        timeoutMs: Long,
    ) {
        val effectiveTimeoutMs = when {
            timeoutMs > 0L -> timeoutMs
            advanceFrames > 0 -> 5_000L
            else -> 0L
        }

        if (effectiveTimeoutMs <= 0L) {
            return
        }

        val currentFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
        val targetAdvance = if (advanceFrames > 0) advanceFrames else 1
        val referenceFrame = maxOf(lastObservedFrame, currentFrame)
        val targetFrame = if (referenceFrame >= 0) {
            referenceFrame + targetAdvance
        } else {
            Int.MIN_VALUE
        }

        if (targetFrame == Int.MIN_VALUE) {
            delay(effectiveTimeoutMs)
            return
        }

        val deadlineAt = System.nanoTime() + effectiveTimeoutMs * 1_000_000L
        while (System.nanoTime() < deadlineAt) {
            val nextFrame = RendererDebugBridge.getCurrentFrameIndexForDebug()
            val hasReachedTargetFrame = nextFrame >= targetFrame
            val rendererReady = renderer != VideoRenderer.VULKAN || RendererDebugBridge.isCurrentFrameReadyForDebug()
            if (hasReachedTargetFrame && rendererReady) {
                return
            }
            delay(8L)
        }
    }

    private fun requiresPausedBurstCapture(captureKinds: Set<RendererDebugCaptureKind>): Boolean {
        return captureKinds.any {
            it != RendererDebugCaptureKind.SCREEN_FRAME
                && it != RendererDebugCaptureKind.PACKED_TOP_PRIMARY
                && it != RendererDebugCaptureKind.PACKED_BOTTOM_PRIMARY
                && it != RendererDebugCaptureKind.RENDERER3D_CAPTURE_FRAME
        }
    }

    private fun shouldFreezeRendererSnapshot(
        renderer: VideoRenderer,
        captureKinds: Set<RendererDebugCaptureKind>,
    ): Boolean {
        if (renderer != VideoRenderer.VULKAN) {
            return true
        }
        return captureKinds.any { it != RendererDebugCaptureKind.SCREEN_FRAME }
    }

    private fun shouldPrepareRendererSnapshot(
        renderer: VideoRenderer,
        captureKinds: Set<RendererDebugCaptureKind>,
    ): Boolean {
        if (renderer != VideoRenderer.OPENGL) {
            return false
        }
        return captureKinds.any {
            it == RendererDebugCaptureKind.RENDERER3D_FRAME
                || it == RendererDebugCaptureKind.RENDERER3D_CAPTURE_FRAME
                || it == RendererDebugCaptureKind.RENDERER3D_DEPTH
                || it == RendererDebugCaptureKind.RENDERER3D_ATTR
                || it == RendererDebugCaptureKind.RENDERER3D_COVERAGE
        }
    }

    private fun Intent.firstStringExtra(vararg keys: String): String? {
        return keys.firstNotNullOfOrNull { key ->
            getStringExtra(key)?.takeIf { value -> value.isNotBlank() }
        }
    }

    private fun Intent.firstIntExtra(vararg keys: String): Int {
        return firstNullableIntExtra(*keys)
            ?: throw IllegalArgumentException("Missing integer extra. Tried keys=${keys.joinToString()}")
    }

    private fun Intent.firstNullableIntExtra(vararg keys: String): Int? {
        keys.forEach { key ->
            if (!hasExtra(key)) {
                return@forEach
            }

            val raw = extras?.get(key)
            when (raw) {
                is Int -> return raw
                is String -> raw.toIntOrNull()?.let { return it }
            }
        }

        return null
    }

    private fun Intent.firstBooleanExtra(vararg keys: String): Boolean? {
        keys.forEach { key ->
            if (!hasExtra(key)) {
                return@forEach
            }

            val raw = extras?.get(key)
            when (raw) {
                is Boolean -> return raw
                is String -> when (raw.trim().lowercase(Locale.US)) {
                    "1", "true", "on", "yes", "enabled" -> return true
                    "0", "false", "off", "no", "disabled" -> return false
                }
                is Int -> return raw != 0
            }
        }

        return null
    }

    private fun Intent.firstFloatExtra(vararg keys: String): Float? {
        keys.forEach { key ->
            if (!hasExtra(key)) {
                return@forEach
            }

            val raw = extras?.get(key)
            when (raw) {
                is Float -> return raw
                is Double -> return raw.toFloat()
                is Int -> return raw.toFloat()
                is String -> raw.toFloatOrNull()?.let { return it }
            }
        }

        return null
    }

    private companion object {
        private const val TAG = "DebugCommand"
        private const val RESULT_FAILURE = 0
        private const val RESULT_SUCCESS = 1
        private const val KEY_VIDEO_RENDERER = "video_renderer"
        private const val KEY_VIDEO_INTERNAL_RESOLUTION = "video_internal_resolution"
        private const val KEY_ENABLE_JIT = "enable_jit"
        private const val KEY_RENDERER_DEBUG_TOOLS_ENABLED = "video_renderer_debug_tools_enabled"
        private const val KEY_RENDERER_DEBUG_BGOBJ_ENABLED = "video_renderer_debug_bgobj_enabled"
        private const val KEY_RENDERER_DEBUG_LATCH_TRACE_ENABLED = "video_renderer_debug_latch_trace_enabled"
        private const val KEY_RENDERER_DEBUG_FILTER_TINT_ENABLED = "video_renderer_debug_filter_tint_enabled"

        private const val EXTRA_RENDERER = "renderer"
        private const val EXTRA_SCALE = "scale"
        private const val EXTRA_IR = "ir"
        private const val EXTRA_ENABLED = "enabled"
        private const val EXTRA_X = "x"
        private const val EXTRA_Y = "y"
        private const val EXTRA_VALUE_X = "value_x"
        private const val EXTRA_VALUE_Y = "value_y"
        private const val EXTRA_AXIS = "axis"
        private const val EXTRA_AXIS_X = "axis_x"
        private const val EXTRA_AXIS_Y = "axis_y"
        private const val EXTRA_INVERT_X = "invert_x"
        private const val EXTRA_INVERT_Y = "invert_y"
        private const val EXTRA_DEADZONE = "deadzone"
        private const val EXTRA_DEVICE_ID = "device_id"
        private const val EXTRA_TIMELINE = "timeline"
        private const val EXTRA_TIMELINE_OFF = "timeline_off"
        private const val EXTRA_DYNAMIC_INDEXING = "dynamic_indexing"
        private const val EXTRA_DYNAMIC_INDEXING_OFF = "dynamic_indexing_off"
        private const val EXTRA_SLOT = "slot"
        private const val EXTRA_PATH = "path"
        private const val EXTRA_URI = "uri"
        private const val EXTRA_ROM_URI = "rom_uri"
        private const val EXTRA_PAUSE_AFTER = "pause_after"
        private const val EXTRA_WAIT_ROM_READY = "wait_rom_ready"
        private const val EXTRA_WAIT_READY = "wait_ready"
        private const val EXTRA_WAIT_TIMEOUT_MS = "wait_timeout_ms"
        private const val EXTRA_RESUME_MS = "resume_ms"
        private const val EXTRA_RESUME_FRAMES = "resume_frames"
        private const val EXTRA_DURATION_MS = "duration_ms"
        private const val EXTRA_INPUT = "input"
        private const val EXTRA_GAP_MS = "gap_ms"
        private const val EXTRA_TIMEOUT_MS = "timeout_ms"
        private const val EXTRA_FRAMES = "frames"
        private const val EXTRA_BURST_COUNT = "burst_count"
        private const val EXTRA_CAPTURE_COUNT = "capture_count"
        private const val EXTRA_BURST_STEP_MS = "burst_step_ms"
        private const val EXTRA_STEP_MS = "step_ms"
        private const val EXTRA_BURST_STEP_FRAMES = "burst_step_frames"
        private const val EXTRA_STEP_FRAMES = "step_frames"
        private const val EXTRA_BURST_LIVE = "burst_live"
        private const val EXTRA_LIVE_BURST = "live_burst"
        private const val EXTRA_CAPTURE_KINDS = "capture_kinds"
        private const val EXTRA_KINDS = "kinds"
        private const val EXTRA_CAPTURE_ID_BASE = "capture_id_base"
        private const val EXTRA_CAPTURE_ID = "capture_id"
        private const val EXTRA_CAPTURE_KINDS_FIRST = "capture_kinds_first"
        private const val EXTRA_FIRST_KINDS = "first_kinds"
        private const val EXTRA_CAPTURE_KINDS_REST = "capture_kinds_rest"
        private const val EXTRA_REST_KINDS = "rest_kinds"
        private const val EXTRA_VALUE = "value"
        private const val DEFAULT_TOUCH_X = 128
        private const val DEFAULT_TOUCH_Y = 96
        private const val DEFAULT_TOUCH_DURATION_MS = 80
        private const val DEFAULT_PRESS_DURATION_MS = 120
        private const val DEFAULT_PRESS_GAP_MS = 250

        private val receiverScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

        private const val ACTION_SET_RENDERER_SUFFIX = "SET_RENDERER"
        private const val ACTION_SET_IR_SUFFIX = "SET_IR"
        private const val ACTION_SET_JIT_SUFFIX = "SET_JIT"
        private const val ACTION_SET_BGOBJ_LOG_SUFFIX = "SET_BGOBJ_LOG"
        private const val ACTION_SET_LATCH_TRACE_SUFFIX = "SET_LATCH_TRACE"
        private const val ACTION_SET_FILTER_TINT_SUFFIX = "SET_FILTER_TINT"
        private const val ACTION_SET_FAST_FORWARD_SUFFIX = "SET_FAST_FORWARD"
        private const val ACTION_SET_SLOT2_ANALOG_SUFFIX = "SET_SLOT2_ANALOG"
        private const val ACTION_SET_SLOT2_ANALOG_MAPPING_SUFFIX = "SET_SLOT2_ANALOG_MAPPING"
        private const val ACTION_SET_VULKAN_FALLBACKS_SUFFIX = "SET_VULKAN_FALLBACKS"
        private const val ACTION_TOUCH_SCREEN_SUFFIX = "TOUCH_SCREEN"
        private const val ACTION_PRESS_INPUT_SUFFIX = "PRESS_INPUT"
        private const val ACTION_LAUNCH_ROM_SUFFIX = "LAUNCH_ROM"
        private const val ACTION_WAIT_ROM_READY_SUFFIX = "WAIT_ROM_READY"
        private const val ACTION_SAVE_STATE_SUFFIX = "SAVE_STATE"
        private const val ACTION_LOAD_STATE_SUFFIX = "LOAD_STATE"
        private const val ACTION_STEP_FRAME_SUFFIX = "STEP_FRAME"
        private const val ACTION_STEP_FRAMES_SUFFIX = "STEP_FRAMES"
        private const val ACTION_DUMP_RENDERER_CAPTURE_SUFFIX = "DUMP_RENDERER_CAPTURE"
        private const val ACTION_GET_PREFERENCES_SUFFIX = "GET_PREFERENCES"
        private const val ACTION_SET_PREFERENCE_SUFFIX = "SET_PREFERENCE"
        private const val ACTION_LIST_ROMS_SUFFIX = "LIST_ROMS"
        private const val ACTION_GET_FPS_SUFFIX = "GET_FPS"
        private const val ACTION_START_DEV_SERVER_SUFFIX = "START_DEV_SERVER"
        private const val ACTION_STOP_DEV_SERVER_SUFFIX = "STOP_DEV_SERVER"
        private const val EXTRA_KEY = "key"
        private const val EXTRA_TYPE = "type"
        private const val EXTRA_FILTER = "filter"
        private const val EXTRA_QUERY = "query"
        private const val EXTRA_PERSIST = "persist"
    }

    private fun Context.debugCommandAction(suffix: String): String {
        return "$packageName.$suffix"
    }
}
