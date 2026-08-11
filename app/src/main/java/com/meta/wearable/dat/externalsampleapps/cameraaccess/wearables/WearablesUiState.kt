/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

// WearablesUiState - DAT API State Management
//
// This data class aggregates DAT API state for the UI layer

package com.meta.wearable.dat.externalsampleapps.cameraaccess.wearables

import com.meta.wearable.dat.camera.types.VideoQuality
import com.meta.wearable.dat.core.types.DeviceIdentifier
import com.meta.wearable.dat.core.types.RegistrationState
import kotlinx.collections.immutable.ImmutableList
import kotlinx.collections.immutable.persistentListOf

data class WearablesUiState(
    val registrationState: RegistrationState = RegistrationState.UNAVAILABLE,
    val devices: ImmutableList<DeviceIdentifier> = persistentListOf(),
    val recentError: String? = null,
    val isStreaming: Boolean = false,
    val isDebugMenuVisible: Boolean = false,
    val isGettingStartedSheetVisible: Boolean = false,
    val isFirmwareUpdateRequired: Boolean = false,
    val isDatAppUpdateRequired: Boolean = false,
    val hasActiveDevice: Boolean = false,
    val canRegister: Boolean = false,
    // stream config picked on the setup screen, read when the stream starts
    val selectedQuality: VideoQuality = VideoQuality.MEDIUM,
    val selectedFrameRate: Int = 24,
    // trial metadata for the metrics CSVs, so every run records the conditions it ran under
    // trialId blank means auto-generate from platform + persisted counter (Gen2_0001, ...)
    val trialId: String = "",
    val platform: String = "mock",
    val phonePosition: String = "near",
    val motionCondition: String = "stationary",
    val networkLimit: String = "none",
    // which independent viewer produced these rows, for two-viewer attribution
    val viewerId: String = "A",
    // BASELINE / STRESS / RECOVERY, changed during a run so app sessions line up
    // with the phases the Gen 1 controller marks
    val trialPhase: String = "BASELINE",
) {
  val isRegistered: Boolean =
      registrationState == RegistrationState.REGISTERED ||
          registrationState == RegistrationState.UNREGISTERING

  val isRegistering: Boolean = registrationState == RegistrationState.REGISTERING

  val canStartRegistration: Boolean = canRegister && !isRegistering
}
