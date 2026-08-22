package com.nostalgiabox.tv

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View
import kotlin.random.Random
import java.util.Locale

class TuningStaticView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {
    private val noisePaint = Paint()
    private val random = Random(2001)

    init {
        visibility = GONE
        isClickable = false
        isFocusable = false
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO
    }

    fun flash() {
        animate().cancel()
        alpha = 0.22f
        visibility = VISIBLE
        invalidate()
        animate()
            .alpha(0f)
            .setDuration(300)
            .withEndAction { visibility = GONE }
            .start()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        canvas.drawColor(Color.argb(105, 255, 255, 255))
        repeat(180) {
            val shade = random.nextInt(256)
            val stripeHeight = random.nextInt(2, 12).toFloat()
            val top = random.nextInt(height.coerceAtLeast(1)).toFloat()
            noisePaint.color = Color.argb(random.nextInt(35, 150), shade, shade, shade)
            canvas.drawRect(0f, top, width.toFloat(), top + stripeHeight, noisePaint)
        }
    }
}

object RetroChannelText {
    fun number(channelNumber: Int): String = "CH ${channelNumber.toString().padStart(2, '0')}"

    fun name(channelName: String): String = channelName.uppercase(Locale.US)
}
