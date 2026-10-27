package com.synaptimesh.bci;

import android.app.Notification;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.media.AudioManager;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSession;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.view.KeyEvent;
import android.accessibilityservice.AccessibilityService;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;

import org.eclipse.paho.client.mqttv3.IMqttDeliveryToken;
import org.eclipse.paho.client.mqttv3.MqttCallbackExtended;
import org.eclipse.paho.client.mqttv3.MqttClient;
import org.eclipse.paho.client.mqttv3.MqttConnectOptions;
import org.eclipse.paho.client.mqttv3.MqttMessage;
import org.eclipse.paho.client.mqttv3.persist.MemoryPersistence;
import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class BciMediaService extends NotificationListenerService {

    private static final String TAG = "BciMediaService";
    private static final String JIOSAAVN_PACKAGE_BEATS = "com.jio.media.jiobeats";
    private static final String JIOSAAVN_PACKAGE_SAAVN = "com.jio.media.jiosaavn";
    public static final String DEFAULT_SEARCH_QUERY = "Inaam";

    public static final String PRIMARY_BROKER_URL = "tcp://52.21.249.6:1883";
    public static final String FALLBACK_BROKER_URL = "tcp://broker.emqx.io:1883";
    public static final String DEFAULT_BROKER_URL = PRIMARY_BROKER_URL;
    private String brokerUrl = DEFAULT_BROKER_URL;
    public static final String DISCOVERY_TOPIC = "aiml/devices/register";

    private String deviceUsername = "";

    public String getDeviceUsername() { return deviceUsername; }
    public String getCommandsTopic() { return "aiml/" + deviceUsername + "/commands"; }

    public String getAckTopic() { return "aiml/" + deviceUsername + "/ack"; }
    public String getStatusTopic() { return "aiml/" + deviceUsername + "/status"; }
    public String getMediaTopic() { return "aiml/" + deviceUsername + "/media"; }

    private static final long PUBLISH_THROTTLE_MS = 1000;
    private static final long NOTIFICATION_THROTTLE_MS = 2000;

    public static BciMediaService instance;
    private MqttClient mqttClient;
    private final ExecutorService mqttPublishExecutor = Executors.newSingleThreadExecutor();

    private void publishMqttAsync(final String topic, final MqttMessage message) {
        mqttPublishExecutor.execute(() -> {
            try {
                if (mqttClient != null && mqttClient.isConnected()) {
                    mqttClient.publish(topic, message);
                }
            } catch (Exception e) {
                Log.e(TAG, "Async MQTT publish failed for topic " + topic, e);
            }
        });
    }

    private MediaController activeMediaController;
    private MediaController.Callback mediaCallback;
    private MediaSessionManager.OnActiveSessionsChangedListener sessionsChangedListener;
    private Handler progressHandler = new Handler(Looper.getMainLooper());
    private Runnable progressRunnable;
    private long lastPublishTime = 0;
    private long lastNotificationPublishTime = 0;
    private boolean isRemoteActive = true;

    private String lastKnownTitle = "Unknown Track";
    private String lastKnownArtist = "Unknown Artist";
    private String lastKnownAlbumArt = "";
    private String lastKnownPackage = "";

    // Deduplication cache: tracks recent command_ids to prevent re-execution from multi-topic delivery or QoS retries
    private final Map<String, Long> processedCommandIds = Collections.synchronizedMap(
        new LinkedHashMap<String, Long>(64, 0.75f, true) {
            @Override
            protected boolean removeEldestEntry(Map.Entry<String, Long> eldest) {
                return size() > 50;
            }
        }
    );
    private long lastDeeplinkTime = 0;
    private String lastDeeplinkUrl = "";

    private boolean isCommandAlreadyProcessed(String commandId) {
        if (commandId == null || commandId.trim().isEmpty()) return false;
        synchronized (processedCommandIds) {
            if (processedCommandIds.containsKey(commandId)) {
                return true;
            }
            processedCommandIds.put(commandId, System.currentTimeMillis());
            return false;
        }
    }

    @Override
    public void onListenerConnected() {
        super.onListenerConnected();
        instance = this;
        loadDeviceUsername();
        setupMediaSessionListeners();

        android.content.SharedPreferences prefs = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE);
        boolean userDisconnected = prefs.getBoolean(MainActivity.KEY_USER_DISCONNECTED, false);
        if (!userDisconnected) {
            this.isRemoteActive = true;
            connectToMqttBroker();
        } else {
            this.isRemoteActive = false;
            Log.d(TAG, "onListenerConnected: User previously disconnected. Staying in standby.");
        }
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        instance = null;

        stopProgressUpdates();
        cleanupMediaSessionListeners();

        if (mqttClient != null) {
            new Thread(() -> {
                try {
                    if (mqttClient != null) {
                        mqttClient.disconnect();
                        mqttClient.close();
                    }
                    Log.d(TAG, "MQTT Client cleanly disconnected.");
                } catch (Exception e) {
                    Log.e(TAG, "Error cleaning up MQTT connection", e);
                }
                mqttClient = null;
            }).start();
        }
    }

    public boolean isRemoteActive() {
        return isRemoteActive;
    }

    public boolean isMqttConnected() {
        return mqttClient != null && mqttClient.isConnected() && isRemoteActive;
    }

    public void connectMqtt() {
        android.content.SharedPreferences prefs = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE);
        prefs.edit().putBoolean(MainActivity.KEY_USER_DISCONNECTED, false).apply();
        this.isRemoteActive = true;
        if (MainActivity.instance != null) {
            MainActivity.instance.updateStatusConnecting();
        }
        connectToMqttBroker();
    }

    public void disconnectMqtt() {
        android.content.SharedPreferences prefs = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE);
        prefs.edit().putBoolean(MainActivity.KEY_USER_DISCONNECTED, true).apply();
        this.isRemoteActive = false;
        if (MainActivity.instance != null) {
            MainActivity.instance.updateStatusDisconnecting();
        }
        new Thread(() -> {
            if (mqttClient != null) {
                try {
                    JSONObject status = new JSONObject();
                    status.put("status", "OFFLINE");
                    status.put("remote_state", "STANDBY");
                    MqttMessage msg = new MqttMessage(status.toString().getBytes());
                    msg.setQos(1);
                    mqttClient.publish(getStatusTopic(), msg);
                    mqttClient.disconnect();
                    mqttClient.close();
                    Log.d(TAG, "MQTT Client cleanly disconnected by user.");
                } catch (Exception e) {
                    Log.e(TAG, "Error disconnecting MQTT", e);
                }
                mqttClient = null;
            }
            if (MainActivity.instance != null) {
                MainActivity.instance.updateStatusOffline("Disconnected");
            }
        }).start();
    }

    private void connectToMqttBroker() {
        if (mqttClient != null && mqttClient.isConnected()) {
            if (MainActivity.instance != null) {
                MainActivity.instance.updateStatusOnline();
            }
            return;
        }

        new Thread(() -> {
            try {
                mqttClient = new MqttClient(brokerUrl, MqttClient.generateClientId(), new MemoryPersistence());
                MqttConnectOptions options = new MqttConnectOptions();
                options.setAutomaticReconnect(true);
                options.setCleanSession(true);
                options.setKeepAliveInterval(30);
                options.setConnectionTimeout(15);
                if (brokerUrl.equals(PRIMARY_BROKER_URL)) {
                    options.setServerURIs(new String[] { PRIMARY_BROKER_URL, FALLBACK_BROKER_URL });
                } else {
                    options.setServerURIs(new String[] { brokerUrl, PRIMARY_BROKER_URL, FALLBACK_BROKER_URL });
                }

                mqttClient.setCallback(new MqttCallbackExtended() {
                    @Override
                    public void connectComplete(boolean reconnect, String serverURI) {
                        log("MQTT Connected! Reconnect=" + reconnect + " | URI=" + serverURI);
                        if (MainActivity.instance != null) {
                            MainActivity.instance.updateStatusOnline(); 
                        }
                        try {
                            // Subscribe only to unique device topics if username is configured
                            if (deviceUsername != null && !deviceUsername.trim().isEmpty()) {
                                mqttClient.subscribe(getCommandsTopic());
                                mqttClient.subscribe(getStatusTopic());
                                Log.d(TAG, "Subscribed to MQTT topics: " + getCommandsTopic() + ", " + getStatusTopic());
                            }
                            if (deviceId != null && !deviceId.trim().isEmpty()) {
                                mqttClient.subscribe("aiml/" + deviceId + "/register_response");
                            }
                            publishDeviceRegistration();
                        } catch (Exception e) {
                            Log.e(TAG, "Subscription Error after connect", e);
                        }
                    }

                    @Override
                    public void connectionLost(Throwable cause) {
                        String errorMsg = (cause != null ? cause.getMessage() : "Unknown cause");
                        Log.w(TAG, "MQTT Connection lost: " + errorMsg);
                        if (MainActivity.instance != null) {
                            MainActivity.instance.updateStatusOffline(errorMsg);
                        }
                        
                        // Only auto-reconnect if remote is still actively set to connect and user hasn't disconnected
                        if (isRemoteActive) {
                            android.content.SharedPreferences prefs = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE);
                            boolean userDisconnected = prefs.getBoolean(MainActivity.KEY_USER_DISCONNECTED, false);
                            if (!userDisconnected) {
                                new Handler(Looper.getMainLooper()).postDelayed(() -> {
                                    if (isRemoteActive && (mqttClient == null || !mqttClient.isConnected())) {
                                        boolean disconnectedNow = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE)
                                            .getBoolean(MainActivity.KEY_USER_DISCONNECTED, false);
                                        if (!disconnectedNow) {
                                            log("Attempting manual MQTT reconnection...");
                                            connectToMqttBroker();
                                        }
                                    }
                                }, 5000);
                            }
                        }
                    }

                    @Override
                    public void messageArrived(String topic, MqttMessage message) {
                        try {
                            String payload = new String(message.getPayload()).trim();
                            Log.d(TAG, "MQTT Message Arrived | Topic: " + topic + " | Payload: " + payload);
                            
                            JSONObject json = null;
                            if (payload.startsWith("[")) {
                                JSONArray array = new JSONArray(payload);
                                if (array.length() > 0) {
                                    json = array.getJSONObject(array.length() - 1);
                                }
                            } else {
                                json = new JSONObject(payload);
                            }

                            if (json != null) {
                                if (topic.equals("aiml/" + deviceId + "/register_response")) {
                                    handleRegistrationResponse(json);
                                    return;
                                }

                                if (topic.equals(getStatusTopic())) {
                                    handleStatusUpdate(topic, payload, json);
                                    return;
                                }

                                String command = json.has("command") ? json.optString("command", "") : json.optString("Command", "");
                                if (command.isEmpty()) return;

                                // Filter by target_device if specified to prevent cross-device command crosstalk
                                String target = json.optString("target_device", "");
                                if (!target.isEmpty() && !target.equalsIgnoreCase(deviceUsername)
                                        && !target.equalsIgnoreCase(deviceId)
                                        && !target.equalsIgnoreCase("mobile") && !target.equalsIgnoreCase("all") && !target.equalsIgnoreCase("android")) {
                                    Log.d(TAG, "Target device '" + target + "' does not match our device '" + deviceUsername + "' / '" + deviceId + "', ignoring.");
                                    return;
                                }

                                // Deduplicate by command_id (prevents multiple executions from duplicate topics or QoS retries)
                                String commandId = json.optString("command_id", "");
                                if (!commandId.isEmpty() && isCommandAlreadyProcessed(commandId)) {
                                    Log.d(TAG, "Duplicate command dropped: " + commandId + " (" + command + ")");
                                    return;
                                }

                                // Log reception immediately to the on-screen console
                                log("MQTT RECEIVED: " + command);

                                if (!isRemoteActive) {
                                    log("COMMAND IGNORED: Remote is in StandBy mode. Command: " + command);
                                    Log.w(TAG, "Gating: Ignored command '" + command + "' because isRemoteActive=false");
                                    return;
                                }

                                String officialCommand = mapToOfficialCommand(command);
                                String rawQuery = json.optString("query", "");
                                String songUrl = json.optString("song_url", "");
                                boolean isSearchCommand = officialCommand.equals("Right_Search_Playlist") || officialCommand.equals("type_query") || officialCommand.equals("SEARCH");
                                final String query = (isSearchCommand && rawQuery.trim().isEmpty() && songUrl.trim().isEmpty()) ? DEFAULT_SEARCH_QUERY : rawQuery;
                                String title = json.optString("title", query);
                                // Publish ACK immediately upon receiving any valid command to minimize dashboard latency
                                publishAck(officialCommand);
                                
                                if (MainActivity.instance != null) {
                                    MainActivity.instance.runOnUiThread(() -> {
                                        MainActivity.instance.processIncomingBciPacket(officialCommand, query);
                                    });
                                }

                                new Thread(() -> {
                                    translateBrainwaveToMedia(officialCommand, query, songUrl, title);
                                }).start();
                            }
                        } catch (Exception e) {
                            Log.e(TAG, "MQTT Packet Parse Error", e);
                            log("MQTT ERROR: Failed to parse packet.");
                        }
                    }

                    @Override
                    public void deliveryComplete(IMqttDeliveryToken token) {}
                });

                mqttClient.connect(options);
            } catch (Exception e) {
                Log.e(TAG, "MQTT Connection Error", e);
            }
        }).start();
    }

    private void handleRegistrationResponse(JSONObject json) {
        if (json == null) return;
        String respDeviceId = json.optString("device_id", "");
        // Filter by deviceId if provided to ensure message was meant for this device
        if (!respDeviceId.isEmpty() && !respDeviceId.equalsIgnoreCase(this.deviceId)) {
            Log.d(TAG, "Ignoring registration response for different device ID: " + respDeviceId);
            return;
        }

        String status = json.optString("status", "");
        String username = json.optString("username", this.deviceUsername);
        String message = json.optString("message", "");
        String error = json.optString("error", "");

        if ("ERROR".equalsIgnoreCase(status) || "USERNAME_TAKEN".equalsIgnoreCase(error)) {
            log("REGISTRATION REJECTED: " + (message.isEmpty() ? "Username taken" : message));
            if (MainActivity.instance != null) {
                MainActivity.instance.runOnUiThread(() -> {
                    MainActivity.instance.onUsernameConflict(username, message);
                });
            }
        } else if ("SUCCESS".equalsIgnoreCase(status)) {
            log("REGISTRATION APPROVED: " + (message.isEmpty() ? "Verified" : message));
            if (MainActivity.instance != null) {
                MainActivity.instance.runOnUiThread(() -> {
                    MainActivity.instance.onUsernameApproved(username);
                });
            }
        }
    }

    private void handleStatusUpdate(String topic, String payload, JSONObject json) {
        if (!topic.equals(getStatusTopic())) {
            return;
        }
        String statusRaw = json.optString("status", "").toUpperCase();
        Log.d(TAG, "Status Topic Received: " + topic + " | Payload: " + payload);

        if (statusRaw.equals("ONLINE")) {
            if (MainActivity.instance != null) {
                MainActivity.instance.updatePythonStatus(true);
            }
            log("Python Pipeline Status: ONLINE");
        } else if (statusRaw.equals("OFFLINE")) {
            if (MainActivity.instance != null) {
                MainActivity.instance.updatePythonStatus(false);
            }
            log("Python Pipeline Status: OFFLINE");
        } else {
            // Ignore REMOTE_ACTIVE, REMOTE_STANDBY or other internal statuses
            Log.d(TAG, "Ignoring non-Python status: " + statusRaw);
        }
    }

    private String mapToOfficialCommand(String command) {
        if (command == null) return "";
        String cmdUpper = command.toUpperCase().trim();

        // Play / Pause mappings
        if (cmdUpper.equals("LEFT") || cmdUpper.equals("PLAY") || cmdUpper.equals("PAUSE")
                || cmdUpper.contains("PLAY_PAUSE") || cmdUpper.contains("PLAY/PAUSE")
                || cmdUpper.contains("PLAY / PAUSE")) {
            return "Right_Play_Pause";
        }
        // Next Song mappings
        if (cmdUpper.equals("PUSH") || cmdUpper.equals("NEXT")
                || cmdUpper.contains("NEXT_SONG") || cmdUpper.contains("NEXTTRACK")
                || cmdUpper.contains("NEXT_TRACK") || cmdUpper.contains("NEXT TRACK")) {
            return "Right_Next_Song";
        }
        // Previous Song mappings
        if (cmdUpper.equals("PULL") || cmdUpper.equals("PREV") || cmdUpper.equals("PREVIOUS")
                || cmdUpper.contains("PREVIOUS_SONG") || cmdUpper.contains("PREVTRACK")
                || cmdUpper.contains("PREV_TRACK") || cmdUpper.contains("PREV_SONG")) {
            return "Right_Previous_Song";
        }
        // Volume Up mappings
        if (cmdUpper.equals("RIGHT+PUSH") || cmdUpper.equals("RIGHT_PUSH")
                || cmdUpper.contains("VOLUME_UP") || cmdUpper.contains("VOLUMEUP")
                || cmdUpper.contains("VOLUME UP") || cmdUpper.contains("VOL_UP")) {
            return "Right_Volume_Up";
        }
        // Volume Down mappings
        if (cmdUpper.equals("RIGHT+PULL") || cmdUpper.equals("RIGHT_PULL")
                || cmdUpper.contains("VOLUME_DOWN") || cmdUpper.contains("VOLUMEDOWN")
                || cmdUpper.contains("VOLUME DOWN") || cmdUpper.contains("VOL_DOWN")) {
            return "Right_Volume_Down";
        }
        // Search Playlist / Query mappings
        if (cmdUpper.equals("RIGHT") || cmdUpper.equals("SEARCH")
                || cmdUpper.contains("SEARCH_PLAYLIST") || cmdUpper.contains("PLAYLIST")
                || cmdUpper.contains("TYPE_QUERY")) {
            return "Right_Search_Playlist";
        }
        // Close App / Return to home / Drop / Back to Level 2 mappings
        if (cmdUpper.equals("RIGHT_CLOSE_APP") || cmdUpper.equals("CLOSE")
                || cmdUpper.contains("CLOSE_APP") || cmdUpper.contains("CLOSE_JIOSAAVN")
                || cmdUpper.contains("BACK_TO_LEVEL_2") || cmdUpper.equals("PUSH+RIGHT")
                || cmdUpper.equals("PUSH_RIGHT") || cmdUpper.equals("DROP")
                || cmdUpper.equals("BACK") || cmdUpper.equals("HOME")
                || cmdUpper.contains("RETURN_TO_HOME") || cmdUpper.contains("RETURN TO HOME")) {
            return "Right_Close_App";
        }
        // JioSaavn launch
        if (cmdUpper.contains("JIOSAAVN") || cmdUpper.contains("OPEN_JIOSAAVN")) {
            return "Right_JioSaavn";
        }
        return command; // Default to raw if no mapping found
    }



    /**
     * Publishes an ACK back to the Python pipeline on aiml/<username>/ack.
     * Matches the subscriber.py handler: data.get("status") == "EXECUTED"
     */
    private void publishAck(String command) {
        publishAck(command, null);
    }

    private void publishAck(String command, String successfulTier) {
        if (mqttClient != null && mqttClient.isConnected()) {
            try {
                JSONObject ack = new JSONObject();
                ack.put("status", "EXECUTED");
                ack.put("command", command);
                if (successfulTier != null && !successfulTier.isEmpty()) {
                    ack.put("tier", successfulTier);
                }
                ack.put("timestamp", new java.text.SimpleDateFormat(
                    "yyyy-MM-dd'T'HH:mm:ss", java.util.Locale.getDefault())
                    .format(new java.util.Date()));

                MqttMessage msg = new MqttMessage(ack.toString().getBytes());
                msg.setQos(1);
                publishMqttAsync(getAckTopic(), msg);
                log("ACK published: " + command + (successfulTier != null ? " [" + successfulTier + "]" : ""));
            } catch (Exception e) {
                Log.e(TAG, "ACK publish failed for " + command, e);
                log("ACK FAILED: Check MQTT connection.");
            }
        } else {
            Log.e(TAG, "Cannot publish ACK: MQTT Client not connected.");
            log("ACK FAILED: MQTT Client not connected.");
        }
    }

    /**
     * Publishes a FAILED ACK back to the Python pipeline.
     * This is sent when post-execution verification detects that the command
     * was silently dropped by the OS (e.g., ColorOS background restrictions).
     */
    private void publishFailedAck(String command, String reason) {
        if (mqttClient != null && mqttClient.isConnected()) {
            try {
                JSONObject ack = new JSONObject();
                ack.put("status", "FAILED");
                ack.put("command", command);
                ack.put("reason", reason);
                ack.put("timestamp", new java.text.SimpleDateFormat(
                    "yyyy-MM-dd'T'HH:mm:ss", java.util.Locale.getDefault())
                    .format(new java.util.Date()));

                MqttMessage msg = new MqttMessage(ack.toString().getBytes());
                msg.setQos(1);
                publishMqttAsync(getAckTopic(), msg);
                log("FAILED ACK published: " + command + " | Reason: " + reason);
            } catch (Exception e) {
                Log.e(TAG, "FAILED ACK publish failed", e);
            }
        }
    }

    /**
     * Captures the current PlaybackState code from JioSaavn's MediaController.
     * Returns -1 if no controller or state is available.
     */
    private int capturePlaybackState() {
        try {
            MediaController controller = findJioSaavnController(1, 200);
            if (controller != null) {
                PlaybackState state = controller.getPlaybackState();
                if (state != null) {
                    return state.getState();
                }
            }
        } catch (Exception e) {
            log("capturePlaybackState error: " + e.getMessage());
        }
        return -1;
    }

    /**
     * Captures the current media queue position (active queue item ID).
     * Returns -1 if unavailable. Used to verify next/previous song actually changed tracks.
     */
    private long captureQueuePosition() {
        try {
            MediaController controller = findJioSaavnController(1, 200);
            if (controller != null) {
                PlaybackState state = controller.getPlaybackState();
                if (state != null) {
                    return state.getPosition();
                }
            }
        } catch (Exception e) {
            log("captureQueuePosition error: " + e.getMessage());
        }
        return -1;
    }



    public void setRemoteActive(boolean active) {
        this.isRemoteActive = active;
        
        if (active && (mqttClient == null || !mqttClient.isConnected())) {
            log("Remote set to ACTIVE but MQTT disconnected. Nudging...");
            connectToMqttBroker();
        }

        if (mqttClient != null && mqttClient.isConnected()) {
            try {
                JSONObject status = new JSONObject();
                if (active) {
                    status.put("status", "REMOTE_ACTIVE");
                    status.put("remote_state", "ACTIVE");
                } else {
                    status.put("status", "REMOTE_STANDBY");
                    status.put("remote_state", "STANDBY");
                }
                MqttMessage msg = new MqttMessage(status.toString().getBytes());
                msg.setQos(1);
                publishMqttAsync(getStatusTopic(), msg);
                log("Published remote status: " + (active ? "ACTIVE" : "STANDBY"));
            } catch (Exception e) {
                Log.e(TAG, "Failed to publish remote status", e);
            }
        }
    }

    public void pausePlaybackIfPlaying() {
        try {
            MediaController controller = findJioSaavnController(2, 400);
            if (controller != null) {
                PlaybackState state = controller.getPlaybackState();
                if (state == null || state.getState() == PlaybackState.STATE_PLAYING) {
                    controller.getTransportControls().pause();
                    log("Paused JioSaavn playback via MediaController.");
                }
            }
        } catch (Exception e) {
            Log.e(TAG, "Failed to pause via MediaController: " + e.getMessage());
        }
        try {
            AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            if (am != null && am.isMusicActive()) {
                am.dispatchMediaKeyEvent(new KeyEvent(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_MEDIA_PAUSE));
                am.dispatchMediaKeyEvent(new KeyEvent(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_MEDIA_PAUSE));
                log("Dispatched KEYCODE_MEDIA_PAUSE via AudioManager.");
            }
        } catch (Exception e) {
            Log.e(TAG, "Failed to pause via AudioManager: " + e.getMessage());
        }
    }

    public void launchJioSaavn() {
        if (BciAccessibilityService.instance != null) {
            BciAccessibilityService.instance.launchJioSaavn();
            return;
        }
        try {
            Intent intent = getPackageManager().getLaunchIntentForPackage(JIOSAAVN_PACKAGE_BEATS);
            if (intent == null) {
                intent = getPackageManager().getLaunchIntentForPackage(JIOSAAVN_PACKAGE_SAAVN);
            }
            if (intent != null) {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(intent);
            } else {
                Intent fallback = new Intent(Intent.ACTION_MAIN);
                fallback.setComponent(new ComponentName("com.jio.media.jiobeats", "com.jio.media.jiobeats.MainActivity"));
                fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(fallback);
            }
        } catch (Exception e) {
            Log.e(TAG, "Error launching JioSaavn: " + e.getMessage());
        }
    }

    private void ensureJioSaavnForeground() {
        if (BciAccessibilityService.instance == null) {
            Log.w(TAG, "BciAccessibilityService.instance is null! Accessibility Service is not enabled. Launching JioSaavn directly without wait loop.");
            if (MainActivity.instance != null) {
                MainActivity.instance.appendLog("SYSTEM WARNING: Accessibility Service is not enabled. Cannot verify foreground status.");
            }
            launchJioSaavn();
            return;
        }

        boolean isForeground = BciAccessibilityService.instance.checkJioSaavnForeground();
        if (!isForeground) {
            Log.d(TAG, "Universal Rule: JioSaavn is not in foreground. Launching directly...");
            if (MainActivity.instance != null) {
                MainActivity.instance.appendLog("SYSTEM: JioSaavn not in foreground. Launching directly...");
            }
            BciAccessibilityService.instance.launchJioSaavn();
            // Wait for it to become foregrounded
            long start = System.currentTimeMillis();
            while (System.currentTimeMillis() - start < 2500) {
                try {
                    Thread.sleep(150);
                } catch (InterruptedException e) {
                    break;
                }
                if (BciAccessibilityService.instance.checkJioSaavnForeground()) {
                    Log.d(TAG, "JioSaavn package verified in foreground.");
                    break;
                }
            }
        }
    }

    /**
     * Attempts to find an active MediaController for JioSaavn.
     * Retries up to maxRetries times with retryDelayMs between each attempt.
     * Returns null if not found within the retry window.
     */
    private MediaController findJioSaavnController(int maxRetries, long retryDelayMs) {
        return findJioSaavnController(maxRetries, retryDelayMs, false);
    }

    private MediaController findJioSaavnController(int maxRetries, long retryDelayMs, boolean silent) {
        try {
            MediaSessionManager sessionManager =
                (MediaSessionManager) getSystemService(Context.MEDIA_SESSION_SERVICE);
            if (sessionManager == null) return null;

            ComponentName listenerComponent = new ComponentName(this, BciMediaService.class);

            for (int attempt = 1; attempt <= maxRetries; attempt++) {
                List<MediaController> controllers =
                    sessionManager.getActiveSessions(listenerComponent);

                if (!silent && controllers != null && !controllers.isEmpty()) {
                    StringBuilder sb = new StringBuilder("Active Media Sessions: ");
                    for (MediaController mc : controllers) {
                        sb.append(mc.getPackageName()).append(", ");
                    }
                    log(sb.toString());
                }

                for (MediaController c : controllers) {
                    if (isJioSaavnPackage(c.getPackageName())) {
                        log("MediaController found for JioSaavn on attempt " + attempt);
                        return c;
                    }
                }

                if (!silent) {
                    log("JioSaavn MediaController not found. Attempt " + attempt + "/" + maxRetries + ". Retrying...");
                }

                if (attempt < maxRetries) {
                    try { Thread.sleep(retryDelayMs); } catch (InterruptedException ignored) {}
                }
            }
        } catch (SecurityException se) {
            if (!silent) log("SecurityException: Notification Listener permission missing. " + se.getMessage());
        } catch (Exception e) {
            if (!silent) log("findJioSaavnController error: " + e.getMessage());
        }
        if (!silent) {
            log("JioSaavn MediaController not found after all retries. Will use fallback path.");
        }
        return null;
    }

    /**
     * Tier 2 & 3 Fallback: dispatch a media KeyEvent via AudioManager,
     * then also via direct KeyEvent injection for maximum compatibility.
     */


    public void openJioSaavnDeeplink(String songUrl, String title) {
        if (songUrl == null || songUrl.trim().isEmpty()) {
            log("Cannot open deep link: empty URL");
            return;
        }
        final String effectiveTitle = (title != null && !title.trim().isEmpty()) ? title.trim() : "";
        final String trimmedUrl = songUrl.trim();
        long now = SystemClock.uptimeMillis();
        if (trimmedUrl.equalsIgnoreCase(lastDeeplinkUrl) && (now - lastDeeplinkTime < 2500)) {
            log("Debounced duplicate JioSaavn deep link for: " + trimmedUrl);
            return;
        }
        lastDeeplinkTime = now;
        lastDeeplinkUrl = trimmedUrl;

        log("Opening JioSaavn deep link: " + trimmedUrl + (!effectiveTitle.isEmpty() ? " (" + effectiveTitle + ")" : ""));
        new Handler(Looper.getMainLooper()).post(() -> {
            try {
                // Pause any existing playback immediately so previous audio does not bleed into the new song
                pausePlaybackIfPlaying();

                Uri uri = Uri.parse(trimmedUrl);
                Intent intent = new Intent(Intent.ACTION_VIEW, uri);
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);

                boolean launched = false;
                String[] packages = new String[]{JIOSAAVN_PACKAGE_BEATS, JIOSAAVN_PACKAGE_SAAVN};
                for (String pkg : packages) {
                    try {
                        getPackageManager().getPackageInfo(pkg, 0);
                        intent.setPackage(pkg);
                        startActivity(intent);
                        launched = true;
                        log("Launched JioSaavn deep link via package: " + pkg);
                        break;
                    } catch (Exception ignored) {}
                }
                if (!launched) {
                    intent.setPackage(null);
                    startActivity(intent);
                    log("Launched deep link via general intent.");
                }

                // 1. Immediately invoke BciAccessibilityService to find and click the song item / play button
                if (BciAccessibilityService.instance != null) {
                    log("Invoking BciAccessibilityService.playSearchedSong for '" + effectiveTitle + "'");
                    BciAccessibilityService.instance.playSearchedSong(effectiveTitle);
                } else {
                    log("BciAccessibilityService instance is null! Fallback to media transport controls.");
                }

                // Rely solely on BciAccessibilityService clicking the actual song result on screen.
                // ponytail: DO NOT use TransportControls.play() or KEYCODE_MEDIA_PLAY here â€”
                // they resume the previously-loaded song, not the newly-searched one.

                publishAck("Right_Search_Playlist", "Deeplink: " + songUrl);
            } catch (Exception e) {
                log("Error opening deep link: " + e.getMessage());
                publishFailedAck("Right_Search_Playlist", "Deeplink error: " + e.getMessage());
            }
        });
    }



    public void searchAndPlaySong(String query) {
        final String cleanQuery = (query != null && !query.trim().isEmpty()) ? query.trim() : DEFAULT_SEARCH_QUERY;
        log("Querying JioSaavn endpoint for: '" + cleanQuery + "'...");

        new Thread(() -> {
            java.net.HttpURLConnection conn = null;
            try {
                String encoded = java.net.URLEncoder.encode(cleanQuery, "UTF-8");
                String apiUrl = "https://www.jiosaavn.com/api.php?__call=autocomplete.get&_format=json&_marker=0&cc=in&includeMetaTags=1&query=" + encoded;

                java.net.URL url = new java.net.URL(apiUrl);
                conn = (java.net.HttpURLConnection) url.openConnection();
                conn.setRequestMethod("GET");
                conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36");
                conn.setConnectTimeout(5000);
                conn.setReadTimeout(5000);

                int responseCode = conn.getResponseCode();
                if (responseCode == 200) {
                    java.io.BufferedReader in = new java.io.BufferedReader(new java.io.InputStreamReader(conn.getInputStream()));
                    StringBuilder response = new StringBuilder();
                    String inputLine;
                    while ((inputLine = in.readLine()) != null) {
                        response.append(inputLine);
                    }
                    in.close();

                    JSONObject json = new JSONObject(response.toString());
                    String songUrl = null;
                    String songTitle = cleanQuery;

                    if (json.has("songs")) {
                        JSONObject songsObj = json.optJSONObject("songs");
                        if (songsObj != null && songsObj.has("data")) {
                            JSONArray data = songsObj.optJSONArray("data");
                            if (data != null && data.length() > 0) {
                                JSONObject firstSong = data.getJSONObject(0);
                                songUrl = firstSong.optString("url", null);
                                songTitle = firstSong.optString("title", cleanQuery);
                            }
                        }
                    }

                    if ((songUrl == null || songUrl.isEmpty()) && json.has("topquery")) {
                        JSONObject topObj = json.optJSONObject("topquery");
                        if (topObj != null && topObj.has("data")) {
                            JSONArray data = topObj.optJSONArray("data");
                            if (data != null && data.length() > 0) {
                                JSONObject firstSong = data.getJSONObject(0);
                                songUrl = firstSong.optString("url", null);
                                songTitle = firstSong.optString("title", cleanQuery);
                            }
                        }
                    }

                    if (songUrl != null && !songUrl.isEmpty()) {
                        log("JioSaavn resolved: '" + songTitle + "' -> " + songUrl);
                        openJioSaavnDeeplink(songUrl, songTitle);
                    } else {
                        log("JioSaavn search: No results found for '" + cleanQuery + "'");
                        if (BciAccessibilityService.instance != null) {
                            BciAccessibilityService.instance.searchPlaylist(cleanQuery);
                        }
                    }
                } else {
                    log("JioSaavn API HTTP error: " + responseCode);
                }
            } catch (Exception e) {
                log("searchAndPlaySong error: " + e.getMessage());
                if (BciAccessibilityService.instance != null) {
                    BciAccessibilityService.instance.searchPlaylist(cleanQuery);
                }
            } finally {
                if (conn != null) {
                    try { conn.disconnect(); } catch (Exception ignored) {}
                }
            }
        }).start();
    }

    private void translateBrainwaveToMedia(String officialCommand, String query, String songUrl, String title) {
        if (officialCommand == null || officialCommand.trim().isEmpty()) return;

        final String effectiveTitle = (title != null && !title.trim().isEmpty()) ? title.trim() : query;
        log("Translating command: " + officialCommand + (query.isEmpty() ? "" : " with query: " + query) + (!effectiveTitle.isEmpty() ? " with title: " + effectiveTitle : "") + (songUrl != null && !songUrl.isEmpty() ? " with url: " + songUrl : ""));

        // â”€â”€ Volume: AudioManager always works universally â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if (officialCommand.equals("Right_Volume_Up") || officialCommand.equals("Right_Volume_Down")) {
            handleVolume(officialCommand);
            return;
        }

        // â”€â”€ Close JioSaavn / Return to Home â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if (officialCommand.equals("Right_Close_App")
                || officialCommand.equals("Right_Return_to_Home")
                || officialCommand.equals("Drop")) {
            log("Close JioSaavn command received. Pausing playback and closing JioSaavn...");
            pausePlaybackIfPlaying();
            if (BciAccessibilityService.instance != null) {
                log("Closing JioSaavn via AccessibilityService...");
                BciAccessibilityService.instance.closeJioSaavn();
            } else {
                log("AccessibilityService unavailable. Sending HOME intent fallback...");
                try {
                    Intent homeIntent = new Intent(Intent.ACTION_MAIN);
                    homeIntent.addCategory(Intent.CATEGORY_HOME);
                    homeIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(homeIntent);
                } catch (Exception e) {
                    log("Failed to send HOME intent: " + e.getMessage());
                }
            }
            publishAck(officialCommand);
            return;
        }

        // â”€â”€ Launch JioSaavn â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if (officialCommand.equals("Right_JioSaavn")) {
            log("JioSaavn launch command received. Ensuring foreground...");
            ensureJioSaavnForeground();
            publishAck(officialCommand);
            return;
        }

        // â”€â”€ Search Playlist / type_query / SEARCH â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if (officialCommand.equals("Right_Search_Playlist") || officialCommand.equals("type_query") || officialCommand.equals("SEARCH")) {
            if (songUrl != null && !songUrl.trim().isEmpty()) {
                log("Search received with deep link URL: " + songUrl + ", title: " + effectiveTitle);
                openJioSaavnDeeplink(songUrl, effectiveTitle);
                return;
            }
            String effectiveQuery = (query != null && !query.trim().isEmpty()) ? query.trim() : DEFAULT_SEARCH_QUERY;
            log("Search received with query: '" + effectiveQuery + "'. Resolving URL...");
            searchAndPlaySong(effectiveQuery);
            return;
        }

        // â”€â”€ Play / Pause / Next / Previous â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


        // â”€â”€ Capture state BEFORE execution for post-verification â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        final int preState = capturePlaybackState();
        final long prePosition = captureQueuePosition();
        log("Pre-execution state capture: PlaybackState=" + preState + ", Position=" + prePosition);

        // â”€â”€ TIER 1: Direct MediaController (Universal & Brand-safe) â”€â”€â”€â”€â”€â”€â”€â”€â”€
        log("Tier 1: Searching for JioSaavn active MediaController...");
        // Check for active background session first. If none exists, ensure foreground and retry.
        MediaController controller = findJioSaavnController(1, 0);
        if (controller == null) {
            log("Tier 1: No active background session found. Ensuring JioSaavn is foregrounded...");
            ensureJioSaavnForeground();
            controller = findJioSaavnController(2, 300);
        }

        if (controller != null) {
            try {
                switch (officialCommand) {
                    case "Right_Play_Pause": {
                        PlaybackState state = controller.getPlaybackState();
                        boolean isPlaying = state != null
                            && state.getState() == PlaybackState.STATE_PLAYING;
                        if (isPlaying) {
                            controller.getTransportControls().pause();
                            log("Tier 1: MediaController.pause() sent to JioSaavn.");
                        } else {
                            controller.getTransportControls().play();
                            log("Tier 1: MediaController.play() sent to JioSaavn.");
                        }
                        // Post-execution verification after 500ms
                        verifyPlaybackStateChange(officialCommand, preState, "Tier 1: MediaController");
                        return;
                    }
                    case "Right_Next_Song":
                        controller.getTransportControls().skipToNext();
                        log("Tier 1: MediaController.skipToNext() sent to JioSaavn.");
                        verifyTrackChange(officialCommand, prePosition, "Tier 1: MediaController");
                        return;

                    case "Right_Previous_Song":
                        controller.getTransportControls().skipToPrevious();
                        log("Tier 1: MediaController.skipToPrevious() sent to JioSaavn.");
                        verifyTrackChange(officialCommand, prePosition, "Tier 1: MediaController");
                        return;
                }
            } catch (Exception e) {
                log("Tier 1 MediaController call failed: " + e.getMessage() + ". Falling back to Tier 2...");
            }
        }

        // â”€â”€ TIER 2: Accessibility UI Click Fallback â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        log("Tier 2: Attempting Accessibility UI Click Fallback...");
        if (BciAccessibilityService.instance != null) {
            boolean result = false;
            switch (officialCommand) {
                case "Right_Play_Pause":
                    result = BciAccessibilityService.instance.clickPlayPause();
                    if (result) verifyPlaybackStateChange(officialCommand, preState, "Tier 2: Accessibility UI");
                    break;
                case "Right_Next_Song":
                    result = BciAccessibilityService.instance.clickNextSong();
                    if (result) verifyTrackChange(officialCommand, prePosition, "Tier 2: Accessibility UI");
                    break;
                case "Right_Previous_Song":
                    result = BciAccessibilityService.instance.clickPreviousSong();
                    if (result) verifyTrackChange(officialCommand, prePosition, "Tier 2: Accessibility UI");
                    break;
            }
            if (result) return;
        } else {
            log("Tier 2: Accessibility Service not available.");
        }

        // â”€â”€ TIER 3: AudioManager KeyEvent Dispatch Fallback â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        log("Tier 3: JioSaavn session and UI click failed. Using AudioManager.dispatchMediaKeyEvent...");
        switch (officialCommand) {
            case "Right_Play_Pause":
                dispatchMediaKeyFallback(android.view.KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE);
                verifyPlaybackStateChange(officialCommand, preState, "Tier 3: AudioManager KeyEvent");
                break;
            case "Right_Next_Song":
                dispatchMediaKeyFallback(android.view.KeyEvent.KEYCODE_MEDIA_NEXT);
                verifyTrackChange(officialCommand, prePosition, "Tier 3: AudioManager KeyEvent");
                break;
            case "Right_Previous_Song":
                dispatchMediaKeyFallback(android.view.KeyEvent.KEYCODE_MEDIA_PREVIOUS);
                verifyTrackChange(officialCommand, prePosition, "Tier 3: AudioManager KeyEvent");
                break;
            default:
                log("No handler for command: " + officialCommand);
                publishFailedAck(officialCommand, "No handler found for command");
                break;
        }
    }

    /**
     * Post-execution verification for Play/Pause.
     * Waits 500ms, then re-checks PlaybackState to confirm the state actually toggled.
     * Publishes EXECUTED if state changed, FAILED if it didn't (OS silently dropped the call).
     */
    private void verifyPlaybackStateChange(String command, int preState, String tierName) {
        try {
            Thread.sleep(500);
        } catch (InterruptedException ignored) {}

        int postState = capturePlaybackState();
        log("Post-execution verification: PreState=" + preState + ", PostState=" + postState);

        if (preState == -1 || postState == -1) {
            // Cannot verify (no MediaController available), assume success
            log("Verification SKIPPED (no PlaybackState available). Assuming success.");
            publishAck(command, tierName);
        } else if (preState != postState) {
            log("Verification PASSED: PlaybackState changed from " + preState + " to " + postState);
            publishAck(command, tierName);
        } else {
            log("Verification FAILED: PlaybackState unchanged (" + preState + "). OS may have silently dropped the call.");
            publishFailedAck(command, "PlaybackState unchanged after execution (" + tierName + ")");
        }
    }

    /**
     * Post-execution verification for Next/Previous track.
     * Waits 500ms, then re-checks playback position to confirm track actually changed.
     * Publishes EXECUTED if position changed, FAILED if it didn't.
     */
    private void verifyTrackChange(String command, long prePosition, String tierName) {
        try {
            Thread.sleep(500);
        } catch (InterruptedException ignored) {}

        long postPosition = captureQueuePosition();
        log("Post-execution verification: PrePosition=" + prePosition + ", PostPosition=" + postPosition);

        if (prePosition == -1 || postPosition == -1) {
            // Cannot verify (no MediaController available), assume success
            log("Verification SKIPPED (no position data available). Assuming success.");
            publishAck(command, tierName);
        } else if (prePosition != postPosition) {
            log("Verification PASSED: Track position changed from " + prePosition + " to " + postPosition);
            publishAck(command, tierName);
        } else {
            log("Verification FAILED: Track position unchanged (" + prePosition + "). OS may have silently dropped the call.");
            publishFailedAck(command, "Track position unchanged after execution (" + tierName + ")");
        }
    }

    /**
     * Handles volume up/down using a robust 3-tier approach:
     * Tier 1 – In-app JioSaavn UI volume slider via Accessibility Service
     * Tier 2 – Direct MediaButton KeyEvent Injection into JioSaavn's MediaController
     * Tier 3 – Hardware STREAM_MUSIC Volume State Management (AudioManager.setStreamVolume)
     */
    private void handleVolume(String officialCommand) {
        log("handleVolume invoked for: " + officialCommand);
        boolean isVolumeUp = officialCommand.equals("Right_Volume_Up");

        // Capture initial volume state for post-execution verification
        AudioManager amCapture = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        int initialVolume = (amCapture != null) ? amCapture.getStreamVolume(AudioManager.STREAM_MUSIC) : -1;
        log("Volume Pre-Capture: initialVolume=" + initialVolume);

        // ── Tier 1: In-app JioSaavn UI volume slider via Accessibility Service (Primary) ──
        log("Volume Control: Attempting Tier 1 (JioSaavn UI Volume Slider)...");
        if (tryAccessibilityVolume(officialCommand)) {
            if (verifyVolumeChange(officialCommand, initialVolume, "Tier 1: JioSaavn UI Volume Slider")) {
                return;
            }
            log("Volume Control: Tier 1 executed but volume unchanged. Falling back to Tier 2...");
        } else {
            log("Volume Control: Tier 1 FAILED. Falling back to Tier 2...");
        }

        // Re-capture volume
        initialVolume = (amCapture != null) ? amCapture.getStreamVolume(AudioManager.STREAM_MUSIC) : -1;

        // ── Tier 2: Direct MediaButton KeyEvent Injection into JioSaavn's MediaController ──
        log("Volume Control: Attempting Tier 2 (MediaController Direct KeyEvent)...");
        MediaController controller = findJioSaavnController(1, 0);
        if (controller != null) {
            try {
                int keyCode = isVolumeUp ? android.view.KeyEvent.KEYCODE_VOLUME_UP : android.view.KeyEvent.KEYCODE_VOLUME_DOWN;
                android.view.KeyEvent down = new android.view.KeyEvent(android.view.KeyEvent.ACTION_DOWN, keyCode);
                android.view.KeyEvent up = new android.view.KeyEvent(android.view.KeyEvent.ACTION_UP, keyCode);
                controller.dispatchMediaButtonEvent(down);
                controller.dispatchMediaButtonEvent(up);
                log("Volume Control: MediaController KeyEvent dispatched.");
                if (verifyVolumeChange(officialCommand, initialVolume, "Tier 2: MediaController Direct KeyEvent")) {
                    return;
                }
                log("Volume Control: Tier 2 executed but volume unchanged. Falling back to Tier 3...");
            } catch (Exception e) {
                log("Volume Control: Tier 2 Exception: " + e.getMessage());
            }
        } else {
            log("Volume Control: Tier 2 FAILED (no controller). Falling back to Tier 3...");
        }

        // Re-capture volume
        initialVolume = (amCapture != null) ? amCapture.getStreamVolume(AudioManager.STREAM_MUSIC) : -1;

        // ── Tier 3: Hardware STREAM_MUSIC Volume State Management (Fallback only) ──
        log("Volume Control: Attempting Tier 3 (Hardware STREAM_MUSIC Volume Fallback)...");
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        if (am != null) {
            try {
                int curVol = am.getStreamVolume(AudioManager.STREAM_MUSIC);
                int maxVol = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
                if (maxVol <= 0) maxVol = 15;
                int step = Math.max(1, (int) Math.round(maxVol * 0.15));
                int targetVol = isVolumeUp ? Math.min(maxVol, curVol + step) : Math.max(0, curVol - step);
                am.setStreamVolume(AudioManager.STREAM_MUSIC, targetVol, AudioManager.FLAG_SHOW_UI);
                log("Volume Control: Tier 3 set volume to " + targetVol + "/" + maxVol);
                if (verifyVolumeChange(officialCommand, initialVolume, "Tier 3: Hardware STREAM_MUSIC Volume")) {
                    return;
                }
            } catch (Exception e) {
                log("Volume Control: Tier 3 Exception: " + e.getMessage());
            }
        }

        publishFailedAck(officialCommand, "All volume tiers exhausted without success");
    }

/**
 * Post-execution verification for volume changes.
 * Waits 400ms, re-reads volume, and publishes EXECUTED if changed, returns false if not.
 */
private boolean verifyVolumeChange(String command, int initialVolume, String tierName) {
    try { Thread.sleep(400); } catch (InterruptedException ignored) {}

    AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
    if (am == null) {
        log("Volume verification: AudioManager null. Assuming success.");
        publishAck(command, tierName);
        return true;
    }

    int postVolume = am.getStreamVolume(AudioManager.STREAM_MUSIC);
    log("Volume Post-Verification: initial=" + initialVolume + ", post=" + postVolume);

    if (initialVolume == -1) {
        // Could not capture initial, assume success
        log("Volume verification SKIPPED (no initial capture). Assuming success.");
        publishAck(command, tierName);
        return true;
    }

    if (postVolume != initialVolume) {
        log("Volume Verification PASSED: " + initialVolume + " -> " + postVolume);
        publishAck(command, tierName);
        return true;
    } else {
        log("Volume Verification FAILED: volume still " + postVolume + " (" + tierName + ")");
        return false;
    }
}

    /** Attempts in-app JioSaavn volume slider UI manipulation via BciAccessibilityService (Tier 1). */
    private boolean tryAccessibilityVolume(String command) {
        if (BciAccessibilityService.instance == null) {
            log("Tier 1: AccessibilityService instance is null/disabled.");
            return false;
        }
        try {
            boolean isVolumeUp = command.equals("Right_Volume_Up");
            boolean result = BciAccessibilityService.instance.adjustJioSaavnVolumeUi(isVolumeUp);
            log("Tier 1: Accessibility UI volume slider adjust returned = " + result);
            return result;
        } catch (Exception e) {
            log("Tier 1 AccessibilityService UI volume failed: " + e.getMessage());
            return false;
        }
    }

    private void log(String message) {
        Log.d(TAG, message);
        if (MainActivity.instance != null) {
            MainActivity.instance.appendLog(message);
        }
    }

    /** Fallback: dispatch media key events via AudioManager when MediaController is unavailable. */
    private void dispatchMediaKeyFallback(int keyCode) {
        try {
            AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            if (am == null) {
                log("AudioManager null in dispatchMediaKeyFallback");
                return;
            }
            android.view.KeyEvent down = new android.view.KeyEvent(android.view.KeyEvent.ACTION_DOWN, keyCode);
            android.view.KeyEvent up = new android.view.KeyEvent(android.view.KeyEvent.ACTION_UP, keyCode);
            am.dispatchMediaKeyEvent(down);
            am.dispatchMediaKeyEvent(up);
            log("dispatchMediaKeyFallback: dispatched keycode=" + keyCode);
        } catch (Exception e) {
            log("dispatchMediaKeyFallback failed: " + e.getMessage());
        }
    }

    private boolean isJioSaavnPackage(String pkg) {
        return JIOSAAVN_PACKAGE_BEATS.equals(pkg) || JIOSAAVN_PACKAGE_SAAVN.equals(pkg);
    }

    private void setupMediaSessionListeners() {
        mediaCallback = new MediaController.Callback() {
            @Override
            public void onPlaybackStateChanged(PlaybackState state) {
                if (state != null && state.getState() == PlaybackState.STATE_PLAYING) {
                    startProgressUpdates();
                } else {
                    stopProgressUpdates();
                }
                publishMediaUpdate();
            }

            @Override
            public void onMetadataChanged(MediaMetadata metadata) {
                if (metadata != null) {
                    log("MediaMetadata Changed. Keys: " + metadata.keySet().toString());
                    for (String key : metadata.keySet()) {
                        try {
                            Object val = metadata.getString(key);
                            if (val == null) val = metadata.getLong(key);
                            Log.d(TAG, "Metadata: " + key + " = " + val);
                        } catch (Exception ignored) {}
                    }
                }
                publishMediaUpdate();
            }
        };

        sessionsChangedListener = controllers -> {
            MediaController jioController = null;
            if (controllers != null) {
                for (MediaController controller : controllers) {
                    if (isJioSaavnPackage(controller.getPackageName())) {
                        jioController = controller;
                        break;
                    }
                }
            }
            updateActiveController(jioController);
        };

        MediaSessionManager sessionManager = (MediaSessionManager) getSystemService(Context.MEDIA_SESSION_SERVICE);
        if (sessionManager != null) {
            ComponentName listenerComponent = new ComponentName(this, BciMediaService.class);
            sessionManager.addOnActiveSessionsChangedListener(sessionsChangedListener, listenerComponent);
            // Initial check
            try {
                List<MediaController> controllers = sessionManager.getActiveSessions(listenerComponent);
                sessionsChangedListener.onActiveSessionsChanged(controllers);
            } catch (SecurityException e) {
                log("SecurityException in setupMediaSessionListeners: " + e.getMessage());
            }
        }
    }

    private void updateActiveController(MediaController newController) {
        if (activeMediaController != null) {
            activeMediaController.unregisterCallback(mediaCallback);
        }
        activeMediaController = newController;
        if (activeMediaController != null) {
            activeMediaController.registerCallback(mediaCallback);
            publishMediaUpdate();
            PlaybackState state = activeMediaController.getPlaybackState();
            if (state != null && state.getState() == PlaybackState.STATE_PLAYING) {
                startProgressUpdates();
            }
        } else {
            stopProgressUpdates();
        }
    }

    private void cleanupMediaSessionListeners() {
        MediaSessionManager sessionManager = (MediaSessionManager) getSystemService(Context.MEDIA_SESSION_SERVICE);
        if (sessionManager != null && sessionsChangedListener != null) {
            sessionManager.removeOnActiveSessionsChangedListener(sessionsChangedListener);
        }
        if (activeMediaController != null) {
            activeMediaController.unregisterCallback(mediaCallback);
        }
    }

    private void startProgressUpdates() {
        if (progressRunnable != null) return;
        progressRunnable = new Runnable() {
            @Override
            public void run() {
                publishMediaUpdate();
                progressHandler.postDelayed(this, 1000);
            }
        };
        progressHandler.post(progressRunnable);
    }

    private void stopProgressUpdates() {
        if (progressRunnable != null) {
            progressHandler.removeCallbacks(progressRunnable);
            progressRunnable = null;
        }
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        if (isJioSaavnPackage(sbn.getPackageName())) {
            long now = System.currentTimeMillis();
            if (now - lastNotificationPublishTime < NOTIFICATION_THROTTLE_MS) return;
            lastNotificationPublishTime = now;

            Notification notification = sbn.getNotification();
            if (notification != null && notification.extras != null) {
                String title = notification.extras.getString(Notification.EXTRA_TITLE);
                String artist = notification.extras.getString(Notification.EXTRA_TEXT);
                
                if (title != null && !title.isEmpty()) {
                    log("Notification Extraction: " + title + " - " + artist);
                    publishMediaUpdate(title, artist, sbn.getPackageName());
                }
            }
        }
    }

    private void publishMediaUpdate() {
        publishMediaUpdate(null, null, null);
    }

    private void publishMediaUpdate(String overrideTitle, String overrideArtist, String overridePackage) {
        try {
            long now = System.currentTimeMillis();
            if (now - lastPublishTime < PUBLISH_THROTTLE_MS) return;
            lastPublishTime = now;

            MediaMetadata metadata = activeMediaController != null ? activeMediaController.getMetadata() : null;
            PlaybackState state = activeMediaController != null ? activeMediaController.getPlaybackState() : null;
            String packageName = activeMediaController != null ? activeMediaController.getPackageName() : overridePackage;

            if (packageName != null) lastKnownPackage = packageName;
            if (overrideTitle != null) lastKnownTitle = overrideTitle;
            if (overrideArtist != null) lastKnownArtist = overrideArtist;

            JSONObject payload = new JSONObject();
            payload.put("type", "media_update");
            payload.put("source_app", lastKnownPackage);

            if (metadata != null) {
                String title = null;
                String[] titleKeys = {
                    MediaMetadata.METADATA_KEY_TITLE,
                    "android.media.metadata.TITLE",
                    "title",
                    "track",
                    "song",
                    "song_name"
                };
                for (String key : titleKeys) {
                    title = metadata.getString(key);
                    if (title != null && !title.isEmpty()) break;
                }
                if (title != null) lastKnownTitle = title;

                String artist = null;
                String[] artistKeys = {
                    MediaMetadata.METADATA_KEY_ARTIST,
                    "android.media.metadata.ARTIST",
                    "artist",
                    "artist_name",
                    "singers"
                };
                for (String key : artistKeys) {
                    artist = metadata.getString(key);
                    if (artist != null && !artist.isEmpty()) break;
                }
                if (artist != null) lastKnownArtist = artist;

                String art = null;
                String[] artKeys = {
                    MediaMetadata.METADATA_KEY_ALBUM_ART_URI,
                    MediaMetadata.METADATA_KEY_ART_URI,
                    "android.media.metadata.ALBUM_ART_URI",
                    "android.media.metadata.ART_URI",
                    "album_art_uri"
                };
                for (String key : artKeys) {
                    art = metadata.getString(key);
                    if (art != null && !art.isEmpty()) break;
                }
                lastKnownAlbumArt = (art != null) ? art : "";
            }

            payload.put("title", lastKnownTitle);
            payload.put("artist", lastKnownArtist);
            payload.put("album_art", lastKnownAlbumArt);

            String status = "Paused";
            if (state != null) {
                status = (state.getState() == PlaybackState.STATE_PLAYING) ? "Playing" : "Paused";
            } else if (overrideTitle != null) {
                status = "Playing"; // If triggered by notification, assume playing
            }
            payload.put("status", status);

            // Progress and Time
            String currentTime = "0:00";
            String totalTime = "0:00";
            int progressPercent = 0;

            if (state != null && metadata != null) {
                long currentPos = state.getPosition();
                long duration = metadata.getLong(MediaMetadata.METADATA_KEY_DURATION);
                currentTime = formatTime(currentPos);
                totalTime = formatTime(duration);
                if (duration > 0) {
                    progressPercent = (int) ((currentPos * 100) / duration);
                }
            }
            payload.put("current_time", currentTime);
            payload.put("total_time", totalTime);
            payload.put("progress_percent", progressPercent);

            // Volume
            int volumePercent = 0;
            AudioManager audioManager = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            if (audioManager != null) {
                int volume = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC);
                int maxVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
                if (maxVolume > 0) {
                    volumePercent = (int) ((volume * 100.0) / maxVolume);
                }
            }
            payload.put("volume", volumePercent);

            if (MainActivity.instance != null) {
                final String fTitle = lastKnownTitle;
                final String fArtist = lastKnownArtist;
                final String fStatus = status;
                final String fCurrent = currentTime;
                final String fTotal = totalTime;
                final int fProg = progressPercent;
                final int fVol = volumePercent;
                MainActivity.instance.runOnUiThread(() -> {
                    MainActivity.instance.updateMediaDisplay(fTitle, fArtist, fStatus, fCurrent, fTotal, fProg, fVol);
                });
            }

            if (mqttClient != null && mqttClient.isConnected()) {
                MqttMessage message = new MqttMessage(payload.toString().getBytes());
                message.setQos(1);
                publishMqttAsync(getMediaTopic(), message);
                Log.d(TAG, "Media Update Published: " + lastKnownTitle);
            }
        } catch (Exception e) {
            Log.e(TAG, "Failed to update media telemetry", e);
        }
    }

    public void refreshMediaTelemetry() {
        new Thread(() -> {
            if (activeMediaController == null) {
                try {
                    MediaController controller = findJioSaavnController(1, 100, true);
                    if (controller != null) {
                        updateActiveController(controller);
                    }
                } catch (Exception ignored) {}
            }
            publishMediaUpdate();
        }).start();
    }

    private String deviceType = "android";
    private String deviceId = "";

    public void loadDeviceUsername() {
        try {
            android.content.SharedPreferences prefs = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE);
            this.brokerUrl = prefs.getString("bci_broker_url", DEFAULT_BROKER_URL);
            this.deviceUsername = prefs.getString(MainActivity.KEY_USERNAME, MainActivity.DEFAULT_USERNAME);
            this.deviceType = MainActivity.DEFAULT_DEVICE_TYPE;
            this.deviceId = prefs.getString(MainActivity.KEY_DEVICE_ID, null);
            if (this.deviceId == null || this.deviceId.trim().isEmpty()) {
                this.deviceId = java.util.UUID.randomUUID().toString();
                prefs.edit().putString(MainActivity.KEY_DEVICE_ID, this.deviceId).apply();
            }
        } catch (Exception e) {
            this.brokerUrl = DEFAULT_BROKER_URL;
            this.deviceUsername = MainActivity.DEFAULT_USERNAME;
            this.deviceType = MainActivity.DEFAULT_DEVICE_TYPE;
            if (this.deviceId == null || this.deviceId.trim().isEmpty()) {
                this.deviceId = java.util.UUID.randomUUID().toString();
            }
        }
        String displayUser = (this.deviceUsername != null && !this.deviceUsername.isEmpty()) ? this.deviceUsername : "Not Set";
        log("Device initialized: " + displayUser + " [" + this.deviceType + "] Broker: " + this.brokerUrl + " ID=" + this.deviceId.substring(0, Math.min(8, this.deviceId.length())) + "...");
    }

    public void updateDeviceUsername(String newUsername) {
        updateDeviceUsername(newUsername, null, null);
    }

    public void updateDeviceUsername(String newUsername, String newDeviceType) {
        updateDeviceUsername(newUsername, newDeviceType, null);
    }

    public void updateDeviceUsername(String newUsername, String newDeviceType, String newDeviceId) {
        boolean usernameChanged = (newUsername != null && !newUsername.trim().isEmpty() && !newUsername.equals(this.deviceUsername));
        boolean typeChanged = (newDeviceType != null && !newDeviceType.trim().isEmpty() && !newDeviceType.equals(this.deviceType));
        boolean idChanged = (newDeviceId != null && !newDeviceId.trim().isEmpty() && !newDeviceId.equals(this.deviceId));

        final String oldCommands = getCommandsTopic();
        final String oldStatus = getStatusTopic();

        if (usernameChanged && newUsername != null && !newUsername.trim().isEmpty()) {
            this.deviceUsername = newUsername.trim().toLowerCase();
        }
        if (newDeviceType != null && !newDeviceType.trim().isEmpty()) {
            this.deviceType = newDeviceType.trim().toLowerCase();
        }
        if (newDeviceId != null && !newDeviceId.trim().isEmpty()) {
            this.deviceId = newDeviceId.trim();
        }

        log("MQTT: Updated device identity to '" + this.deviceUsername + "' [" + this.deviceType + "]");

        if (mqttClient != null && mqttClient.isConnected()) {
            new Thread(() -> {
                try {
                    if (usernameChanged) {
                        if (oldCommands != null && !oldCommands.contains("//")) {
                            try { mqttClient.unsubscribe(oldCommands); } catch (Exception ignored) {}
                        }
                        if (oldStatus != null && !oldStatus.contains("//")) {
                            try { mqttClient.unsubscribe(oldStatus); } catch (Exception ignored) {}
                        }

                        mqttClient.subscribe(getCommandsTopic());
                        mqttClient.subscribe(getStatusTopic());
                        log("MQTT: Resubscribed to topics for " + deviceUsername);
                    }

                    if (this.deviceId != null && !this.deviceId.trim().isEmpty()) {
                        mqttClient.subscribe("aiml/" + this.deviceId + "/register_response");
                    }

                    publishDeviceRegistration();
                } catch (Exception e) {
                    Log.e(TAG, "Error resubscribing after device update", e);
                }
            }).start();
        }
    }

    public void publishDeviceRegistration() {
        if (mqttClient != null && mqttClient.isConnected()) {
            try {
                if (deviceId == null || deviceId.trim().isEmpty()) {
                    loadDeviceUsername();
                }
                if (deviceUsername == null || deviceUsername.trim().isEmpty()) {
                    Log.d(TAG, "Skipping device registration: username is not configured.");
                    return;
                }
                JSONObject reg = new JSONObject();
                reg.put("device_id", deviceId);
                reg.put("username", deviceUsername);
                reg.put("device_type", deviceType);

                MqttMessage msg = new MqttMessage(reg.toString().getBytes());
                msg.setQos(1);
                publishMqttAsync(DISCOVERY_TOPIC, msg);
                log("DEVICE REGISTRATION: Announced '" + deviceUsername + "' [" + deviceType + "] ID=" + deviceId.substring(0, Math.min(8, deviceId.length())) + "... on " + DISCOVERY_TOPIC);
            } catch (Exception e) {
                Log.e(TAG, "Device registration publish failed", e);
            }
        }
    }

    private String formatTime(long ms) {
        long seconds = (ms / 1000) % 60;
        long minutes = (ms / (1000 * 60));
        return String.format(java.util.Locale.US, "%d:%02d", minutes, seconds);
    }
}
