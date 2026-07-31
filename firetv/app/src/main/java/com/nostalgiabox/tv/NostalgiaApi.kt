package com.nostalgiabox.tv

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL

class ApiException(message: String, cause: Throwable? = null) : IOException(message, cause)

class NostalgiaApi(private val baseUrl: String) {
    fun health() {
        val payload = request("GET", "/api/v1/health")
        if (payload.optString("status") != "ready") {
            throw ApiException("NostalgiaBox is reachable but is not ready.")
        }
    }

    fun channels(): ChannelLineup {
        val payload = request("GET", "/api/v1/channels")
        val array = payload.optJSONArray("channels")
            ?: throw ApiException("The server returned an invalid channel lineup.")
        val channels = buildList {
            for (index in 0 until array.length()) {
                val entry = array.optJSONObject(index)
                    ?: throw ApiException("The server returned an invalid channel entry.")
                val channel = entry.optJSONObject("channel")
                    ?: throw ApiException("A channel is missing its identity.")
                val program = entry.optJSONObject("program")
                    ?: throw ApiException("A channel is missing its current program.")
                add(
                    ChannelInfo(
                        number = channel.getInt("number"),
                        name = channel.getString("name"),
                        program = ProgramInfo(program.getString("title")),
                    )
                )
            }
        }
        if (channels.isEmpty()) throw ApiException("The server has no configured channels.")
        return ChannelLineup(payload.getInt("start_channel"), channels)
    }

    fun createPlayback(channelNumber: Int): PlaybackSession {
        val payload = request(
            "POST",
            "/api/v1/playback-sessions",
            JSONObject().put("channel_number", channelNumber).toString(),
        )
        val program = payload.optJSONObject("program")
            ?: throw ApiException("The playback response is missing program information.")
        return PlaybackSession(
            id = payload.getString("id"),
            deliveryMode = payload.getString("delivery_mode"),
            mediaUrl = payload.getString("media_url"),
            hlsUrl = payload.optString("hls_url").takeIf { it.isNotBlank() && it != "null" },
            initialOffsetSeconds = payload.getDouble("initial_offset_seconds"),
            program = ProgramInfo(program.getString("title")),
        )
    }

    fun releasePlayback(sessionId: String) {
        request("DELETE", "/api/v1/playback-sessions/$sessionId", expectBody = false)
    }

    private fun request(
        method: String,
        path: String,
        body: String? = null,
        expectBody: Boolean = true,
    ): JSONObject {
        val connection = (URL(resolve(path)).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 5_000
            readTimeout = 10_000
            setRequestProperty("Accept", "application/json")
            if (body != null) {
                doOutput = true
                setRequestProperty("Content-Type", "application/json")
            }
        }
        return try {
            if (body != null) connection.outputStream.bufferedWriter().use { it.write(body) }
            val status = connection.responseCode
            val response = (if (status in 200..299) connection.inputStream else connection.errorStream)
                ?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (status !in 200..299) {
                val detail = runCatching { JSONObject(response).optString("detail") }.getOrNull()
                throw ApiException(detail?.takeIf { it.isNotBlank() } ?: "Server request failed ($status).")
            }
            if (!expectBody) JSONObject() else try {
                JSONObject(response)
            } catch (error: Exception) {
                throw ApiException("The server returned an invalid response.", error)
            }
        } catch (error: ApiException) {
            throw error
        } catch (error: IOException) {
            throw ApiException("Could not reach NostalgiaBox: ${error.message ?: "network error"}", error)
        } finally {
            connection.disconnect()
        }
    }

    private fun resolve(path: String): String =
        URI(baseUrl.trimEnd('/') + "/").resolve(path.removePrefix("/")).toString()
}
