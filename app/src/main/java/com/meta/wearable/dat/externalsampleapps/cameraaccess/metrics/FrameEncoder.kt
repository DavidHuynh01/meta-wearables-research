package com.meta.wearable.dat.externalsampleapps.cameraaccess.metrics

import android.media.Image
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import java.nio.ByteBuffer

// Re-encodes the decoded frames the toolkit hands us so the encoder-stage metrics
// exist at all. These numbers describe THIS encoder, not the sealed one on the
// glasses. Nothing is written or sent; the compressed bytes are measured and dropped.
class FrameEncoder(
    private val targetBitrate: Int = DEFAULT_BITRATE,
    private val frameRate: Int = 30,
    private val onEncodedFrame: (sizeBytes: Int, ptsUs: Long, keyframe: Boolean, sendQueueBytes: Int) -> Unit,
    private val onEvent: (type: String, detail: String) -> Unit,
) {

  companion object {
    private const val TAG = "CameraAccess:FrameEncoder"
    private const val MIME = MediaFormat.MIMETYPE_VIDEO_AVC
    const val DEFAULT_BITRATE = 2_000_000
    private const val KEYFRAME_INTERVAL_S = 1
    private const val MAX_PENDING = 2
  }

  private class PendingFrame(
      val data: ByteArray,
      val size: Int,
      val width: Int,
      val height: Int,
      val ptsUs: Long,
  )

  private var codec: MediaCodec? = null
  private var thread: HandlerThread? = null
  private var handler: Handler? = null

  private var codecWidth = 0
  private var codecHeight = 0
  private var released = false

  private val pending = ArrayDeque<PendingFrame>()
  private val freeInputs = ArrayDeque<Int>()
  // a 504x896 I420 frame is ~677 KB, so allocating per frame would make GC pauses
  // land in the timings this class exists to measure
  private val bufferPool = ArrayDeque<ByteArray>()

  private var encodedCount = 0
  private var droppedInput = 0

  // set when the encoded frames are also being pushed to a media server; null
  // means measure only, which is the mode that needs no network at all
  var publisher: RtmpPublisher? = null

  @Synchronized
  fun encode(buffer: ByteBuffer, width: Int, height: Int, ptsUs: Long) {
    if (released || width <= 0 || height <= 0) return

    // MediaCodec cannot resize in place, so an adaptation step means a new encoder
    if (codec == null || width != codecWidth || height != codecHeight) {
      if (codec != null) {
        onEvent("encoder_restart", "from=${codecWidth}x${codecHeight};to=${width}x${height}")
      }
      if (!startCodec(width, height)) return
    }

    val needed = width * height * 3 / 2
    if (buffer.remaining() < needed) {
      droppedInput++
      return
    }

    if (pending.size >= MAX_PENDING) {
      recycle(pending.removeFirst().data)
      droppedInput++
    }

    val dst = obtain(needed)
    buffer.duplicate().get(dst, 0, needed)
    pending.addLast(PendingFrame(dst, needed, width, height, ptsUs))
    drain()
  }

  @Synchronized
  fun release() {
    if (released) return
    released = true
    if (encodedCount > 0 || droppedInput > 0) {
      onEvent(
          "encoder_summary",
          "encoded=$encodedCount;dropped_input=$droppedInput;target_bitrate=$targetBitrate")
    }
    stopCodec()
    thread?.quitSafely()
    thread = null
    handler = null
    pending.clear()
    freeInputs.clear()
    bufferPool.clear()
  }

  @Synchronized fun pendingBytes(): Int = pending.sumOf { it.size }

  // ---------------------------------------------------------------- internals

  private fun startCodec(width: Int, height: Int): Boolean {
    stopCodec()
    return try {
      if (thread == null) {
        thread = HandlerThread("FrameEncoder").also { it.start() }
        handler = Handler(thread!!.looper)
      }
      val format =
          MediaFormat.createVideoFormat(MIME, width, height).apply {
            // Flexible lets us fill through getInputImage, which reports the codec's
            // real strides instead of us guessing planar vs semi-planar per chipset
            setInteger(
                MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible)
            setInteger(MediaFormat.KEY_BIT_RATE, targetBitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, frameRate)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, KEYFRAME_INTERVAL_S)
          }
      val c = MediaCodec.createEncoderByType(MIME)
      c.setCallback(callback, handler)
      c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
      c.start()
      codec = c
      codecWidth = width
      codecHeight = height
      onEvent("encoder_start", "${width}x${height};bitrate=$targetBitrate;fps=$frameRate")
      true
    } catch (e: Exception) {
      // a missing or busy encoder must not take the stream down with it
      Log.e(TAG, "Encoder start failed", e)
      onEvent("encoder_error", e.toString())
      codec = null
      false
    }
  }

  private fun stopCodec() {
    val c = codec ?: return
    codec = null
    codecWidth = 0
    codecHeight = 0
    freeInputs.clear()
    try {
      c.stop()
    } catch (e: Exception) {
      Log.w(TAG, "Encoder stop failed", e)
    }
    try {
      c.release()
    } catch (e: Exception) {
      Log.w(TAG, "Encoder release failed", e)
    }
  }

  private val callback =
      object : MediaCodec.Callback() {
        override fun onInputBufferAvailable(c: MediaCodec, index: Int) {
          synchronized(this@FrameEncoder) {
            if (c !== codec) return
            freeInputs.addLast(index)
            drain()
          }
        }

        override fun onOutputBufferAvailable(
            c: MediaCodec,
            index: Int,
            info: MediaCodec.BufferInfo,
        ) {
          synchronized(this@FrameEncoder) {
            if (c !== codec) return
            // the codec-config buffer is SPS/PPS, not a picture
            val isConfig = info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0
            val keyframe = info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
            if (!isConfig && info.size > 0) {
              encodedCount++
              onEncodedFrame(
                  info.size, info.presentationTimeUs, keyframe, publisher?.pendingBytes() ?: 0)
            }
            // read the bytes out before releasing: the buffer belongs to the
            // codec again the moment releaseOutputBuffer returns
            val pub = publisher
            if (pub != null && info.size > 0) {
              try {
                c.getOutputBuffer(index)?.let { buf ->
                  buf.position(info.offset)
                  buf.limit(info.offset + info.size)
                  if (isConfig) pub.setCodecConfig(buf) else pub.sendVideo(buf, keyframe)
                }
              } catch (e: Exception) {
                Log.w(TAG, "Publish failed", e)
              }
            }
            try {
              c.releaseOutputBuffer(index, false)
            } catch (e: Exception) {
              Log.w(TAG, "Release output failed", e)
            }
          }
        }

        override fun onError(c: MediaCodec, e: MediaCodec.CodecException) {
          Log.e(TAG, "Encoder error", e)
          synchronized(this@FrameEncoder) {
            if (c !== codec) return
            onEvent("encoder_error", e.diagnosticInfo ?: e.toString())
            // tearing the codec down inside its own callback can deadlock
            handler?.post { synchronized(this@FrameEncoder) { if (c === codec) stopCodec() } }
          }
        }

        override fun onOutputFormatChanged(c: MediaCodec, format: MediaFormat) {
          // codecs are free to override what we asked for, and they do: a 2.0 Mbps
          // request commonly comes back as 2.4. target_bitrate has to report the
          // granted value, since that is what the rate controller aims at
          val granted = intOrNull(format, MediaFormat.KEY_BIT_RATE)
          val mode = intOrNull(format, MediaFormat.KEY_BITRATE_MODE)
          onEvent(
              "encoder_format",
              "requested=$targetBitrate;granted=${granted ?: "?"};mode=${modeName(mode)}")
          Log.d(TAG, "Encoder format: $format")
        }
      }

  private fun drain() {
    val c = codec ?: return
    while (pending.isNotEmpty() && freeInputs.isNotEmpty()) {
      val index = freeInputs.removeFirst()
      val frame = pending.removeFirst()
      try {
        val image = c.getInputImage(index)
        if (image == null) {
          c.queueInputBuffer(index, 0, 0, frame.ptsUs, 0)
        } else {
          fillImage(image, frame.data, frame.width, frame.height)
          // filled through an Image, the valid region is the whole buffer including
          // stride padding, not the planar w*h*3/2 count
          val capacity = c.getInputBuffer(index)?.capacity() ?: frame.size
          c.queueInputBuffer(index, 0, capacity, frame.ptsUs, 0)
        }
      } catch (e: Exception) {
        Log.w(TAG, "Queue input failed", e)
        droppedInput++
      }
      recycle(frame.data)
    }
  }

  private fun intOrNull(format: MediaFormat, key: String): Int? =
      if (format.containsKey(key)) format.getInteger(key) else null

  // whether the encoder is holding a rate or free to vary decides how to read
  // encoded_bitrate against target_bitrate at all
  private fun modeName(mode: Int?) =
      when (mode) {
        MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CQ -> "CQ"
        MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR -> "VBR"
        MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR -> "CBR"
        null -> "?"
        else -> mode.toString()
      }

  private fun obtain(size: Int): ByteArray {
    while (bufferPool.isNotEmpty()) {
      val b = bufferPool.removeFirst()
      if (b.size >= size) return b
    }
    return ByteArray(size)
  }

  private fun recycle(b: ByteArray) {
    if (bufferPool.size < MAX_PENDING + 2) bufferPool.addLast(b)
  }

  /** Copy planar I420 into whatever layout the codec actually wants. */
  private fun fillImage(image: Image, src: ByteArray, width: Int, height: Int) {
    val cw = width / 2
    val ch = height / 2
    val ySize = width * height
    copyPlane(image.planes[0], src, 0, width, height, width)
    copyPlane(image.planes[1], src, ySize, cw, ch, cw)
    copyPlane(image.planes[2], src, ySize + cw * ch, cw, ch, cw)
  }

  private fun copyPlane(
      plane: Image.Plane,
      src: ByteArray,
      srcOffset: Int,
      width: Int,
      height: Int,
      srcRowStride: Int,
  ) {
    val buf = plane.buffer
    val rowStride = plane.rowStride
    val pixelStride = plane.pixelStride
    if (pixelStride == 1) {
      for (row in 0 until height) {
        val dstPos = row * rowStride
        val srcPos = srcOffset + row * srcRowStride
        if (dstPos + width > buf.capacity() || srcPos + width > src.size) break
        buf.position(dstPos)
        buf.put(src, srcPos, width)
      }
    } else {
      // semi-planar: the chroma planes interleave, so a bulk copy would shred them
      for (row in 0 until height) {
        var dstPos = row * rowStride
        var srcPos = srcOffset + row * srcRowStride
        for (col in 0 until width) {
          if (dstPos >= buf.capacity() || srcPos >= src.size) break
          buf.put(dstPos, src[srcPos])
          dstPos += pixelStride
          srcPos++
        }
      }
    }
    buf.rewind()
  }
}
