package me.magnum.melonds.impl

import java.io.File
import java.io.InputStream
import java.io.OutputStream

/**
 * Copies a save through a temporary snapshot. The picked file can be the target save itself under
 * another URI (a .sav next to the ROM, picked through a different provider); opening the target
 * truncates it, so reading the source afterwards would copy nothing and leave an empty save.
 */
internal fun copySaveWithSnapshot(
    cacheDirectory: File,
    maxBytes: Long,
    openSource: () -> InputStream,
    openTarget: () -> OutputStream,
) {
    val snapshot = File.createTempFile("save-import-", ".tmp", cacheDirectory)
    try {
        openSource().use { input ->
            snapshot.outputStream().use { output ->
                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                var size = 0L
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    size += count
                    require(size <= maxBytes) { "Selected save file is too large" }
                    output.write(buffer, 0, count)
                }
                require(size > 0) { "Selected save file is empty" }
            }
        }
        snapshot.inputStream().use { input ->
            openTarget().use { output -> input.copyTo(output) }
        }
    } finally {
        snapshot.delete()
    }
}
