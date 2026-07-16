package com.meta.wearable.dat.externalsampleapps.cameraaccess.metrics

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import android.util.Log
import java.io.BufferedWriter
import java.io.OutputStreamWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

// Writes per-session research metrics to two CSV files in the phone's public
// Downloads/MetaGlassesResearch folder, so they show up in the Files app and over USB:
//   frames_<time>.csv  one row per video frame, for effective FPS and jitter
//   events_<time>.csv  one row per event: session start/end, state changes, errors
class SessionLogger(private val context: Context) {

  companion object {
    private const val TAG = "CameraAccess:SessionLogger"
    private val FOLDER = Environment.DIRECTORY_DOWNLOADS + "/MetaGlassesResearch"
  }

  private var framesUri: Uri? = null
  private var eventsUri: Uri? = null
  private var framesWriter: BufferedWriter? = null
  private var eventsWriter: BufferedWriter? = null
  private var startNanos = 0L
  private var lastFrameNanos = 0L
  private var frameCount = 0

  @Synchronized
  fun start(quality: String, frameRate: Int) {
    closeWriters()
    val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
    framesUri = createCsv("frames_$stamp.csv")
    eventsUri = createCsv("events_$stamp.csv")
    framesWriter = openWriter(framesUri)
    eventsWriter = openWriter(eventsUri)
    framesWriter?.appendLine("frame_index,timestamp_ms,width,height,gap_ms")
    eventsWriter?.appendLine("timestamp_ms,type,detail")
    startNanos = System.nanoTime()
    lastFrameNanos = 0L
    frameCount = 0
    logEvent("session_start", "quality=$quality;fps=$frameRate")
    Log.d(TAG, "Logging session to $FOLDER")
  }

  @Synchronized
  fun logFrame(width: Int, height: Int) {
    val writer = framesWriter ?: return
    val now = System.nanoTime()
    val elapsedMs = (now - startNanos) / 1_000_000
    val gapMs = if (lastFrameNanos == 0L) "" else ((now - lastFrameNanos) / 1_000_000).toString()
    lastFrameNanos = now
    writer.appendLine("$frameCount,$elapsedMs,$width,$height,$gapMs")
    frameCount++
  }

  @Synchronized
  fun logEvent(type: String, detail: String) {
    val writer = eventsWriter ?: return
    val elapsedMs = if (startNanos == 0L) 0L else (System.nanoTime() - startNanos) / 1_000_000
    writer.appendLine("$elapsedMs,$type,$detail")
  }

  @Synchronized
  fun stop() {
    if (framesWriter == null && eventsWriter == null) return
    val durationMs = if (startNanos == 0L) 0L else (System.nanoTime() - startNanos) / 1_000_000
    val avgFps = if (durationMs > 0) frameCount * 1000.0 / durationMs else 0.0
    logEvent(
        "session_end", "duration_ms=$durationMs;frames=$frameCount;avg_fps=${"%.1f".format(avgFps)}")
    Log.d(TAG, "Session logged: $frameCount frames, ${durationMs}ms, avg ${"%.1f".format(avgFps)} fps")
    closeWriters()
  }

  private fun createCsv(name: String): Uri? {
    val values =
        ContentValues().apply {
          put(MediaStore.Downloads.DISPLAY_NAME, name)
          put(MediaStore.Downloads.MIME_TYPE, "text/csv")
          put(MediaStore.Downloads.RELATIVE_PATH, FOLDER)
          put(MediaStore.Downloads.IS_PENDING, 1)
        }
    return context.contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
  }

  private fun openWriter(uri: Uri?): BufferedWriter? {
    if (uri == null) return null
    val stream = context.contentResolver.openOutputStream(uri) ?: return null
    return BufferedWriter(OutputStreamWriter(stream))
  }

  private fun closeWriters() {
    flushClose(framesWriter)
    framesWriter = null
    flushClose(eventsWriter)
    eventsWriter = null
    markComplete(framesUri)
    framesUri = null
    markComplete(eventsUri)
    eventsUri = null
  }

  private fun flushClose(writer: BufferedWriter?) {
    try {
      writer?.flush()
      writer?.close()
    } catch (e: Exception) {
      Log.e(TAG, "Failed to close file", e)
    }
  }

  private fun markComplete(uri: Uri?) {
    if (uri == null) return
    try {
      val values = ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }
      context.contentResolver.update(uri, values, null, null)
    } catch (e: Exception) {
      Log.e(TAG, "Failed to finalize file", e)
    }
  }
}
