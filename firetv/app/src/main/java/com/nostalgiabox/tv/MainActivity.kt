package com.nostalgiabox.tv

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity(), Player.Listener {
    private val mainHandler = Handler(Looper.getMainLooper())
    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
    private lateinit var player: ExoPlayer
    private lateinit var playerView: PlayerView
    private lateinit var setupPanel: LinearLayout
    private lateinit var errorPanel: LinearLayout
    private lateinit var channelOverlay: LinearLayout
    private lateinit var serverUrlInput: EditText
    private lateinit var setupError: TextView
    private lateinit var playbackError: TextView
    private lateinit var channelNumber: TextView
    private lateinit var channelName: TextView
    private lateinit var programTitle: TextView

    private var baseUrl: String? = null
    private var api: NostalgiaApi? = null
    private var lineup: List<ChannelInfo> = emptyList()
    private var currentIndex = 0
    private var activeSessionId: String? = null
    private var pendingSession: PlaybackSession? = null
    private var tuneGeneration = 0
    private var retryAvailable = true

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        bindViews()
        player = ExoPlayer.Builder(this).build().also {
            it.addListener(this)
            playerView.player = it
        }

        findViewById<Button>(R.id.connect_button).setOnClickListener {
            connect(serverUrlInput.text.toString())
        }
        findViewById<Button>(R.id.retry_button).setOnClickListener {
            errorPanel.visibility = View.GONE
            retryAvailable = true
            tune(currentIndex)
        }
        findViewById<Button>(R.id.settings_button).setOnClickListener { showSettings() }

        val savedUrl = preferences().getString(SERVER_URL, null)
        if (savedUrl == null) showSettings() else connect(savedUrl)
    }

    private fun bindViews() {
        playerView = findViewById(R.id.player_view)
        setupPanel = findViewById(R.id.setup_panel)
        errorPanel = findViewById(R.id.error_panel)
        channelOverlay = findViewById(R.id.channel_overlay)
        serverUrlInput = findViewById(R.id.server_url)
        setupError = findViewById(R.id.setup_error)
        playbackError = findViewById(R.id.playback_error)
        channelNumber = findViewById(R.id.channel_number)
        channelName = findViewById(R.id.channel_name)
        programTitle = findViewById(R.id.program_title)
    }

    private fun connect(input: String) {
        val normalized = try {
            ServerUrl.normalize(input)
        } catch (error: IllegalArgumentException) {
            showSetupError(error.message ?: "Enter a valid server address.")
            return
        }
        setupError.visibility = View.GONE
        serverUrlInput.isEnabled = false
        val candidate = NostalgiaApi(normalized)
        executor.execute {
            try {
                candidate.health()
                val response = candidate.channels()
                mainHandler.post {
                    baseUrl = normalized
                    api = candidate
                    lineup = response.channels
                    currentIndex = lineup.indexOfFirst { it.number == response.startChannel }
                        .takeIf { it >= 0 } ?: 0
                    preferences().edit().putString(SERVER_URL, normalized).apply()
                    serverUrlInput.isEnabled = true
                    setupPanel.visibility = View.GONE
                    errorPanel.visibility = View.GONE
                    retryAvailable = true
                    tune(currentIndex)
                }
            } catch (error: Exception) {
                mainHandler.post {
                    serverUrlInput.isEnabled = true
                    showSetupError(error.message ?: "Could not connect to NostalgiaBox.")
                }
            }
        }
    }

    private fun tune(index: Int) {
        if (lineup.isEmpty()) return
        currentIndex = Math.floorMod(index, lineup.size)
        val generation = ++tuneGeneration
        val selected = lineup[currentIndex]
        val currentApi = api ?: return
        executor.execute {
            try {
                val session = currentApi.createPlayback(selected.number)
                val playbackUrl = PlaybackUrlSelector.select(requireNotNull(baseUrl), session)
                mainHandler.post {
                    if (generation != tuneGeneration) {
                        releaseInBackground(session.id)
                        return@post
                    }
                    pendingSession?.id?.let(::releaseInBackground)
                    pendingSession = session
                    player.setMediaItem(MediaItem.fromUri(playbackUrl))
                    if (session.deliveryMode == "direct") {
                        player.seekTo((session.initialOffsetSeconds * 1_000).toLong())
                    }
                    player.prepare()
                    player.playWhenReady = true
                    showChannel(selected, session.program)
                }
            } catch (error: Exception) {
                mainHandler.post {
                    if (generation == tuneGeneration) {
                        showPlaybackError(error.message ?: "Unable to tune this channel.")
                    }
                }
            }
        }
    }

    override fun onPlaybackStateChanged(playbackState: Int) {
        when (playbackState) {
            Player.STATE_READY -> {
                val readySession = pendingSession ?: return
                pendingSession = null
                val previous = activeSessionId
                activeSessionId = readySession.id
                if (previous != null && previous != readySession.id) releaseInBackground(previous)
                retryAvailable = true
                errorPanel.visibility = View.GONE
            }
            Player.STATE_ENDED -> {
                retryAvailable = true
                tune(currentIndex)
            }
        }
    }

    override fun onPlayerError(error: PlaybackException) {
        pendingSession?.id?.let(::releaseInBackground)
        pendingSession = null
        if (retryAvailable) {
            retryAvailable = false
            tune(currentIndex)
        } else {
            showPlaybackError("Playback failed: ${error.errorCodeName}")
        }
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.action != KeyEvent.ACTION_DOWN || setupPanel.visibility == View.VISIBLE) {
            return super.dispatchKeyEvent(event)
        }
        return when (event.keyCode) {
            KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_CHANNEL_UP -> {
                retryAvailable = true
                currentIndex = ChannelNavigator.move(currentIndex, 1, lineup.size)
                tune(currentIndex)
                true
            }
            KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_CHANNEL_DOWN -> {
                retryAvailable = true
                currentIndex = ChannelNavigator.move(currentIndex, -1, lineup.size)
                tune(currentIndex)
                true
            }
            KeyEvent.KEYCODE_DPAD_CENTER,
            KeyEvent.KEYCODE_ENTER,
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE -> {
                if (player.isPlaying) player.pause() else player.play()
                true
            }
            KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_SETTINGS -> {
                showSettings()
                true
            }
            else -> super.dispatchKeyEvent(event)
        }
    }

    private fun showChannel(channel: ChannelInfo, program: ProgramInfo) {
        channelNumber.text = getString(R.string.channel_number_format, channel.number)
        channelName.text = channel.name
        programTitle.text = program.title
        channelOverlay.visibility = View.VISIBLE
        mainHandler.removeCallbacks(hideOverlay)
        mainHandler.postDelayed(hideOverlay, 4_000)
    }

    private val hideOverlay = Runnable { channelOverlay.visibility = View.GONE }

    private fun showSettings() {
        player.stop()
        pendingSession?.id?.let(::releaseInBackground)
        activeSessionId?.let(::releaseInBackground)
        pendingSession = null
        activeSessionId = null
        errorPanel.visibility = View.GONE
        setupPanel.visibility = View.VISIBLE
        setupError.visibility = View.GONE
        serverUrlInput.setText(baseUrl ?: preferences().getString(SERVER_URL, ""))
        serverUrlInput.isEnabled = true
        serverUrlInput.requestFocus()
    }

    private fun showSetupError(message: String) {
        setupPanel.visibility = View.VISIBLE
        setupError.text = message
        setupError.visibility = View.VISIBLE
        serverUrlInput.requestFocus()
    }

    private fun showPlaybackError(message: String) {
        player.pause()
        playbackError.text = message
        errorPanel.visibility = View.VISIBLE
        findViewById<Button>(R.id.retry_button).requestFocus()
    }

    private fun releaseInBackground(sessionId: String) {
        val currentApi = api ?: return
        executor.execute { runCatching { currentApi.releasePlayback(sessionId) } }
    }

    private fun preferences() = getSharedPreferences(PREFERENCES, MODE_PRIVATE)

    override fun onDestroy() {
        mainHandler.removeCallbacksAndMessages(null)
        val sessions = listOfNotNull(pendingSession?.id, activeSessionId).distinct()
        val currentApi = api
        if (currentApi != null && sessions.isNotEmpty()) {
            Thread {
                sessions.forEach { runCatching { currentApi.releasePlayback(it) } }
            }.start()
        }
        player.removeListener(this)
        player.release()
        executor.shutdownNow()
        super.onDestroy()
    }

    companion object {
        private const val PREFERENCES = "nostalgiabox"
        private const val SERVER_URL = "server_url"
    }
}
