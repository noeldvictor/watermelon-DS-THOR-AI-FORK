package me.magnum.melonds.debug

import android.app.Activity
import android.app.Application
import android.content.Context
import android.content.SharedPreferences
import android.os.Bundle
import android.util.Log
import androidx.core.content.edit
import androidx.preference.PreferenceManager
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import kotlin.concurrent.thread

/**
 * The on-device MCP server (tools/thor_mcp/README.md). Debug builds only: this source set is
 * not compiled into release, which also hides its settings toggle.
 *
 * Binds 127.0.0.1 only and is reached over `adb forward tcp:27184 tcp:27184` (ARMSX2 uses
 * 27183 on the same Thor). Off by default; started by the "Dev server (MCP)" toggle in
 * Settings -> General, by the START_DEV_SERVER broadcast, or by launching an activity with
 * `--ez devserver true`. It belongs to the process, so it keeps running while a game does.
 *
 * Transport is MCP's Streamable HTTP in its simplest legal form: JSON-RPC 2.0 over
 * `POST /mcp` with `application/json` responses, no SSE. Every tool is also reachable as
 * `POST /tool/<name>` with the arguments as the body, so a plain curl works without an MCP
 * client. The tools themselves are in [DevTools]; they share their code with the adb
 * broadcasts through [DebugCommands].
 *
 * The HTTP parsing is deliberately hand-rolled: a few dozen lines against one trusted peer on
 * localhost is not worth a server dependency.
 */
internal object DevServer {
    private const val TAG = "DevServer"
    const val PORT = 27184
    const val PREF_ENABLED = "dev_mcp_server_enabled"
    private const val EXTRA_DEVSERVER = "devserver"
    private const val MAX_BODY_BYTES = 1 shl 20
    private const val SERVER_NAME = "watermelon-thor"
    private val LOOPBACK_NAMES = setOf("127.0.0.1", "localhost", "[::1]")

    @Volatile private var server: ServerSocket? = null
    @Volatile private var appContext: Context? = null
    private val workers = Executors.newFixedThreadPool(3) { runnable ->
        Thread(runnable, "melonds-devserver-worker").apply { isDaemon = true }
    }

    // Activities in front (identity -> class name), for the status tool. Two can be resumed at
    // once on the Thor, one per display.
    private val resumedActivities = ConcurrentHashMap<Int, String>()

    // SharedPreferences holds its listeners weakly: this field keeps the toggle's alive.
    private val preferenceListener = SharedPreferences.OnSharedPreferenceChangeListener { preferences, key ->
        if (key == PREF_ENABLED) {
            val context = appContext ?: return@OnSharedPreferenceChangeListener
            val on = preferences.getBoolean(PREF_ENABLED, false)
            workers.execute { if (on) start(context) else stop() }
        }
    }

    /** Called once at app start ([DevServerInitializer]). */
    fun install(context: Context) {
        val application = context.applicationContext as? Application ?: return
        appContext = application
        val preferences = PreferenceManager.getDefaultSharedPreferences(application)
        preferences.registerOnSharedPreferenceChangeListener(preferenceListener)
        application.registerActivityLifecycleCallbacks(
            object : Application.ActivityLifecycleCallbacks {
                override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) {
                    // `am start ... --ez devserver true|false`; only a fresh start, so a rotation
                    // doesn't undo a later STOP_DEV_SERVER
                    val intent = activity.intent
                    if (savedInstanceState == null && intent?.hasExtra(EXTRA_DEVSERVER) == true) {
                        val on = intent.getBooleanExtra(EXTRA_DEVSERVER, false)
                        workers.execute { if (on) start(application) else stop() }
                    }
                }

                override fun onActivityResumed(activity: Activity) {
                    resumedActivities[System.identityHashCode(activity)] = activity.javaClass.simpleName
                }

                override fun onActivityPaused(activity: Activity) {
                    resumedActivities.remove(System.identityHashCode(activity))
                }

                override fun onActivityDestroyed(activity: Activity) {
                    resumedActivities.remove(System.identityHashCode(activity))
                }

                override fun onActivityStarted(activity: Activity) = Unit

                override fun onActivityStopped(activity: Activity) = Unit

                override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) = Unit
            },
        )
        if (preferences.getBoolean(PREF_ENABLED, false)) {
            workers.execute { start(application) }
        }
    }

    fun resumedActivities(): List<String> = resumedActivities.values.sorted()

    fun isRunning(): Boolean = server?.isClosed == false

    fun isEnabled(context: Context): Boolean =
        PreferenceManager.getDefaultSharedPreferences(context.applicationContext).getBoolean(PREF_ENABLED, false)

    /** Persists the settings toggle and starts or stops the server to match. */
    fun setEnabled(context: Context, on: Boolean) {
        PreferenceManager.getDefaultSharedPreferences(context.applicationContext).edit(commit = true) {
            putBoolean(PREF_ENABLED, on)
        }
        if (on) start(context) else stop()
    }

    @Synchronized
    fun start(context: Context): Boolean {
        appContext = context.applicationContext
        if (isRunning()) return true
        val socket = try {
            ServerSocket(PORT, 8, InetAddress.getLoopbackAddress())
        } catch (e: Exception) {
            Log.e(TAG, "bind 127.0.0.1:$PORT failed: ${e.message}")
            return false
        }
        server = socket
        Log.w(TAG, "MCP server listening on 127.0.0.1:$PORT (adb forward tcp:$PORT tcp:$PORT)")
        thread(name = "melonds-devserver", isDaemon = true) {
            while (!socket.isClosed) {
                val client = try {
                    socket.accept()
                } catch (_: Exception) {
                    break
                }
                workers.execute {
                    runCatching { handle(client) }.onFailure { Log.w(TAG, "request failed: ${it.message}") }
                }
            }
        }
        return true
    }

    @Synchronized
    fun stop() {
        val socket = server ?: return
        runCatching { socket.close() }
        server = null
        Log.w(TAG, "MCP server stopped")
    }

    fun statusJson(context: Context): JSONObject {
        return JSONObject()
            .put("running", isRunning())
            .put("enabled", isEnabled(context))
            .put("port", PORT)
            .put("url", "http://127.0.0.1:$PORT/mcp")
    }

    // ---- HTTP -------------------------------------------------------------------------------

    private class Request(val method: String, val path: String, val headers: Map<String, String>, val body: ByteArray)

    private class HttpError(val status: Int, message: String) : Exception(message)

    private fun handle(client: Socket) {
        client.soTimeout = 15_000
        client.use { c ->
            val output = c.getOutputStream()
            val request = try {
                readRequest(BufferedInputStream(c.getInputStream()), output) ?: return
            } catch (e: HttpError) {
                json(output, e.status, JSONObject().put("error", e.message))
                output.flush()
                return
            }
            try {
                route(request, output)
            } catch (e: Exception) {
                Log.w(TAG, "${request.method} ${request.path}: $e")
                json(output, 500, JSONObject().put("error", e.toString()))
            }
            output.flush()
        }
    }

    private fun readRequest(input: InputStream, output: OutputStream): Request? {
        val line = readLine(input) ?: return null
        val parts = line.trim().split(' ')
        if (parts.size < 2) return null
        val headers = HashMap<String, String>()
        while (true) {
            val h = readLine(input) ?: return null
            if (h.isEmpty()) break
            val idx = h.indexOf(':')
            if (idx > 0) headers[h.substring(0, idx).trim().lowercase()] = h.substring(idx + 1).trim()
            if (headers.size > 100) throw HttpError(431, "too many headers")
        }
        checkOrigin(headers)
        if (headers["transfer-encoding"]?.contains("chunked", ignoreCase = true) == true) {
            throw HttpError(411, "send a Content-Length body, not chunked")
        }
        val length = headers["content-length"]?.toIntOrNull() ?: 0
        if (length !in 0..MAX_BODY_BYTES) throw HttpError(413, "body too large")
        if (length > 0 && headers["expect"].equals("100-continue", ignoreCase = true)) {
            output.write("HTTP/1.1 100 Continue\r\n\r\n".toByteArray())
            output.flush()
        }
        val body = ByteArray(length)
        var read = 0
        while (read < length) {
            val n = input.read(body, read, length - read)
            if (n < 0) break
            read += n
        }
        return Request(parts[0].uppercase(), parts[1], headers, body)
    }

    /**
     * Only local, non-browser callers. adb forward keeps the Host a loopback name; a browser page
     * on the device (or a DNS-rebound one) sends an Origin, which the MCP spec says to refuse.
     */
    private fun checkOrigin(headers: Map<String, String>) {
        val host = headers["host"]?.let { hostName(it) }
        if (host != null && host !in LOOPBACK_NAMES) throw HttpError(403, "host $host refused")
        val origin = headers["origin"] ?: return
        val originHost = hostName(origin.substringAfter("://"))
        if (originHost !in LOOPBACK_NAMES) throw HttpError(403, "origin $origin refused")
    }

    private fun hostName(authority: String): String {
        val value = authority.trim().lowercase()
        return if (value.startsWith("[")) value.substringBefore(']') + "]" else value.substringBefore(':').substringBefore('/')
    }

    private fun readLine(input: InputStream): String? {
        val buf = ByteArrayOutputStream()
        while (true) {
            val b = input.read()
            if (b < 0) return if (buf.size() == 0) null else buf.toString("UTF-8")
            if (b == '\n'.code) break
            if (b != '\r'.code) buf.write(b)
            if (buf.size() > 16384) return null
        }
        return buf.toString("UTF-8")
    }

    private fun respond(out: OutputStream, status: Int, type: String, body: ByteArray, extraHeaders: String = "") {
        val reason = when (status) {
            200 -> "OK"
            202 -> "Accepted"
            400 -> "Bad Request"
            403 -> "Forbidden"
            404 -> "Not Found"
            405 -> "Method Not Allowed"
            411 -> "Length Required"
            413 -> "Payload Too Large"
            431 -> "Request Header Fields Too Large"
            else -> "Error"
        }
        val head = "HTTP/1.1 $status $reason\r\nContent-Type: $type\r\nContent-Length: ${body.size}\r\n${extraHeaders}Connection: close\r\n\r\n"
        out.write(head.toByteArray())
        out.write(body)
    }

    private fun json(out: OutputStream, status: Int, obj: Any) =
        respond(out, status, "application/json", obj.toString().toByteArray())

    private fun route(req: Request, out: OutputStream) {
        val context = appContext ?: return json(out, 500, JSONObject().put("error", "no context"))
        val tools = DevTools(context)
        val path = req.path.substringBefore('?')
        when {
            req.method == "GET" && (path == "/" || path == "/info") ->
                json(out, 200, JSONObject()
                    .put("name", SERVER_NAME)
                    .put("version", tools.versionName())
                    .put("mcp", "/mcp")
                    .put("tools", JSONArray(tools.list().map { it.getString("name") })))

            req.method == "POST" && path.startsWith("/tool/") -> {
                val name = path.removePrefix("/tool/")
                val args = runCatching { JSONObject(String(req.body, Charsets.UTF_8).ifBlank { "{}" }) }.getOrElse { JSONObject() }
                val result = tools.call(name, args)
                json(out, if (result.has("error")) 400 else 200, result)
            }

            path == "/mcp" && req.method == "POST" -> {
                val message = runCatching { JSONObject(String(req.body, Charsets.UTF_8)) }.getOrNull()
                    ?: return json(out, 400, rpcError(JSONObject.NULL, -32700, "parse error (batches are not supported)"))
                val reply = rpc(message, tools)
                if (reply == null) respond(out, 202, "text/plain", ByteArray(0)) else json(out, 200, reply)
            }

            // no SSE stream and no sessions to delete
            path == "/mcp" -> respond(out, 405, "text/plain", ByteArray(0), "Allow: POST\r\n")

            else -> json(out, 404, JSONObject().put("error", "unknown route ${req.method} ${req.path}"))
        }
    }

    // ---- MCP / JSON-RPC ---------------------------------------------------------------------

    private fun rpcError(id: Any, code: Int, message: String): JSONObject =
        JSONObject().put("jsonrpc", "2.0").put("id", id).put("error", JSONObject().put("code", code).put("message", message))

    private fun rpcResult(id: Any, result: JSONObject): JSONObject =
        JSONObject().put("jsonrpc", "2.0").put("id", id).put("result", result)

    /** Returns null for notifications and client responses (no reply). */
    private fun rpc(message: JSONObject, tools: DevTools): JSONObject? {
        if (!message.has("id") || !message.has("method")) return null
        val method = message.optString("method")
        val id: Any = message.opt("id") ?: JSONObject.NULL
        val params = message.optJSONObject("params") ?: JSONObject()
        return when (method) {
            "initialize" -> rpcResult(id, JSONObject()
                .put("protocolVersion", params.optString("protocolVersion").ifEmpty { "2025-06-18" })
                .put("capabilities", JSONObject().put("tools", JSONObject().put("listChanged", false)))
                .put("serverInfo", JSONObject().put("name", SERVER_NAME).put("version", tools.versionName()))
                .put("instructions", "Watermelon Thor's on-device dev server (debug build, reached over adb forward). " +
                    "Status, settings, the ROM library, launching, save states, pause/resume and the app's own log and " +
                    "HD pack / compositor stats. Screenshots, taps and force-stop stay on the PC-side `thor` server. " +
                    "The Thor is shared with other sessions: call status first and don't take it while another app is in front."))
            "ping" -> rpcResult(id, JSONObject())
            "tools/list" -> rpcResult(id, JSONObject().put("tools", JSONArray(tools.list())))
            "tools/call" -> {
                val name = params.optString("name")
                if (!tools.has(name)) return rpcError(id, -32602, "unknown tool: $name")
                val args = params.optJSONObject("arguments") ?: JSONObject()
                val result = tools.call(name, args)
                rpcResult(id, JSONObject()
                    .put("content", JSONArray().put(JSONObject().put("type", "text").put("text", result.toString(1))))
                    .put("isError", result.has("error")))
            }
            else -> rpcError(id, -32601, "method not found: $method")
        }
    }
}
