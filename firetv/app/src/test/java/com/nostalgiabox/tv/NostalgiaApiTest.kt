package com.nostalgiabox.tv

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Before
import org.junit.Test

class NostalgiaApiTest {
    private lateinit var server: MockWebServer
    private lateinit var api: NostalgiaApi

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        api = NostalgiaApi(server.url("/").toString().trimEnd('/'))
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun validatesHealthAndParsesLineup() {
        server.enqueue(json("""{"status":"ready"}"""))
        server.enqueue(
            json(
                """{"start_channel":2,"channels":[{"channel":{"number":2,"name":"Orbit"},"program":{"title":"Episode"}}]}"""
            )
        )

        api.health()
        val lineup = api.channels()

        assertEquals(2, lineup.startChannel)
        assertEquals(ChannelInfo(2, "Orbit", ProgramInfo("Episode")), lineup.channels.single())
    }

    @Test
    fun parsesDirectAndHlsPlaybackResponses() {
        server.enqueue(
            json(
                """{"id":"direct","delivery_mode":"direct","media_url":"/api/v1/media/1","hls_url":null,"initial_offset_seconds":420.5,"program":{"title":"Direct"}}"""
            )
        )
        server.enqueue(
            json(
                """{"id":"hls","delivery_mode":"transcode","media_url":"/stream.mp4","hls_url":"/hls/index.m3u8","initial_offset_seconds":420.5,"program":{"title":"HLS"}}"""
            )
        )

        val direct = api.createPlayback(2)
        assertEquals("/api/v1/media/1", direct.mediaUrl)
        assertEquals(420.5, direct.initialOffsetSeconds, 0.0)
        assertEquals("/hls/index.m3u8", api.createPlayback(3).hlsUrl)
        assertEquals("POST", server.takeRequest().method)
        assertEquals("POST", server.takeRequest().method)
    }

    @Test
    fun reportsUnavailableAndInvalidResponses() {
        server.enqueue(MockResponse().setResponseCode(503).setBody("""{"detail":"not ready"}"""))
        assertEquals(
            "not ready",
            assertThrows(ApiException::class.java) { api.health() }.message,
        )

        server.enqueue(json("not-json"))
        assertEquals(
            "The server returned an invalid response.",
            assertThrows(ApiException::class.java) { api.channels() }.message,
        )
    }

    @Test
    fun reportsNetworkFailure() {
        server.shutdown()
        val error = assertThrows(ApiException::class.java) { api.health() }
        assert(error.message.orEmpty().startsWith("Could not reach NostalgiaBox:"))
    }

    private fun json(body: String) = MockResponse()
        .setHeader("Content-Type", "application/json")
        .setBody(body)
}
