package com.nostalgiabox.tv

import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.KeyEvent
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.ceil

class MainActivity : AppCompatActivity(), Player.Listener {
    private val mainHandler = Handler(Looper.getMainLooper())
    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
    private val pinLimiter = PinAttemptLimiter(SystemClock::elapsedRealtime)
    private val parentAccess = ParentAccessController()
    private lateinit var pinStore: PinStore
    private lateinit var player: ExoPlayer
    private lateinit var playerView: PlayerView
    private lateinit var setupPanel: LinearLayout
    private lateinit var settingsActions: LinearLayout
    private lateinit var errorPanel: LinearLayout
    private lateinit var channelOverlay: LinearLayout
    private lateinit var pinSetupPanel: LinearLayout
    private lateinit var pinEntryPanel: LinearLayout
    private lateinit var parentMenuPanel: LinearLayout
    private lateinit var serverUrlInput: EditText
    private lateinit var newPinInput: EditText
    private lateinit var confirmPinInput: EditText
    private lateinit var parentPinInput: EditText
    private lateinit var setupError: TextView
    private lateinit var playbackError: TextView
    private lateinit var pinSetupTitle: TextView
    private lateinit var pinSetupError: TextView
    private lateinit var pinEntryError: TextView
    private lateinit var channelNumber: TextView
    private lateinit var channelName: TextView
    private lateinit var programTitle: TextView
    private lateinit var pinSetupCancelButton: Button

    private var baseUrl: String? = null
    private var api: NostalgiaApi? = null
    private var lineup: List<ChannelInfo> = emptyList()
    private var currentIndex = 0
    private var activeSessionId: String? = null
    private var pendingSession: PlaybackSession? = null
    private var tuneGeneration = 0
    private var retryAvailable = true
    private var changingPin = false
    private var menuDownTime: Long? = null
    private var restoreErrorAfterPin = false

    private val parentUnlocked: Boolean
        get() = parentAccess.isUnlocked

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        bindViews()
        pinStore = PinStore(preferences())
        player = ExoPlayer.Builder(this).build().also {
            it.addListener(this)
            playerView.player = it
        }
        bindActions()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() = handleBack()
        })

        val savedUrl = preferences().getString(SERVER_URL, null)
        if (savedUrl == null) showSettings(parentAccess = false) else connect(savedUrl)
    }

    override fun onResume() {
        super.onResume()
        if (parentUnlocked) lockKidMode(resumePlayback = true)
    }

    private fun bindViews() {
        playerView = findViewById(R.id.player_view)
        setupPanel = findViewById(R.id.setup_panel)
        settingsActions = findViewById(R.id.settings_actions)
        errorPanel = findViewById(R.id.error_panel)
        channelOverlay = findViewById(R.id.channel_overlay)
        pinSetupPanel = findViewById(R.id.pin_setup_panel)
        pinEntryPanel = findViewById(R.id.pin_entry_panel)
        parentMenuPanel = findViewById(R.id.parent_menu_panel)
        serverUrlInput = findViewById(R.id.server_url)
        newPinInput = findViewById(R.id.new_pin)
        confirmPinInput = findViewById(R.id.confirm_pin)
        parentPinInput = findViewById(R.id.parent_pin)
        setupError = findViewById(R.id.setup_error)
        playbackError = findViewById(R.id.playback_error)
        pinSetupTitle = findViewById(R.id.pin_setup_title)
        pinSetupError = findViewById(R.id.pin_setup_error)
        pinEntryError = findViewById(R.id.pin_entry_error)
        channelNumber = findViewById(R.id.channel_number)
        channelName = findViewById(R.id.channel_name)
        programTitle = findViewById(R.id.program_title)
        pinSetupCancelButton = findViewById(R.id.pin_setup_cancel_button)
    }

    private fun bindActions() {
        findViewById<Button>(R.id.connect_button).setOnClickListener {
            connect(serverUrlInput.text.toString())
        }
        findViewById<Button>(R.id.retry_button).setOnClickListener {
            errorPanel.visibility = View.GONE
            retryAvailable = true
            if (lineup.isEmpty()) {
                preferences().getString(SERVER_URL, null)?.let(::connect)
            } else {
                tune(currentIndex)
            }
        }
        findViewById<Button>(R.id.settings_button).setOnClickListener { showPinEntry() }
        findViewById<Button>(R.id.unlock_button).setOnClickListener { verifyParentPin() }
        findViewById<Button>(R.id.pin_cancel_button).setOnClickListener { hidePinEntry() }
        findViewById<Button>(R.id.save_pin_button).setOnClickListener { savePin() }
        pinSetupCancelButton.setOnClickListener { showSettings(parentAccess = true) }
        findViewById<Button>(R.id.change_pin_button).setOnClickListener { showPinSetup(change = true) }
        findViewById<Button>(R.id.settings_cancel_button).setOnClickListener { showParentMenu() }
        findViewById<Button>(R.id.fire_home_button).setOnClickListener { openFireHome() }
        findViewById<Button>(R.id.parent_settings_button).setOnClickListener {
            showSettings(parentAccess = true)
        }
        findViewById<Button>(R.id.relock_button).setOnClickListener {
            lockKidMode(resumePlayback = true)
        }
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
                    if (pinStore.hasPin()) {
                        lockKidMode(resumePlayback = true)
                    } else {
                        showPinSetup(change = false)
                    }
                }
            } catch (error: Exception) {
                mainHandler.post {
                    serverUrlInput.isEnabled = true
                    val message = error.message ?: "Could not connect to NostalgiaBox."
                    if (pinStore.hasPin() && !parentUnlocked) {
                        showPlaybackError(message)
                    } else {
                        showSetupError(message)
                    }
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
        if (event.keyCode == KeyEvent.KEYCODE_BACK) {
            if (event.action == KeyEvent.ACTION_UP) handleBack()
            return true
        }
        if (event.keyCode == KeyEvent.KEYCODE_MENU || event.keyCode == KeyEvent.KEYCODE_SETTINGS) {
            handleMenuKey(event)
            return true
        }
        if (event.action != KeyEvent.ACTION_DOWN || anyPanelVisible()) {
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
            else -> super.dispatchKeyEvent(event)
        }
    }

    private fun handleMenuKey(event: KeyEvent) {
        if (!pinStore.hasPin() || anyPanelVisible()) return
        when (event.action) {
            KeyEvent.ACTION_DOWN -> {
                if (menuDownTime == null) {
                    menuDownTime = event.eventTime
                    mainHandler.postDelayed(openParentAccess, MenuHoldPolicy.HOLD_MILLIS)
                } else if (MenuHoldPolicy.isSatisfied(requireNotNull(menuDownTime), event.eventTime)) {
                    openParentAccess.run()
                }
            }
            KeyEvent.ACTION_UP -> cancelMenuHold()
        }
    }

    private val openParentAccess = Runnable {
        if (menuDownTime != null && !anyPanelVisible()) showPinEntry()
        cancelMenuHold()
    }

    private fun cancelMenuHold() {
        menuDownTime = null
        mainHandler.removeCallbacks(openParentAccess)
    }

    private fun handleBack() {
        cancelMenuHold()
        when {
            pinEntryPanel.visibility == View.VISIBLE -> hidePinEntry()
            parentMenuPanel.visibility == View.VISIBLE -> lockKidMode(resumePlayback = true)
            setupPanel.visibility == View.VISIBLE && parentUnlocked -> showParentMenu()
            pinSetupPanel.visibility == View.VISIBLE && changingPin -> showSettings(parentAccess = true)
        }
    }

    private fun showPinSetup(change: Boolean) {
        hidePanels()
        changingPin = change
        pinSetupTitle.setText(if (change) R.string.change_parent_pin else R.string.create_parent_pin)
        pinSetupCancelButton.visibility = if (change) View.VISIBLE else View.GONE
        newPinInput.text.clear()
        confirmPinInput.text.clear()
        pinSetupError.visibility = View.GONE
        pinSetupPanel.visibility = View.VISIBLE
        newPinInput.requestFocus()
    }

    private fun savePin() {
        val pin = newPinInput.text.toString()
        val confirmation = confirmPinInput.text.toString()
        val error = when (PinPolicy.validateSetup(pin, confirmation)) {
            PinSetupError.INVALID -> getString(R.string.pin_must_be_four_digits)
            PinSetupError.MISMATCH -> getString(R.string.pins_do_not_match)
            null -> null
        }
        if (error != null) {
            pinSetupError.text = error
            pinSetupError.visibility = View.VISIBLE
            newPinInput.requestFocus()
            return
        }
        pinStore.set(pin)
        pinLimiter.recordSuccess()
        if (changingPin) showSettings(parentAccess = true) else lockKidMode(resumePlayback = true)
    }

    private fun showPinEntry() {
        if (!pinStore.hasPin()) return
        parentAccess.requestPin()
        if (parentAccess.state != ParentAccessState.PIN_ENTRY) return
        restoreErrorAfterPin = errorPanel.visibility == View.VISIBLE
        hidePanels()
        parentPinInput.text.clear()
        pinEntryError.visibility = View.GONE
        pinEntryPanel.visibility = View.VISIBLE
        parentPinInput.requestFocus()
    }

    private fun verifyParentPin() {
        val remaining = pinLimiter.remainingLockoutMillis()
        if (remaining > 0) {
            showPinEntryError(lockoutMessage(remaining))
            return
        }
        if (!pinStore.verify(parentPinInput.text.toString())) {
            pinLimiter.recordFailure()
            val newlyLocked = pinLimiter.remainingLockoutMillis()
            val message = if (newlyLocked > 0) {
                lockoutMessage(newlyLocked)
            } else {
                getString(R.string.incorrect_pin)
            }
            showPinEntryError(message)
            parentPinInput.text.clear()
            parentPinInput.requestFocus()
            return
        }
        pinLimiter.recordSuccess()
        parentAccess.acceptPin()
        restoreErrorAfterPin = false
        stopPlaybackAndRelease()
        showParentMenu()
    }

    private fun showPinEntryError(message: String) {
        pinEntryError.text = message
        pinEntryError.visibility = View.VISIBLE
    }

    private fun lockoutMessage(remainingMillis: Long): String {
        val seconds = ceil(remainingMillis / 1_000.0).toInt()
        return resources.getQuantityString(R.plurals.pin_locked, seconds, seconds)
    }

    private fun hidePinEntry() {
        parentAccess.back()
        pinEntryPanel.visibility = View.GONE
        pinEntryError.visibility = View.GONE
        parentPinInput.text.clear()
        if (restoreErrorAfterPin) {
            errorPanel.visibility = View.VISIBLE
            findViewById<Button>(R.id.retry_button).requestFocus()
        }
        restoreErrorAfterPin = false
    }

    private fun showParentMenu() {
        if (!parentUnlocked) return
        if (parentAccess.state == ParentAccessState.SETTINGS) parentAccess.back()
        hidePanels()
        parentMenuPanel.visibility = View.VISIBLE
        findViewById<Button>(R.id.fire_home_button).requestFocus()
    }

    private fun openFireHome() {
        if (!parentUnlocked) return
        val homeIntent = Intent(Intent.ACTION_MAIN)
            .addCategory(Intent.CATEGORY_HOME)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        startActivity(homeIntent)
    }

    private fun lockKidMode(resumePlayback: Boolean) {
        parentAccess.relock()
        changingPin = false
        hidePanels()
        if (resumePlayback && lineup.isNotEmpty()) {
            retryAvailable = true
            tune(currentIndex)
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

    private fun showSettings(parentAccess: Boolean) {
        if (parentAccess && !parentUnlocked) return
        if (parentAccess) this.parentAccess.openSettings()
        stopPlaybackAndRelease()
        hidePanels()
        setupPanel.visibility = View.VISIBLE
        settingsActions.visibility = if (parentAccess) View.VISIBLE else View.GONE
        setupError.visibility = View.GONE
        serverUrlInput.setText(baseUrl ?: preferences().getString(SERVER_URL, ""))
        serverUrlInput.isEnabled = true
        serverUrlInput.requestFocus()
    }

    private fun showSetupError(message: String) {
        hidePanels()
        setupPanel.visibility = View.VISIBLE
        settingsActions.visibility = if (parentUnlocked) View.VISIBLE else View.GONE
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

    private fun hidePanels() {
        setupPanel.visibility = View.GONE
        errorPanel.visibility = View.GONE
        pinSetupPanel.visibility = View.GONE
        pinEntryPanel.visibility = View.GONE
        parentMenuPanel.visibility = View.GONE
    }

    private fun anyPanelVisible(): Boolean =
        setupPanel.visibility == View.VISIBLE ||
            errorPanel.visibility == View.VISIBLE ||
            pinSetupPanel.visibility == View.VISIBLE ||
            pinEntryPanel.visibility == View.VISIBLE ||
            parentMenuPanel.visibility == View.VISIBLE

    private fun stopPlaybackAndRelease() {
        ++tuneGeneration
        player.stop()
        pendingSession?.id?.let(::releaseInBackground)
        activeSessionId?.let(::releaseInBackground)
        pendingSession = null
        activeSessionId = null
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
