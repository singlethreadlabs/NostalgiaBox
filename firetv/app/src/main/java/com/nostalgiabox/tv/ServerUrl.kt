package com.nostalgiabox.tv

import java.net.URI

object ServerUrl {
    fun normalize(input: String): String {
        val trimmed = input.trim()
        require(trimmed.isNotEmpty()) { "Enter the NostalgiaBox server address." }
        val withScheme = if (trimmed.contains("://")) trimmed else "http://$trimmed"
        val uri = try {
            URI(withScheme)
        } catch (_: Exception) {
            throw IllegalArgumentException("Enter a valid server address.")
        }
        require(uri.scheme in setOf("http", "https") && !uri.host.isNullOrBlank()) {
            "The server address must use HTTP or HTTPS and include a host."
        }
        require(uri.rawQuery == null && uri.rawFragment == null && uri.rawUserInfo == null) {
            "The server address cannot include credentials, a query, or a fragment."
        }
        val normalized = URI(
            uri.scheme.lowercase(),
            null,
            uri.host.lowercase(),
            uri.port,
            uri.path?.trimEnd('/').orEmpty(),
            null,
            null,
        )
        return normalized.toString().trimEnd('/')
    }
}

object PlaybackUrlSelector {
    fun select(baseUrl: String, session: PlaybackSession): String {
        val path = if (session.deliveryMode == "direct") {
            session.mediaUrl
        } else {
            requireNotNull(session.hlsUrl) { "The server did not provide an HLS stream." }
        }
        return URI(baseUrl.trimEnd('/') + "/").resolve(path.removePrefix("/")).toString()
    }
}

object ChannelNavigator {
    fun move(currentIndex: Int, delta: Int, channelCount: Int): Int {
        require(channelCount > 0) { "A channel lineup cannot be empty." }
        return Math.floorMod(currentIndex + delta, channelCount)
    }
}

class ChannelTuneQueue {
    private var pendingIndex: Int? = null

    fun request(index: Int) {
        pendingIndex = index
    }

    fun consume(): Int? = pendingIndex.also { pendingIndex = null }

    fun cancel() {
        pendingIndex = null
    }

    companion object {
        const val DELAY_MILLIS = 220L
    }
}
