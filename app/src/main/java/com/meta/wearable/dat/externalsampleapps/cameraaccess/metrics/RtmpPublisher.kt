package com.meta.wearable.dat.externalsampleapps.cameraaccess.metrics

import android.util.Log
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlin.random.Random

// Publishes the encoded frames to a media server over RTMP, which is what gives
// the Server stage of the metrics sheet anything to report. Hand-rolled rather
// than pulled in as a dependency: the protocol was verified against MediaMTX
// first, and the send queue here is itself one of the metrics.
//
// Everything happens on one writer thread. sendVideo() only enqueues, so the
// SDK's frame callback is never blocked by the network.
class RtmpPublisher(
    private val host: String,
    private val port: Int,
    private val app: String,
    private val streamName: String,
    private val onEvent: (type: String, detail: String) -> Unit,
) {

  companion object {
    private const val TAG = "CameraAccess:Rtmp"
    private const val CHUNK_SIZE = 4096
    // Bounded on purpose. This queue IS send_queue_bytes: when the uplink narrows
    // it grows, which is the signal we are trying to measure. The cap stops a dead
    // link turning into an OOM, and drops are counted rather than hidden.
    private const val MAX_QUEUED_FRAMES = 60
    private const val CONNECT_TIMEOUT_MS = 5000
  }

  private class Pending(val body: ByteArray, val timestampMs: Int)

  private val queue = ArrayBlockingQueue<Pending>(MAX_QUEUED_FRAMES)
  private val queuedBytes = AtomicInteger(0)
  private val running = AtomicBoolean(false)
  private val connected = AtomicBoolean(false)
  private var thread: Thread? = null
  private var socket: Socket? = null
  private var out: OutputStream? = null

  private var droppedFrames = 0
  private var sentFrames = 0
  private var sentBytes = 0L
  private var startNanos = 0L
  private var configSent = false

  /** Bytes accepted from the encoder but not yet written to the socket. */
  fun pendingBytes(): Int = queuedBytes.get()

  fun isConnected(): Boolean = connected.get()

  fun start() {
    if (running.getAndSet(true)) return
    startNanos = System.nanoTime()
    thread = Thread({ run() }, "RtmpPublisher").also { it.start() }
  }

  fun stop() {
    if (!running.getAndSet(false)) return
    thread?.interrupt()
    thread = null
    onEvent(
        "rtmp_summary",
        "sent_frames=$sentFrames;sent_bytes=$sentBytes;dropped=$droppedFrames")
    closeQuietly()
  }

  /**
   * Hand the codec's SPS/PPS over. MediaCodec emits these as an Annex-B buffer
   * flagged BUFFER_FLAG_CODEC_CONFIG; RTMP wants them repacked into an
   * AVCDecoderConfigurationRecord, which is a different layout of the same bytes.
   */
  fun setCodecConfig(buffer: ByteBuffer) {
    val nalus = splitAnnexB(toByteArray(buffer))
    val sps = nalus.firstOrNull { it.isNotEmpty() && (it[0].toInt() and 0x1F) == 7 }
    val pps = nalus.firstOrNull { it.isNotEmpty() && (it[0].toInt() and 0x1F) == 8 }
    if (sps == null || pps == null || sps.size < 4) {
      Log.w(TAG, "codec config without both SPS and PPS, ignoring")
      return
    }
    val cfg = ByteArray(11 + sps.size + pps.size)
    var i = 0
    cfg[i++] = 1                       // configurationVersion
    cfg[i++] = sps[1]                  // AVCProfileIndication
    cfg[i++] = sps[2]                  // profile_compatibility
    cfg[i++] = sps[3]                  // AVCLevelIndication
    cfg[i++] = 0xFF.toByte()           // 6 bits reserved + lengthSizeMinusOne = 3
    cfg[i++] = 0xE1.toByte()           // 3 bits reserved + numOfSPS = 1
    cfg[i++] = (sps.size shr 8).toByte()
    cfg[i++] = sps.size.toByte()
    System.arraycopy(sps, 0, cfg, i, sps.size); i += sps.size
    cfg[i++] = 1                       // numOfPPS
    cfg[i++] = (pps.size shr 8).toByte()
    cfg[i++] = pps.size.toByte()
    System.arraycopy(pps, 0, cfg, i, pps.size)

    val body = ByteArray(5 + cfg.size)
    body[0] = 0x17                     // keyframe + AVC
    body[1] = 0x00                     // AVCPacketType = sequence header
    System.arraycopy(cfg, 0, body, 5, cfg.size)
    enqueue(Pending(body, 0))
    configSent = true
  }

  /** Enqueue one encoded frame. Never blocks. */
  fun sendVideo(buffer: ByteBuffer, keyframe: Boolean) {
    if (!running.get() || !configSent) return
    val avcc = annexBToAvcc(toByteArray(buffer)) ?: return
    val body = ByteArray(5 + avcc.size)
    body[0] = if (keyframe) 0x17 else 0x27
    body[1] = 0x01                     // AVCPacketType = NALU
    // composition time offset stays 0: no B-frames in this encoder config
    System.arraycopy(avcc, 0, body, 5, avcc.size)
    val ts = ((System.nanoTime() - startNanos) / 1_000_000L).toInt()
    enqueue(Pending(body, ts))
  }

  private fun enqueue(p: Pending) {
    if (!queue.offer(p)) {
      // full means the socket is not draining as fast as the encoder fills it,
      // which is exactly the condition send_queue_bytes exists to record
      droppedFrames++
      return
    }
    queuedBytes.addAndGet(p.body.size)
  }

  // ------------------------------------------------------------------ writer

  private fun run() {
    try {
      openConnection()
      connected.set(true)
      onEvent("rtmp_connected", "rtmp://$host:$port/$app/$streamName")
      while (running.get()) {
        val p = queue.poll(500, TimeUnit.MILLISECONDS) ?: continue
        queuedBytes.addAndGet(-p.body.size)
        sendMessage(6, 9, 1, p.body, p.timestampMs)
        sentFrames++
        sentBytes += p.body.size
      }
    } catch (e: InterruptedException) {
      // stop() interrupts us; nothing to report
    } catch (e: Exception) {
      Log.e(TAG, "publisher failed", e)
      onEvent("rtmp_error", e.toString())
    } finally {
      connected.set(false)
      closeQuietly()
    }
  }

  private fun openConnection() {
    val s = Socket()
    s.tcpNoDelay = true
    s.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
    s.soTimeout = CONNECT_TIMEOUT_MS
    socket = s
    val o = BufferedOutputStream(s.getOutputStream(), 64 * 1024)
    out = o
    val input = DataInputStream(s.getInputStream())

    // handshake: C0 + C1, read S0 + S1 + S2, echo S1 back as C2
    val c1 = ByteArray(1536)
    Random.nextBytes(c1)
    java.util.Arrays.fill(c1, 0, 8, 0)
    o.write(0x03)
    o.write(c1)
    o.flush()
    if (input.readUnsignedByte() != 0x03) throw IllegalStateException("bad S0")
    val s1 = ByteArray(1536)
    input.readFully(s1)
    input.readFully(ByteArray(1536))
    o.write(s1)
    o.flush()

    sendMessage(2, 1, 0, be32(CHUNK_SIZE), 0)
    val tcUrl = "rtmp://$host:$port/$app"
    sendMessage(3, 20, 0,
        amfString("connect") + amfNumber(1.0) + amfObject(
            listOf("app" to amfString(app),
                   "type" to amfString("nonprivate"),
                   "flashVer" to amfString("FMLE/3.0 (compatible; MetaGlassesResearch)"),
                   "tcUrl" to amfString(tcUrl))), 0)
    drain(input, 900)
    sendMessage(3, 20, 0, amfString("createStream") + amfNumber(2.0) + byteArrayOf(0x05), 0)
    drain(input, 900)
    // servers hand out stream id 1 for the first createStream; the reply is read
    // only to keep the socket drained
    sendMessage(4, 20, 1,
        amfString("publish") + amfNumber(3.0) + byteArrayOf(0x05) +
            amfString(streamName) + amfString("live"), 0)
    drain(input, 900)
  }

  /** Read and discard whatever the server sent, so its window never fills. */
  private fun drain(input: DataInputStream, ms: Int) {
    val sock = socket ?: return
    val old = sock.soTimeout
    sock.soTimeout = ms
    val buf = ByteArray(8192)
    try {
      while (true) {
        if (input.read(buf) <= 0) break
      }
    } catch (e: Exception) {
      // a read timeout here is the expected way out
    } finally {
      sock.soTimeout = old
    }
  }

  private fun sendMessage(csid: Int, type: Int, streamId: Int, payload: ByteArray, timestamp: Int) {
    val o = out ?: return
    val header = ByteArray(12)
    header[0] = (csid and 0x3F).toByte()               // fmt 0
    header[1] = (timestamp shr 16).toByte()
    header[2] = (timestamp shr 8).toByte()
    header[3] = timestamp.toByte()
    header[4] = (payload.size shr 16).toByte()
    header[5] = (payload.size shr 8).toByte()
    header[6] = payload.size.toByte()
    header[7] = type.toByte()
    // message stream id is the one little-endian field in the header
    header[8] = streamId.toByte()
    header[9] = (streamId shr 8).toByte()
    header[10] = (streamId shr 16).toByte()
    header[11] = (streamId shr 24).toByte()
    o.write(header)
    var off = 0
    while (off < payload.size) {
      val n = minOf(CHUNK_SIZE, payload.size - off)
      o.write(payload, off, n)
      off += n
      if (off < payload.size) o.write(0xC0 or (csid and 0x3F))
    }
    o.flush()
  }

  private fun closeQuietly() {
    try { out?.flush() } catch (e: Exception) {}
    try { socket?.close() } catch (e: Exception) {}
    out = null
    socket = null
    queue.clear()
    queuedBytes.set(0)
  }

  // -------------------------------------------------------------- encoding

  private fun toByteArray(b: ByteBuffer): ByteArray {
    val dup = b.duplicate()
    val a = ByteArray(dup.remaining())
    dup.get(a)
    return a
  }

  /** Annex-B start codes to 4-byte length prefixes, which is what FLV carries. */
  private fun annexBToAvcc(data: ByteArray): ByteArray? {
    val nalus = splitAnnexB(data)
    if (nalus.isEmpty()) return null
    var total = 0
    for (n in nalus) total += 4 + n.size
    val outArr = ByteArray(total)
    var i = 0
    for (n in nalus) {
      outArr[i++] = (n.size shr 24).toByte()
      outArr[i++] = (n.size shr 16).toByte()
      outArr[i++] = (n.size shr 8).toByte()
      outArr[i++] = n.size.toByte()
      System.arraycopy(n, 0, outArr, i, n.size)
      i += n.size
    }
    return outArr
  }

  private fun splitAnnexB(data: ByteArray): List<ByteArray> {
    val starts = ArrayList<Pair<Int, Int>>()
    var i = 0
    while (i < data.size - 3) {
      if (data[i] == 0.toByte() && data[i + 1] == 0.toByte()) {
        if (data[i + 2] == 1.toByte()) {
          starts.add(i to 3); i += 3; continue
        }
        if (data[i + 2] == 0.toByte() && data[i + 3] == 1.toByte()) {
          starts.add(i to 4); i += 4; continue
        }
      }
      i++
    }
    val out = ArrayList<ByteArray>(starts.size)
    for (k in starts.indices) {
      val (pos, sz) = starts[k]
      val end = if (k + 1 < starts.size) starts[k + 1].first else data.size
      if (end > pos + sz) out.add(data.copyOfRange(pos + sz, end))
    }
    return out
  }

  private fun be32(v: Int) =
      byteArrayOf((v shr 24).toByte(), (v shr 16).toByte(), (v shr 8).toByte(), v.toByte())

  private fun amfString(s: String): ByteArray {
    val b = s.toByteArray()
    return byteArrayOf(0x02, (b.size shr 8).toByte(), b.size.toByte()) + b
  }

  private fun amfNumber(d: Double): ByteArray {
    val bits = java.lang.Double.doubleToLongBits(d)
    val out = ByteArray(9)
    out[0] = 0x00
    for (k in 0 until 8) out[1 + k] = (bits shr (56 - 8 * k)).toByte()
    return out
  }

  private fun amfObject(pairs: List<Pair<String, ByteArray>>): ByteArray {
    var out = byteArrayOf(0x03)
    for ((k, v) in pairs) {
      val kb = k.toByteArray()
      out += byteArrayOf((kb.size shr 8).toByte(), kb.size.toByte()) + kb + v
    }
    return out + byteArrayOf(0x00, 0x00, 0x09)
  }
}
