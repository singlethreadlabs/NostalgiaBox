package com.nostalgiabox.tv

import android.content.SharedPreferences
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec

data class PinRecord(val saltHex: String, val hashHex: String)

class PinCodec(private val secureRandom: SecureRandom = SecureRandom()) {
    fun create(pin: String): PinRecord {
        require(PinPolicy.isValid(pin)) { "PIN must contain exactly four digits." }
        val salt = ByteArray(SALT_BYTES).also(secureRandom::nextBytes)
        return PinRecord(salt.toHex(), derive(pin, salt).toHex())
    }

    fun verify(pin: String, record: PinRecord): Boolean {
        if (!PinPolicy.isValid(pin)) return false
        val salt = record.saltHex.hexToBytes() ?: return false
        val expected = record.hashHex.hexToBytes() ?: return false
        return MessageDigest.isEqual(expected, derive(pin, salt))
    }

    private fun derive(pin: String, salt: ByteArray): ByteArray {
        val spec = PBEKeySpec(pin.toCharArray(), salt, ITERATIONS, KEY_BITS)
        return try {
            SecretKeyFactory.getInstance("PBKDF2WithHmacSHA1").generateSecret(spec).encoded
        } finally {
            spec.clearPassword()
        }
    }

    private fun ByteArray.toHex(): String = joinToString("") { byte -> "%02x".format(byte) }

    private fun String.hexToBytes(): ByteArray? {
        if (length % 2 != 0 || any { it.digitToIntOrNull(16) == null }) return null
        return ByteArray(length / 2) { index ->
            substring(index * 2, index * 2 + 2).toInt(16).toByte()
        }
    }

    private companion object {
        const val SALT_BYTES = 16
        const val ITERATIONS = 120_000
        const val KEY_BITS = 256
    }
}

class PinStore(
    private val preferences: SharedPreferences,
    private val codec: PinCodec = PinCodec(),
) {
    fun hasPin(): Boolean = preferences.contains(PIN_SALT) && preferences.contains(PIN_HASH)

    fun set(pin: String) {
        val record = codec.create(pin)
        preferences.edit()
            .putString(PIN_SALT, record.saltHex)
            .putString(PIN_HASH, record.hashHex)
            .apply()
    }

    fun verify(pin: String): Boolean {
        if (!PinPolicy.isValid(pin)) return false
        val salt = preferences.getString(PIN_SALT, null) ?: return false
        val hash = preferences.getString(PIN_HASH, null) ?: return false
        return codec.verify(pin, PinRecord(salt, hash))
    }

    private companion object {
        const val PIN_SALT = "parent_pin_salt"
        const val PIN_HASH = "parent_pin_hash"
    }
}

object PinPolicy {
    fun isValid(pin: String): Boolean = pin.length == 4 && pin.all(Char::isDigit)

    fun validateSetup(pin: String, confirmation: String): PinSetupError? = when {
        !isValid(pin) -> PinSetupError.INVALID
        pin != confirmation -> PinSetupError.MISMATCH
        else -> null
    }
}

enum class PinSetupError { INVALID, MISMATCH }

class PinAttemptLimiter(
    private val clockMillis: () -> Long,
    private val maxFailures: Int = 5,
    private val lockoutMillis: Long = 30_000,
) {
    private var failures = 0
    private var lockedUntil = 0L

    fun remainingLockoutMillis(): Long = (lockedUntil - clockMillis()).coerceAtLeast(0)

    fun recordFailure() {
        failures += 1
        if (failures >= maxFailures) {
            failures = 0
            lockedUntil = clockMillis() + lockoutMillis
        }
    }

    fun recordSuccess() {
        failures = 0
        lockedUntil = 0
    }
}

object MenuHoldPolicy {
    const val HOLD_MILLIS = 3_000L

    fun isSatisfied(downTime: Long, currentTime: Long): Boolean =
        currentTime - downTime >= HOLD_MILLIS
}

enum class ParentAccessState { LOCKED, PIN_ENTRY, PARENT_MENU, SETTINGS }

class ParentAccessController {
    var state: ParentAccessState = ParentAccessState.LOCKED
        private set

    val isUnlocked: Boolean
        get() = state == ParentAccessState.PARENT_MENU || state == ParentAccessState.SETTINGS

    fun requestPin() {
        if (state == ParentAccessState.LOCKED) state = ParentAccessState.PIN_ENTRY
    }

    fun acceptPin() {
        if (state == ParentAccessState.PIN_ENTRY) state = ParentAccessState.PARENT_MENU
    }

    fun openSettings() {
        if (state == ParentAccessState.PARENT_MENU) state = ParentAccessState.SETTINGS
    }

    fun back() {
        state = when (state) {
            ParentAccessState.PIN_ENTRY, ParentAccessState.PARENT_MENU -> ParentAccessState.LOCKED
            ParentAccessState.SETTINGS -> ParentAccessState.PARENT_MENU
            ParentAccessState.LOCKED -> ParentAccessState.LOCKED
        }
    }

    fun relock() {
        state = ParentAccessState.LOCKED
    }
}
