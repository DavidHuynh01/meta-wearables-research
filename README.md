# Meta Wearables Research

An Android app built on Meta's Wearables Device Access Toolkit (DAT SDK),
starting from the CameraAccess sample. It connects to Ray-Ban Meta Gen 2 glasses,
shows the live camera feed on the phone, lets you pick video quality and frame
rate, and logs per-session research metrics to CSV. It can also run against the
SDK's mock device, so it works without physical glasses.

## Requirements

- Android Studio Narwhal (2025.1.1) or newer
- JDK 17 or newer
- Android SDK 36 or newer
- A physical Android device (Android 12 or newer)

## Setup

### 1. GitHub token (needed to download the SDK)

The DAT SDK is distributed through GitHub Packages, so Gradle needs a GitHub
personal access token (classic) with the `read:packages` scope.

1. Create the token at GitHub: Settings, Developer settings, Personal access
   tokens (classic), with `read:packages` checked.
2. Add it to `local.properties` in the project root:
   ```
   github_token=YOUR_TOKEN_HERE
   ```

### 2. Developer Mode credentials

For running in Developer Mode, the credentials in `app/build.gradle.kts` are set
to `"0"`:
```
manifestPlaceholders["mwdat_application_id"] = "0"
manifestPlaceholders["mwdat_client_token"] = "0"
```

## Build and run

1. Open the project in Android Studio.
2. File, Sync Project with Gradle Files.
3. Select your device and click Run.

## Using the mock device (no glasses needed)

The app can run against the SDK's MockDeviceKit, which simulates the glasses with
the phone camera:

1. Launch the app.
2. Tap the floating bug icon on the right edge.
3. Enable MockDeviceKit, then Pair Ray-Ban Meta.
4. On the device card, turn on Power, Donned, and Unfolded.
5. Set the Camera source to Front camera or Back Camera
6. Close the sheet.
7. On the setup screen, pick Quality and FPS, then tap Start streaming.

## Metrics output

Every session writes two CSV files to the phone's public
`Download/MetaGlassesResearch` folder. See the `data/` folder for an example session.

### frames_&lt;time&gt;.csv (one row per video frame)

| column | meaning |
|---|---|
| frame_index | frame counter: 0, 1, 2, ... |
| timestamp_ms | milliseconds since the session started |
| width, height | the frame's resolution |
| gap_ms | milliseconds since the previous frame (inter-frame jitter) |


### events_&lt;time&gt;.csv (one row per event)

| column | meaning |
|---|---|
| timestamp_ms | milliseconds since the session started |
| type | `session_start`, `stream_state`, `resolution`, `error`, `session_end` |
| detail | the value: state name, resolution, error, or the end summary |

The `session_end` row includes the total duration, frame count, and average FPS.
A `resolution` event is written whenever the frame size changes, which is how the
SDK's quality adaptation is captured.

### Getting the CSVs off the phone

- USB: open the phone in File Explorer, go to Internal storage, Download,
  MetaGlassesResearch, and copy the files.

## Known limitations

On the mock device on some phones, the back camera and High quality can crash
Android's software VP8 encoder. Low and Medium with the front camera are stable.
This is a software codec limit.

If the app crashes a few seconds into streaming while running from Android
Studio, turn off Device Mirroring (Settings, Tools, Device Mirroring). The
mirror runs its own video encoder that competes with the stream's encoder and
can crash the shared codec. With mirroring off it is stable.
