package com.meta.wearable.dat.externalsampleapps.cameraaccess.metrics

import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.BatteryManager
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
// frames_<time>.csv  one row per video frame, for effective FPS and jitter
// events_<time>.csv  one row per event: session start/end, state changes, errors
// Methods are synchronized because frames and events arrive on different threads.
class SessionLogger(private val context: Context) {

  companion object {
    private const val TAG = "CameraAccess:SessionLogger"
    private val FOLDER = Environment.DIRECTORY_DOWNLOADS + "/MetaGlassesResearch"
  }

  private var framesUri: Uri? = null
  private var eventsUri: Uri? = null
  private var displayUri: Uri? = null
  private var framesWriter: BufferedWriter? = null
  private var eventsWriter: BufferedWriter? = null
  private var displayWriter: BufferedWriter? = null
  private var startNanos = 0L
  private var lastFrameNanos = 0L
  private var frameCount = 0
  private var lastDisplayNanos = 0L
  private var displayCount = 0

  @Synchronized
  fun start(
      quality: String,
      frameRate: Int,
      trialId: String,
      platform: String,
      phonePosition: String,
      motionCondition: String,
      networkLimit: String,
  ) {
    closeWriters()
    val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
    framesUri = createCsv("frames_$stamp.csv")
    eventsUri = createCsv("events_$stamp.csv")
    displayUri = createCsv("display_$stamp.csv")
    framesWriter = openWriter(framesUri)
    eventsWriter = openWriter(eventsUri)
    displayWriter = openWriter(displayUri)
    framesWriter?.appendLine("frame_index,timestamp_ms,width,height,gap_ms,pts_us")
    eventsWriter?.appendLine("timestamp_ms,type,detail")
    displayWriter?.appendLine("display_index,timestamp_ms,gap_ms,pts_us")
    // two clocks on purpose: nanoTime is monotonic so gaps and durations can't be
    // corrupted by clock adjustments, epoch is wall time so these logs can be lined
    // up with router captures and viewer recordings on one shared timeline
    startNanos = System.nanoTime()
    val epochStartMs = System.currentTimeMillis()
    lastFrameNanos = 0L
    frameCount = 0
    lastDisplayNanos = 0L
    displayCount = 0
    appendTrialRow(
        stamp, trialId, platform, quality, frameRate, phonePosition, motionCondition, networkLimit, epochStartMs)
    logEvent("session_start", "quality=$quality;fps=$frameRate;epoch_start_ms=$epochStartMs")
    logEvent("battery", "percent=${batteryPercent()}")
    Log.d(TAG, "Logging session to $FOLDER")
  }

  // all sessions share ONE trials.csv, each run appends a row. session_stamp is
  // the join key to that run's frames_/events_ files
  private fun appendTrialRow(
      stamp: String,
      trialId: String,
      platform: String,
      quality: String,
      frameRate: Int,
      phonePosition: String,
      motionCondition: String,
      networkLimit: String,
      epochStartMs: Long,
  ) {
    // the mock's video never crosses a radio, so label its transport honestly
    val transport = if (platform == "mock") "mock_loopback" else "bluetooth_classic"
    val header =
        "trial_id,session_stamp,device,local_transport,quality,frame_rate,phone_position,motion_condition,network_limit,epoch_start_ms"
    val row =
        "$trialId,$stamp,$platform,$transport,$quality,$frameRate,$phonePosition,$motionCondition,$networkLimit,$epochStartMs"

    val existing = findCsv("trials.csv")
    if (existing != null) {
      try {
        // "wa" = append; works as long as this install created the file
        context.contentResolver.openOutputStream(existing, "wa")?.let { stream ->
          BufferedWriter(OutputStreamWriter(stream)).use { it.appendLine(row) }
          return
        }
      } catch (e: Exception) {
        // after a clean reinstall the app loses write access to its old file,
        // fall through and start a fresh one rather than losing the row
        Log.e(TAG, "Append to trials.csv failed, creating new file", e)
      }
    }
    val uri = createCsv("trials.csv")
    val writer = openWriter(uri) ?: return
    try {
      writer.appendLine(header)
      writer.appendLine(row)
      writer.flush()
      writer.close()
    } catch (e: Exception) {
      Log.e(TAG, "Failed to write trials.csv", e)
    }
    markComplete(uri)
  }

  private fun findCsv(name: String): Uri? {
    val projection = arrayOf(MediaStore.Downloads._ID)
    val selection =
        "${MediaStore.Downloads.DISPLAY_NAME}=? AND ${MediaStore.Downloads.RELATIVE_PATH}=?"
    // MediaStore stores the relative path with a trailing slash
    val args = arrayOf(name, "$FOLDER/")
    context.contentResolver
        .query(MediaStore.Downloads.EXTERNAL_CONTENT_URI, projection, selection, args, null)
        ?.use { cursor ->
          if (cursor.moveToFirst()) {
            return ContentUris.withAppendedId(
                MediaStore.Downloads.EXTERNAL_CONTENT_URI, cursor.getLong(0))
          }
        }
    return null
  }

  @Synchronized
  fun logFrame(width: Int, height: Int, ptsUs: Long) {
    val writer = framesWriter ?: return
    val now = System.nanoTime()
    val elapsedMs = (now - startNanos) / 1_000_000
    // the first frame has no previous frame to measure a gap against
    val gapMs = if (lastFrameNanos == 0L) "" else ((now - lastFrameNanos) / 1_000_000).toString()
    lastFrameNanos = now
    // pts is the sender's own timestamp; comparing its cadence against arrival
    // times is what separates delivery jitter from capture jitter
    writer.appendLine("$frameCount,$elapsedMs,$width,$height,$gapMs,$ptsUs")
    frameCount++
  }

  // one row per frame that actually reached the screen. Arrival minus display
  // for the same pts is the presentation buffer's real delay; display gaps over
  // 500 ms are her freeze definition
  @Synchronized
  fun logDisplayFrame(ptsUs: Long) {
    val writer = displayWriter ?: return
    val now = System.nanoTime()
    val elapsedMs = (now - startNanos) / 1_000_000
    val gapMs = if (lastDisplayNanos == 0L) "" else ((now - lastDisplayNanos) / 1_000_000).toString()
    lastDisplayNanos = now
    writer.appendLine("$displayCount,$elapsedMs,$gapMs,$ptsUs")
    displayCount++
  }

  @Synchronized
  fun logEvent(type: String, detail: String) {
    val writer = eventsWriter ?: return
    val elapsedMs = if (startNanos == 0L) 0L else (System.nanoTime() - startNanos) / 1_000_000
    writer.appendLine("$elapsedMs,$type,$detail")
  }

  // session clock for callers that need to compute durations between events
  @Synchronized
  fun elapsedMs(): Long = if (startNanos == 0L) 0L else (System.nanoTime() - startNanos) / 1_000_000

  @Synchronized
  fun stop() {
    // shutdown can come from several paths, only write session_end once
    if (framesWriter == null && eventsWriter == null) return
    val durationMs = if (startNanos == 0L) 0L else (System.nanoTime() - startNanos) / 1_000_000
    val avgFps = if (durationMs > 0) frameCount * 1000.0 / durationMs else 0.0
    logEvent("battery", "percent=${batteryPercent()}")
    logEvent(
        "session_end", "duration_ms=$durationMs;frames=$frameCount;avg_fps=${"%.1f".format(avgFps)}")
    Log.d(TAG, "Session logged: $frameCount frames, ${durationMs}ms, avg ${"%.1f".format(avgFps)} fps")
    closeWriters()
  }

  // phone battery, the glasses' own battery is not reachable through the toolkit
  private fun batteryPercent(): Int {
    val bm = context.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager ?: return -1
    return bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
  }

  private fun createCsv(name: String): Uri? {
    val values =
        ContentValues().apply {
          put(MediaStore.Downloads.DISPLAY_NAME, name)
          put(MediaStore.Downloads.MIME_TYPE, "text/csv")
          put(MediaStore.Downloads.RELATIVE_PATH, FOLDER)
          // pending keeps the file hidden until the session is finished
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
    flushClose(displayWriter)
    displayWriter = null
    markComplete(framesUri)
    framesUri = null
    markComplete(eventsUri)
    eventsUri = null
    markComplete(displayUri)
    displayUri = null
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
      // clearing pending is what makes the file show up in the Files app
      val values = ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }
      context.contentResolver.update(uri, values, null, null)
    } catch (e: Exception) {
      Log.e(TAG, "Failed to finalize file", e)
    }
  }
}
