package com.nostalgiabox.tv

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.Shader
import android.util.AttributeSet
import android.view.View
import kotlin.math.hypot
import kotlin.random.Random
import java.util.Locale

class RetroScreenOverlay @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {
    private val scanlinePaint = Paint().apply { color = Color.argb(22, 0, 0, 0) }
    private val vignettePaint = Paint()
    private val scanlineSpacing = resources.displayMetrics.density * 4f
    private val scanlineHeight = resources.displayMetrics.density

    init {
        isClickable = false
        isFocusable = false
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO
        setLayerType(LAYER_TYPE_HARDWARE, null)
    }

    override fun onSizeChanged(width: Int, height: Int, oldWidth: Int, oldHeight: Int) {
        super.onSizeChanged(width, height, oldWidth, oldHeight)
        val radius = hypot(width.toDouble(), height.toDouble()).toFloat() / 2f
        vignettePaint.shader = RadialGradient(
            width / 2f,
            height / 2f,
            radius,
            intArrayOf(Color.TRANSPARENT, Color.TRANSPARENT, Color.argb(105, 0, 0, 0)),
            floatArrayOf(0f, 0.64f, 1f),
            Shader.TileMode.CLAMP,
        )
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        var y = 0f
        while (y < height) {
            canvas.drawRect(0f, y, width.toFloat(), y + scanlineHeight, scanlinePaint)
            y += scanlineSpacing
        }
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), vignettePaint)
    }
}

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
