package me.magnum.melonds.debug

import android.content.Context
import me.magnum.melonds.MelonEmulator
import me.magnum.melonds.domain.model.Cheat
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.Locale

/**
 * Reverse-engineering tools for [DevTools]: memory read/write/search/dump, registers, watchpoints
 * ("which instruction writes this address"), call traces ("which functions run in this scene"),
 * the GDB stub and the cartridge's ROM (header, overlays, dump).
 *
 * Watchpoints and call traces hook the interpreter; the JIT compiles memory accesses and calls into
 * native code they never see, so starting either switches the interpreter on (`debug_mode`,
 * ~2-4x slower) until `debug_mode off`. Everything else works with the JIT.
 */
internal class ReTools(private val context: Context) {

    private class Search(
        val cpu: Int,
        val start: Long,
        val valueSize: Int,
        var snapshot: ByteArray,
        var candidates: IntArray,
    )

    // the dev server makes a ReTools per request: the search lives in the process
    private var search: Search?
        get() = currentSearch
        set(value) { currentSearch = value }

    // ---- memory -----------------------------------------------------------------------------

    fun memRead(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val cpu = cpu(args)
        val address = address(args, "address") ?: return error("address required (e.g. \"0x02000000\")")
        val length = args.optInt("length", 64).coerceIn(1, MAX_INLINE_READ)
        val data = MelonEmulator.debugReadMemory(cpu, address.toInt(), length) ?: return error("read failed")
        val result = JSONObject().put("address", hex(address)).put("length", length).put("cpu", cpuName(cpu))
        when (val format = args.optString("format", "hex").lowercase(Locale.US)) {
            "hex" -> result.put("lines", JSONArray(hexDump(address, data)))
            "u8", "u16", "u32" -> {
                val size = format.drop(1).toInt() / 8
                val values = JSONArray()
                var offset = 0
                while (offset + size <= data.size) {
                    values.put("${hex(address + offset)}=${hex(littleEndian(data, offset, size), size * 2)}")
                    offset += size
                }
                result.put("values", values)
            }
            "ascii" -> result.put("text", data.map { b -> (b.toInt() and 0xFF).let { if (it in 0x20..0x7E) it.toChar() else '.' } }.joinToString(""))
            else -> return error("format must be hex, u8, u16, u32 or ascii")
        }
        return result
    }

    fun memWrite(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val cpu = cpu(args)
        val address = address(args, "address") ?: return error("address required")
        val bytes = when {
            args.has("hex") -> parseHexBytes(args.optString("hex")) ?: return error("hex: pairs of hex digits, e.g. \"01 0A FF\"")
            args.has("value") -> {
                val size = args.optInt("size", 4)
                if (size !in listOf(1, 2, 4)) return error("size must be 1, 2 or 4")
                val value = number(args, "value") ?: return error("value: a number or \"0x...\"")
                ByteArray(size) { ((value shr (8 * it)) and 0xFF).toByte() }
            }
            else -> return error("pass hex (bytes) or value + size")
        }
        if (bytes.isEmpty() || bytes.size > MAX_INLINE_READ) return error("1-$MAX_INLINE_READ bytes")
        val result = JSONObject().put("address", hex(address)).put("bytes", bytes.size).put("cpu", cpuName(cpu))
        when (MelonEmulator.debugWriteMemory(cpu, address.toInt(), bytes)) {
            1 -> {
                val readBack = MelonEmulator.debugReadMemory(cpu, address.toInt(), bytes.size)
                result.put("applied", "now (paused)").put("readBack", readBack?.let { toHex(it) } ?: JSONObject.NULL)
            }
            2 -> result.put("applied", "at the next frame start")
            else -> return error("no game running")
        }
        if ((address shr 24) == 0x04L) result.put("note", "I/O registers are skipped")
        return result
    }

    fun cheatTest(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val words = args.optString("code").trim().split(WHITESPACE).filter { it.isNotEmpty() }
        if (words.size % 2 != 0 || words.any { !it.matches(HEX_WORD) }) {
            return error("code: pairs of 8-digit hex words")
        }
        val cheats = if (words.isEmpty()) {
            emptyArray()
        } else {
            arrayOf(Cheat(null, -1, "cheat_test", null, words.joinToString(" ") { it.uppercase(Locale.ROOT) }, true))
        }
        MelonEmulator.setupCheats(cheats)
        return JSONObject().put("lines", words.size / 2).put("note", if (words.isEmpty()) "no cheats run now" else "runs every frame")
    }

    fun memDump(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val cpu = cpu(args)
        val (address, length) = when (args.optString("region").lowercase(Locale.US)) {
            "main" -> MAIN_RAM to args.optLong("length", MAIN_RAM_SIZE)
            "itcm" -> ITCM to ITCM_SIZE
            "dtcm" -> (dtcmBase() ?: return error("no game running")) to DTCM_SIZE
            "" -> (address(args, "address") ?: return error("pass region (main, itcm, dtcm) or address + length")) to
                args.optLong("length", 0x1000L)
            else -> return error("region must be main, itcm or dtcm")
        }
        if (length !in 1..MAX_DUMP) return error("length 1-$MAX_DUMP")
        val name = safeName(args.optString("name").ifEmpty { "${cpuName(cpu)}_${hex(address).removePrefix("0x")}" })
        val file = File(reDir(), "$name.bin")
        val digest = MessageDigest.getInstance("SHA-1")
        file.outputStream().use { out ->
            var offset = 0L
            while (offset < length) {
                val chunk = minOf(DUMP_CHUNK, length - offset).toInt()
                val data = MelonEmulator.debugReadMemory(cpu, (address + offset).toInt(), chunk) ?: return error("read failed at ${hex(address + offset)}")
                out.write(data)
                digest.update(data)
                offset += chunk
            }
        }
        return JSONObject().put("file", file.absolutePath).put("address", hex(address)).put("length", length)
            .put("sha1", toHex(digest.digest(), separator = ""))
            .put("pull", pullCommand(file))
            .put("frame", MelonEmulator.debugGetFrame())
            .put("paused", MelonEmulator.debugIsPaused())
    }

    /**
     * Cheat-search style: find where a value lives. `value` (+ `size`) finds equal values; `hex`
     * finds a byte pattern; `snapshot` keeps every aligned address for a later [memRefine] by change.
     */
    fun memSearch(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val cpu = cpu(args)
        val start = address(args, "start") ?: MAIN_RAM
        val length = args.optLong("length", MAIN_RAM_SIZE).coerceIn(1, MAX_DUMP)
        val snapshot = readRange(cpu, start, length.toInt()) ?: return error("read failed")
        val pattern = when {
            args.has("hex") -> parseHexBytes(args.optString("hex")) ?: return error("hex: pairs of hex digits")
            else -> null
        }
        val valueSize = if (pattern != null) pattern.size else args.optInt("size", 4)
        if (pattern == null && valueSize !in listOf(1, 2, 4)) return error("size must be 1, 2 or 4")
        val step = if (pattern == null && args.optBoolean("aligned", true)) valueSize else 1
        val candidates = when {
            pattern != null -> findPattern(snapshot, pattern)
            args.optBoolean("snapshot") -> IntArray(((snapshot.size - valueSize) / step) + 1) { it * step }
            else -> {
                val value = number(args, "value") ?: return error("pass value, hex or snapshot=true")
                val mask = sizeMask(valueSize)
                filterOffsets(snapshot, valueSize, step) { littleEndian(snapshot, it, valueSize) == (value and mask) }
            }
        }
        search = Search(cpu, start, valueSize, snapshot, candidates)
        return searchResult(args)
    }

    fun memRefine(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val state = search ?: return error("no search: start one with mem_search")
        val current = readRange(state.cpu, state.start, state.snapshot.size) ?: return error("read failed")
        val size = state.valueSize
        val mode = args.optString("mode", "changed").lowercase(Locale.US)
        val value = number(args, "value")?.and(sizeMask(size))
        if (mode in listOf("equal", "not_equal") && value == null) return error("$mode needs value")
        val keep: (Int) -> Boolean = when (mode) {
            "equal" -> { o -> littleEndian(current, o, size) == value }
            "not_equal" -> { o -> littleEndian(current, o, size) != value }
            "changed" -> { o -> littleEndian(current, o, size) != littleEndian(state.snapshot, o, size) }
            "unchanged" -> { o -> littleEndian(current, o, size) == littleEndian(state.snapshot, o, size) }
            "increased" -> { o -> littleEndian(current, o, size) > littleEndian(state.snapshot, o, size) }
            "decreased" -> { o -> littleEndian(current, o, size) < littleEndian(state.snapshot, o, size) }
            else -> return error("mode: equal, not_equal, changed, unchanged, increased, decreased")
        }
        state.candidates = state.candidates.filter(keep).toIntArray()
        state.snapshot = current
        return searchResult(args).put("mode", mode)
    }

    private fun searchResult(args: JSONObject): JSONObject {
        val state = search!!
        val limit = args.optInt("limit", 40).coerceIn(0, 2000)
        val shown = JSONArray()
        for (offset in state.candidates.take(limit)) {
            val value = littleEndian(state.snapshot, offset, minOf(state.valueSize, 4))
            shown.put("${hex(state.start + offset)}=${hex(value, minOf(state.valueSize, 4) * 2)}")
        }
        return JSONObject().put("count", state.candidates.size).put("cpu", cpuName(state.cpu))
            .put("region", "${hex(state.start)}+${hex(state.snapshot.size.toLong())}").put("size", state.valueSize)
            .put("results", shown).put("frame", MelonEmulator.debugGetFrame())
    }

    // ---- CPU --------------------------------------------------------------------------------

    fun regs(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val cpu = cpu(args)
        val values = MelonEmulator.debugGetRegisters(cpu) ?: return error("no game running")
        val registers = JSONObject()
        for (i in 0 until 13) registers.put("r$i", hex(values[i].toLong() and 0xFFFFFFFFL))
        registers.put("sp", hex(values[13].toLong() and 0xFFFFFFFFL))
        registers.put("lr", hex(values[14].toLong() and 0xFFFFFFFFL))
        registers.put("pc", hex(values[15].toLong() and 0xFFFFFFFFL))
        val cpsr = values[16]
        val flags = values[17]
        return JSONObject().put("cpu", cpuName(cpu)).put("registers", registers)
            .put("cpsr", hex(cpsr.toLong() and 0xFFFFFFFFL)).put("mode", MODES[cpsr and 0x1F] ?: "?")
            .put("thumb", flags and 4 != 0).put("halted", flags and 2 != 0).put("jit", flags and 1 != 0)
            .put("itcmSize", hex(u32(values[18])))
            .put("dtcm", "${hex(u32(values[19]))} mask ${hex(u32(values[20]))}")
            .put("paused", values[21] == 1)
            .put("note", if (values[21] == 1) "between frames (paused)" else "the game runs: a snapshot taken mid-frame")
    }

    /** Where the game put its DTCM (CP15; the SDK default is 0x027E0000). */
    private fun dtcmBase(): Long? = MelonEmulator.debugGetRegisters(0)?.let { u32(it[19]) }

    fun debugMode(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val on = args.optBoolean("on", true)
        MelonEmulator.debugSetInterpreter(on)
        val jit = waitForJit(!on)
        return JSONObject().put("interpreter", !jit).put("jit", jit)
            .put("note", if (jit == !on) "applied" else "queued: applies at the next frame (the game is paused)")
    }

    fun watch(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        return when (val action = args.optString("action", "read").lowercase(Locale.US)) {
            "start" -> {
                val value = number(args, "value")
                // a value watch may cover all memory: "who writes 0x1555 anywhere"
                val address = address(args, "address") ?: if (value != null) 0L else return error("address (or value) required")
                val end = address(args, "end")
                    ?: if (!args.has("address")) 0xFFFFFFFFL else (address + args.optLong("length", 4L).coerceAtLeast(1L))
                val reads = args.optBoolean("reads", false)
                val writes = args.optBoolean("writes", true)
                if (!reads && !writes) return error("watch reads, writes or both")
                val interpreter = ensureInterpreter()
                MelonEmulator.debugWatchStart(address.toInt(), end.toInt(), reads, writes, args.optBoolean("arm7", false),
                    args.optInt("max_events", 4096).coerceIn(1, 1_000_000), value != null, (value ?: 0L).toInt())
                JSONObject().put("watching", "${hex(address)}-${hex(end)}").put("reads", reads).put("writes", writes)
                    .put("value", value?.let { hex(it and 0xFFFFFFFFL) } ?: JSONObject.NULL)
                    .put("interpreter", interpreter)
                    .put("next", "play or step frames, then watch action=read")
            }
            "read" -> watchEvents(args)
            "stop" -> {
                MelonEmulator.debugWatchStop()
                watchEvents(args).put("stopped", true).put("note", "the interpreter stays on until debug_mode on=false")
            }
            else -> error("action: start, read or stop (got $action)")
        }
    }

    private fun watchEvents(args: JSONObject): JSONObject {
        val raw = MelonEmulator.debugWatchTake()
        val count = (raw.size - 1) / WATCH_EVENT_INTS
        data class Event(val frame: Int, val pc: Long, val lr: Long, val address: Long, val value: Long, val size: Int,
                         val write: Boolean, val cpu: Int, val thumb: Boolean, val regs: List<Long>, val sp: Long)
        val events = List(count) { i ->
            val b = 1 + i * WATCH_EVENT_INTS
            Event(raw[b], u32(raw[b + 1]), u32(raw[b + 2]), u32(raw[b + 3]), u32(raw[b + 4]), raw[b + 5], raw[b + 6] != 0,
                raw[b + 7], raw[b + 8] != 0, (9..12).map { u32(raw[b + it]) }, u32(raw[b + 13]))
        }
        val result = JSONObject().put("events", count).put("dropped", raw[0])
        // grouped by instruction: the usual question is "which code touches this"
        val groups = JSONArray()
        events.groupBy { Triple(it.pc, it.write, it.cpu) }.entries.sortedByDescending { it.value.size }.take(args.optInt("groups", 30)).forEach { (key, list) ->
            groups.put(JSONObject()
                .put("pc", hex(key.first)).put("thumb", list.first().thumb).put(if (key.second) "write" else "read", list.size)
                .put("cpu", cpuName(key.third)).put("lr", hex(list.first().lr))
                .put("sizes", JSONArray(list.map { it.size }.distinct()))
                .put("addresses", JSONArray(list.map { hex(it.address) }.distinct().take(6)))
                .put("values", JSONArray(list.map { hex(it.value, it.size * 2) }.distinct().take(6)))
                .put("firstRegs", "r0=${hex(list.first().regs[0])} r1=${hex(list.first().regs[1])} " +
                    "r2=${hex(list.first().regs[2])} r3=${hex(list.first().regs[3])} sp=${hex(list.first().sp)}")
                .put("frames", "${list.first().frame}-${list.last().frame}"))
        }
        result.put("byInstruction", groups)
        val recent = args.optInt("recent", 10).coerceIn(0, 500)
        result.put("recent", JSONArray(events.takeLast(recent).map {
            "f${it.frame} ${cpuName(it.cpu)} pc=${hex(it.pc)} ${if (it.write) "W" else "R"}${it.size * 8} ${hex(it.address)}=${hex(it.value, it.size * 2)} " +
                "lr=${hex(it.lr)} r0-r3=${it.regs.joinToString(",") { r -> hex(r) }} sp=${hex(it.sp)}"
        }))
        return result
    }

    fun trace(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        return when (val action = args.optString("action", "read").lowercase(Locale.US)) {
            "start" -> {
                val targetStart = address(args, "target_start") ?: 0L
                val targetEnd = address(args, "target_end") ?: 0L
                val interpreter = ensureInterpreter()
                MelonEmulator.debugTraceStart(targetStart.toInt(), targetEnd.toInt(), args.optBoolean("arm7", false),
                    args.optInt("max_events", 2048).coerceIn(1, 1_000_000))
                JSONObject().put("tracing", if (targetEnd == 0L) "all calls" else "calls into ${hex(targetStart)}-${hex(targetEnd)}")
                    .put("interpreter", interpreter).put("next", "play or step frames, then trace action=read")
            }
            "read" -> traceEvents(args)
            "stop" -> {
                MelonEmulator.debugTraceStop()
                traceEvents(args).put("stopped", true).put("note", "the interpreter stays on until debug_mode on=false")
            }
            else -> error("action: start, read or stop (got $action)")
        }
    }

    private fun traceEvents(args: JSONObject): JSONObject {
        val counts = MelonEmulator.debugTraceTakeCounts()
        val raw = MelonEmulator.debugTraceTakeEvents()
        val top = args.optInt("top", 50).coerceIn(0, 5000)
        val calls = JSONArray()
        var i = 0
        while (i + 2 < counts.size && calls.length() < top) {
            calls.put("${hex(u32(counts[i]))} -> ${hex(u32(counts[i + 1]))} x${counts[i + 2]}")
            i += 3
        }
        // the same per target: what runs, however many sites call it
        val byTarget = HashMap<Long, Long>()
        i = 0
        while (i + 2 < counts.size) {
            byTarget.merge(u32(counts[i + 1]), counts[i + 2].toLong(), Long::plus)
            i += 3
        }
        val functions = JSONArray(byTarget.entries.sortedByDescending { it.value }.take(top).map { "${hex(it.key)} x${it.value}" })
        val recent = args.optInt("recent", 20).coerceIn(0, 2000)
        val eventCount = (raw.size - 1) / 4
        val recentLines = JSONArray()
        for (e in maxOf(0, eventCount - recent) until eventCount) {
            val b = 1 + e * 4
            recentLines.put("f${raw[b]} ${cpuName(raw[b + 3])} ${hex(u32(raw[b + 1]))} -> ${hex(u32(raw[b + 2]))}")
        }
        return JSONObject().put("distinctCallSites", counts.size / 3).put("functions", functions).put("calls", calls)
            .put("recent", recentLines).put("dropped", raw.firstOrNull() ?: 0)
            .put("note", "counts reset on every read; bit 0 of a target = Thumb")
    }

    fun dlTrace(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        return when (val action = args.optString("action", "read").lowercase(Locale.US)) {
            "start" -> {
                MelonEmulator.debugDisplayListTraceStart()
                JSONObject().put("tracing", "display lists DMA'd into the geometry FIFO")
                    .put("next", "play or step frames, then dl_trace action=read")
            }
            "read" -> displayLists(args)
            "stop" -> {
                MelonEmulator.debugDisplayListTraceStop()
                displayLists(args).put("stopped", true)
            }
            else -> error("action: start, read or stop (got $action)")
        }
    }

    private fun displayLists(args: JSONObject): JSONObject {
        val raw = MelonEmulator.debugDisplayListTraceTake()
        val top = args.optInt("top", 100).coerceIn(0, 20000)
        val lists = JSONArray()
        var transfers = 0L
        var i = 0
        while (i + 6 < raw.size) {
            transfers += raw[i + 3]
            if (lists.length() < top) {
                val hash = (u32(raw[i + 1]) shl 32) or u32(raw[i])
                lists.put(JSONObject()
                    .put("key", "mdl1_${raw[i + 2]}_${"%016x".format(hash)}")
                    .put("count", raw[i + 3]).put("src", hex(u32(raw[i + 4])))
                    .put("frames", "${raw[i + 5]}-${raw[i + 6]}"))
            }
            i += 7
        }
        return JSONObject().put("distinct", raw.size / 7).put("transfers", transfers).put("lists", lists)
            .put("note", "counts reset on every read; keys match tools/hd_remaster/models3d.py shape keys")
    }

    fun gdb(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val port9 = args.optInt("port_arm9", 3333)
        val port7 = args.optInt("port_arm7", 3334)
        return when (args.optString("action", "start").lowercase(Locale.US)) {
            "start" -> {
                MelonEmulator.debugStartGdbStub(port9, port7)
                JSONObject().put("arm9", "127.0.0.1:$port9").put("arm7", "127.0.0.1:$port7")
                    .put("connect", JSONArray(listOf(
                        "adb -s <serial> forward tcp:$port9 tcp:$port9",
                        "gdb-multiarch -ex \"set architecture armv5te\" -ex \"target remote :$port9\"",
                        "Ghidra: Debugger > gdb (remote), target remote localhost:$port9",
                    )))
                    .put("note", "the stub listens on the device's loopback only; it runs on the interpreter, " +
                        "and a halted CPU freezes the game until gdb continues")
            }
            "stop" -> {
                MelonEmulator.debugStopGdbStub()
                JSONObject().put("stopped", true).put("note", "the interpreter stays on until debug_mode on=false")
            }
            else -> error("action: start or stop")
        }
    }

    // ---- ROM --------------------------------------------------------------------------------

    fun romInfo(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val header = MelonEmulator.debugReadRom(0, 0x200)?.takeIf { it.size == 0x200 } ?: return error("no cartridge")
        fun u32At(offset: Int) = littleEndian(header, offset, 4)
        val result = JSONObject()
            .put("title", String(header, 0, 12, Charsets.US_ASCII).trimEnd('\u0000'))
            .put("gameCode", String(header, 12, 4, Charsets.US_ASCII))
            .put("maker", String(header, 16, 2, Charsets.US_ASCII))
            .put("unitCode", header[0x12].toInt() and 0xFF)
            .put("version", header[0x1E].toInt() and 0xFF)
            .put("arm9", JSONObject().put("romOffset", hex(u32At(0x20))).put("entry", hex(u32At(0x24)))
                .put("ramAddress", hex(u32At(0x28))).put("size", hex(u32At(0x2C))))
            .put("arm7", JSONObject().put("romOffset", hex(u32At(0x30))).put("entry", hex(u32At(0x34)))
                .put("ramAddress", hex(u32At(0x38))).put("size", hex(u32At(0x3C))))
            .put("fnt", "${hex(u32At(0x40))}+${hex(u32At(0x44))}")
            .put("fat", "${hex(u32At(0x48))}+${hex(u32At(0x4C))}")
            .put("romSize", hex(u32At(0x80)))
        if (args.optBoolean("overlays", true)) {
            result.put("arm9Overlays", overlays(u32At(0x50), u32At(0x54), u32At(0x48), cpu = 0))
            result.put("arm7Overlays", overlays(u32At(0x58), u32At(0x5C), u32At(0x48), cpu = 1))
        }
        return result
    }

    /** The overlay table, and which overlays sit in RAM now (their code compared with the RAM). */
    private fun overlays(tableOffset: Long, tableSize: Long, fatOffset: Long, cpu: Int): JSONArray {
        val list = JSONArray()
        if (tableSize == 0L) return list
        val table = MelonEmulator.debugReadRom(tableOffset.toInt(), tableSize.toInt()) ?: return list
        for (entry in 0 until table.size / 32) {
            val b = entry * 32
            val id = littleEndian(table, b, 4)
            val ram = littleEndian(table, b + 4, 4)
            val ramSize = littleEndian(table, b + 8, 4)
            val bss = littleEndian(table, b + 12, 4)
            val fileId = littleEndian(table, b + 24, 4)
            val flags = littleEndian(table, b + 28, 4)
            val compressed = (flags shr 24) and 1L == 1L
            val json = JSONObject().put("id", id).put("ram", hex(ram)).put("size", hex(ramSize)).put("bss", hex(bss))
                .put("fileId", fileId).put("compressed", compressed)
            val fat = MelonEmulator.debugReadRom((fatOffset + fileId * 8).toInt(), 8)
            if (fat != null && fat.size == 8) {
                val fileStart = littleEndian(fat, 0, 4)
                val fileEnd = littleEndian(fat, 4, 4)
                json.put("romOffset", hex(fileStart))
                json.put("loaded", overlayLoaded(cpu, ram, ramSize, fileStart, fileEnd, compressed))
            }
            list.put(json)
        }
        return list
    }

    private fun overlayLoaded(cpu: Int, ram: Long, ramSize: Long, fileStart: Long, fileEnd: Long, compressed: Boolean): Any {
        val compareLength = minOf(256L, ramSize).toInt()
        if (compareLength <= 0 || fileEnd <= fileStart) return JSONObject.NULL
        val expected = if (compressed) {
            val file = MelonEmulator.debugReadRom(fileStart.toInt(), (fileEnd - fileStart).toInt()) ?: return JSONObject.NULL
            runCatching { Blz.decompress(file) }.getOrNull()?.copyOf(compareLength) ?: return JSONObject.NULL
        } else {
            MelonEmulator.debugReadRom(fileStart.toInt(), compareLength) ?: return JSONObject.NULL
        }
        val actual = MelonEmulator.debugReadMemory(cpu, ram.toInt(), compareLength) ?: return JSONObject.NULL
        // code patched after loading (relocations, hooks) keeps most bytes
        val same = expected.indices.count { expected[it] == actual[it] }
        return same * 100 / compareLength >= 90
    }

    fun romDump(args: JSONObject): JSONObject {
        requireGame()?.let { return it }
        val header = MelonEmulator.debugReadRom(0, 0x200)?.takeIf { it.size == 0x200 } ?: return error("no cartridge")
        val gameCode = String(header, 12, 4, Charsets.US_ASCII)
        val file = File(reDir(), "${safeName(args.optString("name").ifEmpty { gameCode })}.nds")
        val digest = MessageDigest.getInstance("SHA-256")
        var total = 0L
        file.outputStream().use { out ->
            while (true) {
                val chunk = MelonEmulator.debugReadRom(total.toInt(), DUMP_CHUNK.toInt()) ?: break
                if (chunk.isEmpty()) break
                out.write(chunk)
                digest.update(chunk)
                total += chunk.size
                if (chunk.size < DUMP_CHUNK) break
            }
        }
        return JSONObject().put("file", file.absolutePath).put("bytes", total).put("gameCode", gameCode)
            .put("sha256", toHex(digest.digest(), separator = "")).put("pull", pullCommand(file))
            .put("note", "a ROM: keep it out of git")
    }

    // ---- helpers ----------------------------------------------------------------------------

    private fun requireGame(): JSONObject? = if (MelonEmulator.debugIsGameRunning()) null else error("no game running")

    /** Switches the interpreter on for the hooks; returns whether it is on (now or at the next frame). */
    private fun ensureInterpreter(): Boolean {
        if (!MelonEmulator.debugIsJitActive()) return true
        MelonEmulator.debugSetInterpreter(true)
        return !waitForJit(false) || MelonEmulator.debugIsPaused()
    }

    /** Waits up to a second for the JIT to reach [expected]; returns the JIT state then. */
    private fun waitForJit(expected: Boolean): Boolean {
        val deadline = System.currentTimeMillis() + 1000
        while (MelonEmulator.debugIsJitActive() != expected && System.currentTimeMillis() < deadline) {
            if (MelonEmulator.debugIsPaused()) break
            Thread.sleep(20)
        }
        return MelonEmulator.debugIsJitActive()
    }

    private fun readRange(cpu: Int, start: Long, length: Int): ByteArray? {
        val out = ByteArray(length)
        var offset = 0
        while (offset < length) {
            val chunk = minOf(DUMP_CHUNK.toInt(), length - offset)
            val data = MelonEmulator.debugReadMemory(cpu, (start + offset).toInt(), chunk) ?: return null
            data.copyInto(out, offset)
            offset += chunk
        }
        return out
    }

    private fun reDir(): File = File(context.filesDir, "re").apply { mkdirs() }

    private fun pullCommand(file: File): String =
        "adb -s <serial> exec-out run-as ${context.packageName} cat files/re/${file.name} > ${file.name}"

    private fun error(message: String): JSONObject = JSONObject().put("error", message)

    private fun cpu(args: JSONObject): Int = when (args.optString("cpu", "arm9").lowercase(Locale.US)) {
        "arm7", "7", "1" -> 1
        else -> 0
    }

    private fun cpuName(cpu: Int): String = if (cpu == 1) "arm7" else "arm9"

    private fun address(args: JSONObject, key: String): Long? = number(args, key)?.and(0xFFFFFFFFL)

    /** A JSON number, or a string: "0x..." hex, digits decimal, anything with a-f hex. */
    private fun number(args: JSONObject, key: String): Long? {
        if (!args.has(key) || args.isNull(key)) return null
        return when (val value = args.get(key)) {
            is Number -> value.toLong()
            is String -> {
                val text = value.trim().replace("_", "")
                when {
                    text.startsWith("0x", true) -> text.drop(2).toLongOrNull(16)
                    text.startsWith("-") -> text.toLongOrNull()
                    text.all { it.isDigit() } -> text.toLongOrNull()
                    else -> text.toLongOrNull(16)
                }
            }
            else -> null
        }
    }

    private fun u32(value: Int): Long = value.toLong() and 0xFFFFFFFFL

    private fun sizeMask(size: Int): Long = if (size >= 4) 0xFFFFFFFFL else (1L shl (8 * size)) - 1

    private fun littleEndian(data: ByteArray, offset: Int, size: Int): Long {
        var value = 0L
        for (i in 0 until size) value = value or ((data[offset + i].toLong() and 0xFF) shl (8 * i))
        return value
    }

    private inline fun filterOffsets(data: ByteArray, size: Int, step: Int, keep: (Int) -> Boolean): IntArray {
        val out = ArrayList<Int>()
        var offset = 0
        while (offset + size <= data.size) {
            if (keep(offset)) out.add(offset)
            offset += step
        }
        return out.toIntArray()
    }

    private fun findPattern(data: ByteArray, pattern: ByteArray): IntArray {
        val out = ArrayList<Int>()
        outer@ for (offset in 0..data.size - pattern.size) {
            for (i in pattern.indices) if (data[offset + i] != pattern[i]) continue@outer
            out.add(offset)
        }
        return out.toIntArray()
    }

    private fun parseHexBytes(text: String): ByteArray? {
        val digits = text.replace(" ", "").replace(",", "").removePrefix("0x")
        if (digits.isEmpty() || digits.length % 2 != 0) return null
        val bytes = ByteArray(digits.length / 2)
        for (i in bytes.indices) bytes[i] = (digits.substring(i * 2, i * 2 + 2).toIntOrNull(16) ?: return null).toByte()
        return bytes
    }

    private fun hex(value: Long, digits: Int = 8): String = "0x" + value.toString(16).uppercase(Locale.US).padStart(digits, '0')

    private fun toHex(data: ByteArray, separator: String = " "): String =
        data.joinToString(separator) { (it.toInt() and 0xFF).toString(16).uppercase(Locale.US).padStart(2, '0') }

    private fun hexDump(address: Long, data: ByteArray): List<String> = data.toList().chunked(16).mapIndexed { row, bytes ->
        val ascii = bytes.joinToString("") { b -> (b.toInt() and 0xFF).let { if (it in 0x20..0x7E) it.toChar().toString() else "." } }
        "${hex(address + row * 16).removePrefix("0x")}: ${toHex(bytes.toByteArray()).padEnd(47)}  $ascii"
    }

    private fun safeName(name: String): String = name.replace(Regex("[^A-Za-z0-9._-]"), "_").take(64)

    private companion object {
        @Volatile
        var currentSearch: Search? = null

        const val MAIN_RAM = 0x02000000L
        const val MAIN_RAM_SIZE = 0x400000L
        // games map ITCM at 0x01FF8000 (the 32 KB repeats up to the ITCM size)
        const val ITCM = 0x01FF8000L
        const val ITCM_SIZE = 0x8000L
        const val DTCM_SIZE = 0x4000L
        const val MAX_INLINE_READ = 0x10000
        // frame, pc, lr, address, value, size, write, cpu, thumb, r0-r3, sp (DebugToolsJNI.cpp)
        const val WATCH_EVENT_INTS = 14
        const val MAX_DUMP = 0x1000000L
        const val DUMP_CHUNK = 0x400000L
        val MODES = mapOf(0x10 to "usr", 0x11 to "fiq", 0x12 to "irq", 0x13 to "svc", 0x17 to "abt", 0x1B to "und", 0x1F to "sys")
        val WHITESPACE = Regex("\\s+")
        val HEX_WORD = Regex("[0-9A-Fa-f]{8}")
    }
}

/** Nintendo's backward LZ, used for compressed overlays and ARM9 binaries (the footer sits at the end). */
internal object Blz {
    fun decompress(data: ByteArray): ByteArray {
        val n = data.size
        if (n < 8) return data
        fun u32(offset: Int) = (data[offset].toInt() and 0xFF) or ((data[offset + 1].toInt() and 0xFF) shl 8) or
            ((data[offset + 2].toInt() and 0xFF) shl 16) or ((data[offset + 3].toInt() and 0xFF) shl 24)
        val extra = u32(n - 4)
        if (extra == 0) return data
        val headerLength = data[n - 5].toInt() and 0xFF
        val encodedLength = u32(n - 8) and 0xFFFFFF
        val out = data.copyOf(n + extra)
        var src = n - headerLength
        var dst = n + extra
        val end = n - encodedLength
        while (src > end) {
            val flags = out[--src].toInt() and 0xFF
            for (bit in 7 downTo 0) {
                if (src <= end) break
                if (flags and (1 shl bit) == 0) {
                    out[--dst] = out[--src]
                } else {
                    val high = out[--src].toInt() and 0xFF
                    val low = out[--src].toInt() and 0xFF
                    val token = (high shl 8) or low
                    val length = (token shr 12) + 3
                    val distance = (token and 0xFFF) + 3
                    repeat(length) {
                        dst--
                        out[dst] = out[dst + distance]
                    }
                }
            }
        }
        return out
    }
}
