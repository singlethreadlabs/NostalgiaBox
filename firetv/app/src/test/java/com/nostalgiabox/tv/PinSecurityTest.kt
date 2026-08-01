package com.nostalgiabox.tv

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PinSecurityTest {
    @Test
    fun validatesPinCreationAndConfirmation() {
        assertNull(PinPolicy.validateSetup("4829", "4829"))
        assertEquals(PinSetupError.INVALID, PinPolicy.validateSetup("123", "123"))
        assertEquals(PinSetupError.INVALID, PinPolicy.validateSetup("12a4", "12a4"))
        assertEquals(PinSetupError.MISMATCH, PinPolicy.validateSetup("4829", "4820"))
    }

    @Test
    fun hashesPinsWithUniqueSaltsAndVerifiesWithoutPlaintext() {
        val codec = PinCodec()
        val first = codec.create("4829")
        val second = codec.create("4829")

        assertNotEquals(first.saltHex, second.saltHex)
        assertNotEquals(first.hashHex, second.hashHex)
        assertFalse(first.hashHex.contains("4829"))
        assertTrue(codec.verify("4829", first))
        assertFalse(codec.verify("4820", first))
        assertFalse(codec.verify("123", first))
        assertFalse(codec.verify("4829", first.copy(hashHex = "invalid")))
    }

    @Test
    fun rateLimitsFiveFailuresAndClearsAfterTimeoutOrSuccess() {
        var now = 1_000L
        val limiter = PinAttemptLimiter(clockMillis = { now })

        repeat(4) { limiter.recordFailure() }
        assertEquals(0, limiter.remainingLockoutMillis())
        limiter.recordFailure()
        assertEquals(30_000, limiter.remainingLockoutMillis())

        now += 30_000
        assertEquals(0, limiter.remainingLockoutMillis())
        limiter.recordFailure()
        limiter.recordSuccess()
        assertEquals(0, limiter.remainingLockoutMillis())
    }

    @Test
    fun requiresAFullThreeSecondMenuHold() {
        assertFalse(MenuHoldPolicy.isSatisfied(1_000, 3_999))
        assertTrue(MenuHoldPolicy.isSatisfied(1_000, 4_000))
    }

    @Test
    fun parentAccessTransitionsAlwaysReturnToLockedKidMode() {
        val access = ParentAccessController()
        assertEquals(ParentAccessState.LOCKED, access.state)

        access.requestPin()
        assertEquals(ParentAccessState.PIN_ENTRY, access.state)
        access.acceptPin()
        assertEquals(ParentAccessState.PARENT_MENU, access.state)
        assertTrue(access.isUnlocked)

        access.openSettings()
        assertEquals(ParentAccessState.SETTINGS, access.state)
        access.back()
        assertEquals(ParentAccessState.PARENT_MENU, access.state)
        access.relock()
        assertEquals(ParentAccessState.LOCKED, access.state)
        assertFalse(access.isUnlocked)
    }

    @Test
    fun backFromPinOrParentMenuNeverExitsKidMode() {
        val access = ParentAccessController()
        access.requestPin()
        access.back()
        assertEquals(ParentAccessState.LOCKED, access.state)

        access.requestPin()
        access.acceptPin()
        access.back()
        assertEquals(ParentAccessState.LOCKED, access.state)
    }
}
