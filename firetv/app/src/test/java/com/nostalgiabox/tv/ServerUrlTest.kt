package com.nostalgiabox.tv

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class ServerUrlTest {
    @Test
    fun normalizesLanAddresses() {
        assertEquals("http://192.168.1.20:8080", ServerUrl.normalize(" 192.168.1.20:8080/ "))
        assertEquals("https://box.local/base", ServerUrl.normalize("https://BOX.local/base/"))
    }

    @Test
    fun rejectsUnsupportedOrAmbiguousAddresses() {
        assertThrows(IllegalArgumentException::class.java) { ServerUrl.normalize("") }
        assertThrows(IllegalArgumentException::class.java) { ServerUrl.normalize("ftp://box.local") }
        assertThrows(IllegalArgumentException::class.java) { ServerUrl.normalize("http://user@box.local") }
        assertThrows(IllegalArgumentException::class.java) { ServerUrl.normalize("http://box.local?a=1") }
    }

    @Test
    fun selectsDirectMediaAndHlsFallback() {
        val direct = PlaybackSession("1", "direct", "/media/4", null, 120.0, ProgramInfo("Show"))
        val transcode = PlaybackSession(
            "2",
            "transcode",
            "/sessions/2/stream.mp4",
            "/sessions/2/hls/index.m3u8",
            120.0,
            ProgramInfo("Show"),
        )
        assertEquals("http://box:8080/media/4", PlaybackUrlSelector.select("http://box:8080", direct))
        assertEquals(
            "http://box:8080/sessions/2/hls/index.m3u8",
            PlaybackUrlSelector.select("http://box:8080", transcode),
        )
    }

    @Test
    fun wrapsChannelNavigation() {
        assertEquals(0, ChannelNavigator.move(2, 1, 3))
        assertEquals(2, ChannelNavigator.move(0, -1, 3))
        assertEquals(1, ChannelNavigator.move(0, 1, 3))
    }
}
