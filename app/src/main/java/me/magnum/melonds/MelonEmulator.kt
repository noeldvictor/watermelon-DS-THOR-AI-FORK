package me.magnum.melonds

import android.graphics.Bitmap
import android.net.Uri
import android.view.Surface
import me.magnum.melonds.common.camera.DSiCameraSource
import me.magnum.melonds.domain.model.Cheat
import me.magnum.melonds.domain.model.EmulatorConfiguration
import me.magnum.melonds.domain.model.Input
import me.magnum.melonds.domain.model.retroachievements.RASimpleAchievement
import me.magnum.melonds.domain.model.retroachievements.RASimpleLeaderboard
import me.magnum.melonds.domain.model.retroachievements.RARuntimeBridgeConfig
import me.magnum.melonds.domain.model.retroachievements.RASimpleRuntimeAchievement
import me.magnum.melonds.domain.model.retroachievements.RASimpleRuntimeAchievementBucketEntry
import me.magnum.melonds.ui.emulator.render.FrameRenderCallback
import me.magnum.melonds.ui.emulator.model.VulkanPresentationConfig
import me.magnum.melonds.ui.emulator.rewind.model.RewindSaveState
import me.magnum.melonds.ui.emulator.rewind.model.RewindWindow
import java.nio.ByteBuffer

object MelonEmulator {
    enum class LoadResult(val isTerminal: Boolean) {
        SUCCESS(false),
        SUCCESS_GBA_FAILED(false),
        NDS_FAILED(true),
        BIOS_FAILED(true)
    }

    enum class FirmwareLoadResult {
        SUCCESS,
        BIOS9_MISSING,
        BIOS9_BAD,
        BIOS7_MISSING,
        BIOS7_BAD,
        FIRMWARE_MISSING,
        FIRMWARE_BAD,
        FIRMWARE_NOT_BOOTABLE,
        DSI_BIOS9_MISSING,
        DSI_BIOS9_BAD,
        DSI_BIOS7_MISSING,
        DSI_BIOS7_BAD,
        DSI_NAND_MISSING,
        DSI_NAND_BAD
    }

    enum class GbaSlotType {
        NONE,
        GBA_ROM,
        RUMBLE_PAK,
        MEMORY_EXPANSION,
        ANALOG_INPUT,
    }

    external fun setupEmulator(
        emulatorConfiguration: EmulatorConfiguration,
        dsiCameraSource: DSiCameraSource?,
        screenshotBuffer: ByteBuffer,
    )

    external fun setupCheats(cheats: Array<Cheat>)

    external fun setupAchievements(
        achievements: Array<RASimpleAchievement>,
        leaderboards: Array<RASimpleLeaderboard>,
        richPresenceScript: String?,
        runtimeConfig: RARuntimeBridgeConfig?,
    ): Boolean

    external fun unloadRetroAchievementsData()

    external fun getRichPresenceStatus(): String?

    external fun getRuntimeAchievements(): Array<RASimpleRuntimeAchievement>

    external fun getRuntimeAchievementBuckets(): Array<RASimpleRuntimeAchievementBucketEntry>

    external fun getRuntimeSubsetIds(): LongArray

    external fun retryPendingRetroAchievementsSubmissions(
        expectedNativeSubmissionIds: LongArray,
    ): LongArray?

    external fun refreshPendingRetroAchievementsSubmissions(): Long

    external fun discardPendingRetroAchievementsSubmissions(
        expectedNativeSubmissionIds: LongArray,
    ): Int

    external fun setRetroAchievementsSubmissionTransportSuspended(suspended: Boolean)

    fun loadRom(romUri: Uri, sramUri: Uri, gbaSlotType: GbaSlotType, gbaRomUri: Uri?, gbaSramUri: Uri?): LoadResult {
        val loadResult = loadRomInternal(romUri.toString(), sramUri.toString(), gbaSlotType.ordinal, gbaRomUri?.toString(), gbaSramUri?.toString())
        return when (loadResult) {
            0 -> LoadResult.SUCCESS
            1 -> LoadResult.SUCCESS_GBA_FAILED
            2 -> LoadResult.NDS_FAILED
            3 -> LoadResult.BIOS_FAILED
            else -> throw RuntimeException("Unknown load result")
        }
    }

    fun bootFirmware(): FirmwareLoadResult {
        val loadResult = bootFirmwareInternal()
        return FirmwareLoadResult.entries[loadResult]
    }

    private external fun loadRomInternal(romPath: String, sramPath: String, gbaSlotType: Int, gbaRomPath: String?, gbaSramPath: String?): Int

    private external fun bootFirmwareInternal(): Int

	external fun startEmulation(startPaused: Boolean)
    external fun precompileVulkanPipelines(
        videoFilteringOrdinal: Int,
        retroShaderPresetPath: String?,
        retroShaderSourceResolution: String,
        retroShaderPassCount: Int,
        retroShaderParameterOverrides: Map<String, Float>,
    ): Boolean

    external fun configureOpenGlRetroArchFilter(
        enabled: Boolean,
        presetPath: String?,
        parameterOverrides: String?,
        clearHistory: Boolean,
        sourceResolution: String,
        maxLayoutWidth: Int,
        maxLayoutHeight: Int,
        passCount: Int,
    )

    external fun prewarmOpenGlRetroArchFilter(atlasWidth: Int, atlasHeight: Int): Boolean

    external fun releaseOpenGlRetroArchFilter()

    external fun consumeShaderDiagnostics(): Array<String>?

    external fun presentFrame(deadlineNs: Long, frameRenderCallback: FrameRenderCallback)
    external fun attachVulkanSurface(surface: Surface, width: Int, height: Int): Int
    external fun resizeVulkanSurface(surfaceId: Int, width: Int, height: Int)
    external fun configureVulkanSurface(surfaceId: Int, presentationConfig: VulkanPresentationConfig, backgroundBitmap: Bitmap?)
    external fun detachVulkanSurface(surfaceId: Int)
    external fun presentVulkanFrame(deadlineNs: Long, budgetDeadlineNs: Long)

	external fun getFPS(): Float

    external fun getCurrentRenderer(): Int

	external fun pauseEmulation()

	external fun resumeEmulation()

    external fun debugStepFrame(): Boolean

    // Reverse-engineering tools (debug builds; DebugToolsJNI.cpp). cpu: 0 = ARM9, 1 = ARM7.
    external fun debugIsGameRunning(): Boolean
    external fun debugIsPaused(): Boolean
    external fun debugGetFrame(): Int
    /** I/O registers (0x04xxxxxx) read as 0: reading them has side effects. */
    external fun debugReadMemory(cpu: Int, address: Int, length: Int): ByteArray?
    /** 0 = no game, 1 = written (paused), 2 = queued for the next frame start. I/O is skipped. */
    external fun debugWriteMemory(cpu: Int, address: Int, data: ByteArray): Int
    /** R0-R14, next instruction, CPSR, flags (bit 0 JIT, 1 halted, 2 Thumb), ITCM size, DTCM base, DTCM mask, 1 if paused. */
    external fun debugGetRegisters(cpu: Int): IntArray?
    /** The interpreter instead of the JIT (watchpoints and call traces only see the interpreter). */
    external fun debugSetInterpreter(enabled: Boolean): Boolean
    external fun debugIsJitActive(): Boolean
    external fun debugStartGdbStub(portArm9: Int, portArm7: Int): Boolean
    external fun debugStopGdbStub(): Boolean
    external fun debugReadRom(offset: Int, length: Int): ByteArray?
    external fun debugWatchStart(start: Int, end: Int, reads: Boolean, writes: Boolean, arm7: Boolean, maxEvents: Int, matchValue: Boolean, value: Int): Boolean
    external fun debugWatchStop()
    /** [dropped, then per event: frame, pc, lr, address, value, size, write, cpu, thumb, r0, r1, r2, r3, sp] */
    external fun debugWatchTake(): IntArray
    external fun debugTraceStart(targetStart: Int, targetEnd: Int, arm7: Boolean, maxEvents: Int): Boolean
    external fun debugTraceStop()
    /** [dropped, then per call: frame, from, to, cpu] */
    external fun debugTraceTakeEvents(): IntArray
    /** per (call site, target): from, to, count; most frequent first */
    external fun debugTraceTakeCounts(): IntArray
    external fun debugDisplayListTraceStart(): Boolean
    external fun debugDisplayListTraceStop()
    /** Per display list: hash low, hash high, size, count, last source, first frame, last frame. */
    external fun debugDisplayListTraceTake(): IntArray
    /** [dropped, then every word the CPU wrote to the GX FIFO since the last call]. */
    external fun debugDisplayListTraceTakeCpuWords(): IntArray

    external fun resetEmulation()

	external fun stopEmulation()

    fun saveState(path: Uri): Boolean {
        return saveStateInternal(path.toString())
    }

    private external fun saveStateInternal(path: String): Boolean

    fun loadState(path: Uri): Boolean {
        return loadStateInternal(path.toString())
    }

    private external fun loadStateInternal(path: String): Boolean

    external fun loadRewindState(rewindSaveState: RewindSaveState): Boolean

    external fun getRewindWindow(): RewindWindow

	external fun onScreenTouch(x: Int, y: Int)

	external fun onScreenRelease()

	fun onInputDown(input: Input) {
        onKeyPress(input.keyCode)
    }

	fun onInputUp(input: Input) {
        onKeyRelease(input.keyCode)
    }

    private external fun onKeyPress(key: Int)

    private external fun onKeyRelease(key: Int)

    external fun setSlot2AnalogInput(x: Float, y: Float)

    // free camera (Settings -> Input): the right stick orbits the 3D view, see MelonInstance
    external fun setFreeCameraEnabled(enabled: Boolean)
    external fun setFreeCameraInput(x: Float, y: Float, zoom: Float)
    external fun resetFreeCamera()
    // radians; zoom as a fraction of the distance to the screen centre (positive = farther)
    external fun setFreeCameraPose(yaw: Float, pitch: Float, zoom: Float)
    // the running game's pack has a camera profile (texturepacks/<GAMECODE>/camera.txt) that turns
    // the free camera on whatever the setting
    external fun isFreeCameraForced(): Boolean

    external fun takeScreenshot(): Boolean

    external fun setFastForwardEnabled(enabled: Boolean)

    external fun setFastForwardSpeedMultiplier(multiplier: Float)

    external fun setFrameLimitSpeedMultiplier(multiplier: Float)

    external fun setMicrophoneEnabled(enabled: Boolean)

    external fun updateEmulatorConfiguration(emulatorConfiguration: EmulatorConfiguration)
}
