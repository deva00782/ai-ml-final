package com.synaptimesh.bci;

import android.app.Notification;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.media.AudioManager;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.view.KeyEvent;
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
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

@SuppressWarnings("java:S6548")
public class BciMediaService extends NotificationListenerService {

    private static final String TAG = "BciMediaService";
    private static final String JIOSAAVN_PACKAGE_BEATS = "com.jio.media.jiobeats";
    private static final String JIOSAAVN_PACKAGE_SAAVN = "com.jio.media.jiosaavn";
    public static final String DEFAULT_SEARCH_QUERY = "Inaam";

    private static final String PRIMARY_BROKER_HOST = String.join(".", "52", "21", "249", "6");
    public static final String PRIMARY_BROKER_URL = "tcp://" + PRIMARY_BROKER_HOST + ":1883";
    public static final String FALLBACK_BROKER_URL = "tcp://broker.emqx.io:1883";
    public static final String DEFAULT_BROKER_URL = PRIMARY_BROKER_URL;
    private String brokerUrl = DEFAULT_BROKER_URL;
    public static final String DISCOVERY_TOPIC = "aiml/devices/register";

    private static final String TOPIC_PREFIX = "aiml/";
    private static final String KEY_STATUS = "status";
    private static final String KEY_REMOTE_STATE = "remote_state";
    private static final String STATE_STANDBY = "STANDBY";
    private static final String SUFFIX_REGISTER_RESPONSE = "/register_response";
    private static final String KEY_COMMAND = "command";
    private static final String CMD_SEARCH = "SEARCH";
    private static final String CMD_SEARCH_PLAYLIST = "Right_Search_Playlist";
    private static final String KEY_TITLE = "title";
    private static final String CMD_PLAY_PAUSE = "Right_Play_Pause";
    private static final String CMD_NEXT_SONG = "Right_Next_Song";
    private static final String CMD_PREVIOUS_SONG = "Right_Previous_Song";
    private static final String CMD_VOLUME_UP = "Right_Volume_Up";
    private static final String TIER_MEDIA_CONTROLLER = "Tier 1: MediaController";
    private static final String TIER_ACCESSIBILITY_UI = "Tier 2: Accessibility UI";
    private static final String TIER_AUDIO_MANAGER = "Tier 3: AudioManager KeyEvent";

    private String deviceUsername = "";
    private String deviceType = "android";
    private String deviceId = "";

    public String getDeviceUsername() { return deviceUsername; }
    public String getCommandsTopic() { return TOPIC_PREFIX + deviceUsername + "/commands"; }
    public String getAckTopic() { return TOPIC_PREFIX + deviceUsername + "/ack"; }
    public String getStatusTopic() { return TOPIC_PREFIX + deviceUsername + "/status"; }
    public String getMediaTopic() { return TOPIC_PREFIX + deviceUsername + "/media"; }

    private static final long PUBLISH_THROTTLE_MS = 1000;
    private static final long NOTIFICATION_THROTTLE_MS = 2000;

    private static BciMediaService instance;

    public static synchronized BciMediaService getInstance() {
        return instance;
    }

    private static synchronized void setInstance(BciMediaService service) {
        instance = service;
    }

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
    private String lastPublishedMqttTitle = null;
    private String lastPublishedMqttArtist = null;
    private String lastPublishedMqttStatus = null;

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
        setInstance(this);
        loadDeviceUsername();
        setupMediaSessionListeners();

        android.content.SharedPreferences prefs = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE);
        boolean userDisconnected = prefs.getBoolean(MainActivity.KEY_USER_DISCONNECTED, false);
        if (!userDisconnected) {
            this.isRemoteActive = true;
            connectToMqttBroker();
        } else {
            this.isRemoteActive = false;
            this.lastPublishedMqttTitle = null;
            this.lastPublishedMqttArtist = null;
            this.lastPublishedMqttStatus = null;
            Log.d(TAG, "onListenerConnected: User previously disconnected. Staying in standby.");
        }
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        setInstance(null);

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
        if (MainActivity.getInstance() != null) {
            MainActivity.getInstance().updateStatusConnecting();
        }
        connectToMqttBroker();
    }

    public void disconnectMqtt() {
        android.content.SharedPreferences prefs = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE);
        prefs.edit().putBoolean(MainActivity.KEY_USER_DISCONNECTED, true).apply();
        this.isRemoteActive = false;
        this.lastPublishedMqttTitle = null;
        this.lastPublishedMqttArtist = null;
        this.lastPublishedMqttStatus = null;
        if (MainActivity.getInstance() != null) {
            MainActivity.getInstance().updateStatusDisconnecting();
        }
        new Thread(() -> {
            if (mqttClient != null) {
                try {
                    JSONObject status = new JSONObject();
                    status.put(KEY_STATUS, "OFFLINE");
                    status.put(KEY_REMOTE_STATE, STATE_STANDBY);
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
            if (MainActivity.getInstance() != null) {
                MainActivity.getInstance().updateStatusOffline("Disconnected");
            }
        }).start();
    }

    private void connectToMqttBroker() {
        if (mqttClient != null && mqttClient.isConnected()) {
            if (MainActivity.getInstance() != null) {
                MainActivity.getInstance().updateStatusOnline();
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

                mqttClient.setCallback(createMqttCallback());
                mqttClient.connect(options);
            } catch (Exception e) {
                Log.e(TAG, "MQTT Connection Error", e);
            }
        }).start();
    }

    private MqttCallbackExtended createMqttCallback() {
        return new MqttCallbackExtended() {
            @Override
            public void connectComplete(boolean reconnect, String serverURI) {
                log("MQTT Connected! Reconnect=" + reconnect + " | URI=" + serverURI);
                if (MainActivity.getInstance() != null) {
                    MainActivity.getInstance().updateStatusOnline(); 
                }
                subscribeAfterConnect();
            }

            @Override
            public void connectionLost(Throwable cause) {
                handleMqttConnectionLost(cause);
            }

            @Override
            public void messageArrived(String topic, MqttMessage message) {
                onMessageArrivedInternal(topic, message);
            }

            @Override
            public void deliveryComplete(IMqttDeliveryToken token) {
                // Tracking delivery complete tokens is not required
            }
        };
    }

    private void subscribeAfterConnect() {
        try {
            if (deviceUsername != null && !deviceUsername.trim().isEmpty()) {
                mqttClient.subscribe(getCommandsTopic());
                mqttClient.subscribe(getStatusTopic());
                Log.d(TAG, "Subscribed to MQTT topics: " + getCommandsTopic() + ", " + getStatusTopic());
            }
            if (deviceId != null && !deviceId.trim().isEmpty()) {
                mqttClient.subscribe(TOPIC_PREFIX + deviceId + SUFFIX_REGISTER_RESPONSE);
            }
            publishDeviceRegistration();
        } catch (Exception e) {
            Log.e(TAG, "Subscription Error after connect", e);
        }
    }

    private void handleMqttConnectionLost(Throwable cause) {
        String errorMsg = (cause != null ? cause.getMessage() : "Unknown cause");
        Log.w(TAG, "MQTT Connection lost: " + errorMsg);
        if (MainActivity.getInstance() != null) {
            MainActivity.getInstance().updateStatusOffline(errorMsg);
        }

        if (isRemoteActive) {
            android.content.SharedPreferences prefs = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE);
            boolean userDisconnected = prefs.getBoolean(MainActivity.KEY_USER_DISCONNECTED, false);
            if (!userDisconnected) {
                scheduleMqttReconnect();
            }
        }
    }

    private void scheduleMqttReconnect() {
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

    private void onMessageArrivedInternal(String topic, MqttMessage message) {
        try {
            String payload = new String(message.getPayload()).trim();
            Log.d(TAG, "MQTT Message Arrived | Topic: " + topic + " | Payload: " + payload);

            JSONObject json = parseMessagePayload(payload);
            if (json == null) return;

            if (topic.equals(TOPIC_PREFIX + deviceId + SUFFIX_REGISTER_RESPONSE)) {
                handleRegistrationResponse(json);
                return;
            }

            if (topic.equals(getStatusTopic())) {
                handleStatusUpdate(topic, payload, json);
                return;
            }

            processCommandPayload(json);
        } catch (Exception e) {
            Log.e(TAG, "MQTT Packet Parse Error", e);
            log("MQTT ERROR: Failed to parse packet.");
        }
    }

    private JSONObject parseMessagePayload(String payload) throws Exception {
        if (payload.startsWith("[")) {
            JSONArray array = new JSONArray(payload);
            return (array.length() > 0) ? array.getJSONObject(array.length() - 1) : null;
        }
        return new JSONObject(payload);
    }

    private void processCommandPayload(JSONObject json) {
        String command = json.has(KEY_COMMAND) ? json.optString(KEY_COMMAND, "") : json.optString("Command", "");
        if (command.isEmpty()) return;

        String target = json.optString("target_device", "");
        if (!target.isEmpty() && !target.equalsIgnoreCase(deviceUsername)
                && !target.equalsIgnoreCase(deviceId)
                && !target.equalsIgnoreCase("mobile") && !target.equalsIgnoreCase("all") && !target.equalsIgnoreCase("android")) {
            Log.d(TAG, "Target device '" + target + "' does not match our device '" + deviceUsername + "' / '" + deviceId + "', ignoring.");
            return;
        }

        String commandId = json.optString("command_id", "");
        if (!commandId.isEmpty() && isCommandAlreadyProcessed(commandId)) {
            Log.d(TAG, "Duplicate command dropped: " + commandId + " (" + command + ")");
            return;
        }

        log("MQTT RECEIVED: " + command);
        if (!isRemoteActive) {
            log("COMMAND IGNORED: Remote is in StandBy mode. Command: " + command);
            Log.w(TAG, "Gating: Ignored command '" + command + "' because isRemoteActive=false");
            return;
        }

        String officialCommand = mapToOfficialCommand(command);
        String rawQuery = json.optString("query", "");
        String songUrl = json.optString("song_url", "");
        boolean isSearchCommand = officialCommand.equals(CMD_SEARCH_PLAYLIST) || officialCommand.equals("type_query") || officialCommand.equals(CMD_SEARCH);
        final String query = (isSearchCommand && rawQuery.trim().isEmpty() && songUrl.trim().isEmpty()) ? DEFAULT_SEARCH_QUERY : rawQuery;
        String title = json.optString(KEY_TITLE, query);

        publishAck(officialCommand);
        if (MainActivity.getInstance() != null) {
            MainActivity.getInstance().runOnUiThread(() -> MainActivity.getInstance().processIncomingBciPacket(officialCommand, query));
        }

        new Thread(() -> translateBrainwaveToMedia(officialCommand, query, songUrl, title)).start();
    }

    private void handleRegistrationResponse(JSONObject json) {
        if (json == null) return;
        String respDeviceId = json.optString("device_id", "");
        if (!respDeviceId.isEmpty() && !respDeviceId.equalsIgnoreCase(this.deviceId)) {
            Log.d(TAG, "Ignoring registration response for different device ID: " + respDeviceId);
            return;
        }

        String status = json.optString(KEY_STATUS, "");
        String username = json.optString("username", this.deviceUsername);
        String message = json.optString("message", "");
        String error = json.optString("error", "");

        if ("ERROR".equalsIgnoreCase(status) || "USERNAME_TAKEN".equalsIgnoreCase(error)) {
            log("REGISTRATION REJECTED: " + (message.isEmpty() ? "Username taken" : message));
            if (MainActivity.getInstance() != null) {
                MainActivity.getInstance().runOnUiThread(() -> MainActivity.getInstance().onUsernameConflict(username, message));
            }
        } else if ("SUCCESS".equalsIgnoreCase(status)) {
            log("REGISTRATION APPROVED: " + (message.isEmpty() ? "Verified" : message));
            if (MainActivity.getInstance() != null) {
                MainActivity.getInstance().runOnUiThread(() -> MainActivity.getInstance().onUsernameApproved(username));
            }
        }
    }

    private void handleStatusUpdate(String topic, String payload, JSONObject json) {
        if (!topic.equals(getStatusTopic())) {
            return;
        }
        String statusRaw = json.optString(KEY_STATUS, "").toUpperCase();
        Log.d(TAG, "Status Topic Received: " + topic + " | Payload: " + payload);

        if (statusRaw.equals("ONLINE")) {
            if (MainActivity.getInstance() != null) {
                MainActivity.getInstance().updatePythonStatus(true);
            }
            log("Python Pipeline Status: ONLINE");
        } else if (statusRaw.equals("OFFLINE")) {
            if (MainActivity.getInstance() != null) {
                MainActivity.getInstance().updatePythonStatus(false);
            }
            log("Python Pipeline Status: OFFLINE");
        } else {
            Log.d(TAG, "Ignoring non-Python status: " + statusRaw);
        }
    }

    private boolean isPlayPauseCommand(String cmd) {
        return cmd.equals("LEFT") || cmd.equals("PLAY") || cmd.equals("PAUSE")
                || cmd.contains("PLAY_PAUSE") || cmd.contains("PLAY/PAUSE") || cmd.contains("PLAY / PAUSE");
    }

    private boolean isNextCommand(String cmd) {
        return cmd.equals("PUSH") || cmd.equals("NEXT")
                || cmd.contains("NEXT_SONG") || cmd.contains("NEXTTRACK") || cmd.contains("NEXT_TRACK") || cmd.contains("NEXT TRACK");
    }

    private boolean isPrevCommand(String cmd) {
        return cmd.equals("PULL") || cmd.equals("PREV") || cmd.equals("PREVIOUS")
                || cmd.contains("PREVIOUS_SONG") || cmd.contains("PREVTRACK") || cmd.contains("PREV_TRACK") || cmd.contains("PREV_SONG");
    }

    private boolean isVolumeUpCommand(String cmd) {
        return cmd.equals("RIGHT+PUSH") || cmd.equals("RIGHT_PUSH")
                || cmd.contains("VOLUME_UP") || cmd.contains("VOLUMEUP") || cmd.contains("VOLUME UP") || cmd.contains("VOL_UP");
    }

    private boolean isVolumeDownCommand(String cmd) {
        return cmd.equals("RIGHT+PULL") || cmd.equals("RIGHT_PULL")
                || cmd.contains("VOLUME_DOWN") || cmd.contains("VOLUMEDOWN") || cmd.contains("VOLUME DOWN") || cmd.contains("VOL_DOWN");
    }

    private boolean isSearchCommand(String cmd) {
        return cmd.equals("RIGHT") || cmd.equals(CMD_SEARCH)
                || cmd.contains("SEARCH_PLAYLIST") || cmd.contains("PLAYLIST") || cmd.contains("TYPE_QUERY");
    }

    private boolean isCloseAppCommand(String cmd) {
        return cmd.equals("RIGHT_CLOSE_APP") || cmd.equals("CLOSE")
                || cmd.contains("CLOSE_APP") || cmd.contains("CLOSE_JIOSAAVN")
                || cmd.contains("BACK_TO_LEVEL_2") || cmd.equals("PUSH+RIGHT")
                || cmd.equals("PUSH_RIGHT") || cmd.equals("DROP")
                || cmd.equals("BACK") || cmd.equals("HOME")
                || cmd.contains("RETURN_TO_HOME") || cmd.contains("RETURN TO HOME");
    }

    private String mapToOfficialCommand(String command) {
        if (command == null) return "";
        String cmdUpper = command.toUpperCase().trim();

        if (isPlayPauseCommand(cmdUpper)) return CMD_PLAY_PAUSE;
        if (isNextCommand(cmdUpper)) return CMD_NEXT_SONG;
        if (isPrevCommand(cmdUpper)) return CMD_PREVIOUS_SONG;
        if (isVolumeUpCommand(cmdUpper)) return CMD_VOLUME_UP;
        if (isVolumeDownCommand(cmdUpper)) return "Right_Volume_Down";
        if (isSearchCommand(cmdUpper)) return CMD_SEARCH_PLAYLIST;
        if (isCloseAppCommand(cmdUpper)) return "Right_Close_App";
        if (cmdUpper.contains("JIOSAAVN") || cmdUpper.contains("OPEN_JIOSAAVN")) return "Right_JioSaavn";

        return command;
    }

    private void publishAck(String command) {
        publishAck(command, null);
    }

    private void publishAck(String command, String successfulTier) {
        if (mqttClient != null && mqttClient.isConnected()) {
            try {
                JSONObject ack = new JSONObject();
                ack.put(KEY_STATUS, "EXECUTED");
                ack.put(KEY_COMMAND, command);
                if (successfulTier != null && !successfulTier.isEmpty()) {
                    ack.put("tier", successfulTier);
                }
                ack.put("timestamp", java.time.LocalDateTime.now()
                    .format(java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss")));

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

    private void publishFailedAck(String command, String reason) {
        if (mqttClient != null && mqttClient.isConnected()) {
            try {
                JSONObject ack = new JSONObject();
                ack.put(KEY_STATUS, "FAILED");
                ack.put(KEY_COMMAND, command);
                ack.put("reason", reason);
                ack.put("timestamp", java.time.LocalDateTime.now()
                    .format(java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss")));

                MqttMessage msg = new MqttMessage(ack.toString().getBytes());
                msg.setQos(1);
                publishMqttAsync(getAckTopic(), msg);
                log("FAILED ACK published: " + command + " | Reason: " + reason);
            } catch (Exception e) {
                Log.e(TAG, "FAILED ACK publish failed", e);
            }
        }
    }

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
                    status.put(KEY_STATUS, "REMOTE_ACTIVE");
                    status.put(KEY_REMOTE_STATE, "ACTIVE");
                } else {
                    status.put(KEY_STATUS, "REMOTE_STANDBY");
                    status.put(KEY_REMOTE_STATE, STATE_STANDBY);
                }
                MqttMessage msg = new MqttMessage(status.toString().getBytes());
                msg.setQos(1);
                publishMqttAsync(getStatusTopic(), msg);
                log("Published remote status: " + (active ? "ACTIVE" : STATE_STANDBY));
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
        if (BciAccessibilityService.getActiveService() != null) {
            BciAccessibilityService.getActiveService().launchJioSaavn();
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
                fallback.setComponent(new ComponentName(JIOSAAVN_PACKAGE_BEATS, JIOSAAVN_PACKAGE_BEATS + ".MainActivity"));
                fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(fallback);
            }
        } catch (Exception e) {
            Log.e(TAG, "Error launching JioSaavn: " + e.getMessage());
        }
    }

    private void ensureJioSaavnForeground() {
        if (BciAccessibilityService.getActiveService() == null) {
            Log.w(TAG, "Accessibility Service is not enabled. Launching JioSaavn directly.");
            if (MainActivity.getInstance() != null) {
                MainActivity.getInstance().appendLog("SYSTEM WARNING: Accessibility Service is not enabled.");
            }
            launchJioSaavn();
            return;
        }

        boolean isForeground = BciAccessibilityService.getActiveService().checkJioSaavnForeground();
        if (!isForeground) {
            Log.d(TAG, "Universal Rule: JioSaavn is not in foreground. Launching directly...");
            if (MainActivity.getInstance() != null) {
                MainActivity.getInstance().appendLog("SYSTEM: JioSaavn not in foreground. Launching directly...");
            }
            BciAccessibilityService.getActiveService().launchJioSaavn();

            long start = System.currentTimeMillis();
            boolean inForeground = false;
            while ((System.currentTimeMillis() - start < 2500) && !inForeground) {
                try {
                    Thread.sleep(150);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
                inForeground = BciAccessibilityService.getActiveService().checkJioSaavnForeground();
            }
            if (inForeground) {
                Log.d(TAG, "JioSaavn package verified in foreground.");
            }
        }
    }

    private MediaController findJioSaavnController(int maxRetries, long retryDelayMs) {
        return findJioSaavnController(maxRetries, retryDelayMs, false);
    }

    private void sleepQuietly(long delayMs) {
        try {
            Thread.sleep(delayMs);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private MediaController findMatchingSession(List<MediaController> controllers) {
        if (controllers == null) return null;
        for (MediaController c : controllers) {
            if (isJioSaavnPackage(c.getPackageName())) {
                return c;
            }
        }
        return null;
    }

    private void logIfNotSilent(boolean silent, String message) {
        if (!silent) {
            log(message);
        }
    }

    private MediaController findJioSaavnController(int maxRetries, long retryDelayMs, boolean silent) {
        try {
            MediaSessionManager sessionManager =
                (MediaSessionManager) getSystemService(Context.MEDIA_SESSION_SERVICE);
            if (sessionManager == null) return null;

            ComponentName listenerComponent = new ComponentName(this, BciMediaService.class);

            for (int attempt = 1; attempt <= maxRetries; attempt++) {
                List<MediaController> controllers = sessionManager.getActiveSessions(listenerComponent);
                MediaController match = findMatchingSession(controllers);
                if (match != null) {
                    logIfNotSilent(silent, "MediaController found for JioSaavn on attempt " + attempt);
                    return match;
                }

                logIfNotSilent(silent, "JioSaavn MediaController not found. Attempt " + attempt + "/" + maxRetries + ". Retrying...");
                if (attempt < maxRetries) {
                    sleepQuietly(retryDelayMs);
                }
            }
        } catch (SecurityException se) {
            logIfNotSilent(silent, "SecurityException: Notification Listener permission missing. " + se.getMessage());
        } catch (Exception e) {
            logIfNotSilent(silent, "findJioSaavnController error: " + e.getMessage());
        }
        logIfNotSilent(silent, "JioSaavn MediaController not found after all retries. Will use fallback path.");
        return null;
    }

    private boolean launchDeeplinkIntent(Intent intent) {
        String[] packages = new String[]{JIOSAAVN_PACKAGE_BEATS, JIOSAAVN_PACKAGE_SAAVN};
        for (String pkg : packages) {
            try {
                getPackageManager().getPackageInfo(pkg, 0);
                intent.setPackage(pkg);
                startActivity(intent);
                log("Launched JioSaavn deep link via package: " + pkg);
                return true;
            } catch (Exception ignored) {
                // Ignore package not found and test next candidate
            }
        }
        return false;
    }

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
                pausePlaybackIfPlaying();

                Uri uri = Uri.parse(trimmedUrl);
                Intent intent = new Intent(Intent.ACTION_VIEW, uri);
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);

                boolean launched = launchDeeplinkIntent(intent);
                if (!launched) {
                    intent.setPackage(null);
                    startActivity(intent);
                    log("Launched deep link via general intent.");
                }

                if (BciAccessibilityService.getActiveService() != null) {
                    log("Invoking BciAccessibilityService.playSearchedSong for '" + effectiveTitle + "'");
                    BciAccessibilityService.getActiveService().playSearchedSong(effectiveTitle);
                } else {
                    log("BciAccessibilityService instance is null! Fallback to media transport controls.");
                }

                publishAck(CMD_SEARCH_PLAYLIST, "Deeplink: " + songUrl);
            } catch (Exception e) {
                log("Error opening deep link: " + e.getMessage());
                publishFailedAck(CMD_SEARCH_PLAYLIST, "Deeplink error: " + e.getMessage());
            }
        });
    }

    private String extractUrlFromSection(JSONObject parent, String key) {
        JSONObject obj = parent.optJSONObject(key);
        if (obj == null) return null;
        JSONArray data = obj.optJSONArray("data");
        if (data == null || data.length() == 0) return null;
        JSONObject first = data.optJSONObject(0);
        return (first != null && first.has("url")) ? first.optString("url") : null;
    }

    private String extractSongUrlFromJson(JSONObject json) {
        String url = extractUrlFromSection(json, "songs");
        if (url != null) return url;
        return extractUrlFromSection(json, "topquery");
    }

    private void triggerAccessibilitySearchFallback(String cleanQuery) {
        if (BciAccessibilityService.getActiveService() != null) {
            BciAccessibilityService.getActiveService().searchPlaylist(cleanQuery);
        }
    }

    private void handleSuccessfulSearchResponse(java.net.HttpURLConnection conn, String cleanQuery) throws Exception {
        StringBuilder response = new StringBuilder();
        try (java.io.BufferedReader in = new java.io.BufferedReader(new java.io.InputStreamReader(conn.getInputStream()))) {
            String inputLine;
            while ((inputLine = in.readLine()) != null) {
                response.append(inputLine);
            }
        }
        JSONObject json = new JSONObject(response.toString());
        String songUrl = extractSongUrlFromJson(json);
        if (songUrl != null && !songUrl.isEmpty()) {
            log("JioSaavn resolved: '" + cleanQuery + "' -> " + songUrl);
            openJioSaavnDeeplink(songUrl, cleanQuery);
        } else {
            log("JioSaavn search: No results found for '" + cleanQuery + "'");
            triggerAccessibilitySearchFallback(cleanQuery);
        }
    }

    private void executeSongSearchNetworkQuery(String cleanQuery) {
        java.net.HttpURLConnection conn = null;
        try {
            String encoded = java.net.URLEncoder.encode(cleanQuery, "UTF-8");
            String apiUrl = "https://www.jiosaavn.com/api.php?__call=autocomplete.get&_format=json&_marker=0&cc=in&includeMetaTags=1&query=" + encoded;

            java.net.URL url = java.net.URI.create(apiUrl).toURL();
            conn = (java.net.HttpURLConnection) url.openConnection();
            conn.setRequestMethod("GET");
            conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36");
            conn.setConnectTimeout(5000);
            conn.setReadTimeout(5000);

            if (conn.getResponseCode() == 200) {
                handleSuccessfulSearchResponse(conn, cleanQuery);
            } else {
                log("JioSaavn API HTTP error: " + conn.getResponseCode());
            }
        } catch (Exception e) {
            log("searchAndPlaySong error: " + e.getMessage());
            triggerAccessibilitySearchFallback(cleanQuery);
        } finally {
            if (conn != null) {
                try {
                    conn.disconnect();
                } catch (Exception ignored) {
                    // Ignored error on connection disconnect
                }
            }
        }
    }

    public void searchAndPlaySong(String query) {
        final String cleanQuery = (query != null && !query.trim().isEmpty()) ? query.trim() : DEFAULT_SEARCH_QUERY;
        log("Querying JioSaavn endpoint for: '" + cleanQuery + "'...");
        new Thread(() -> executeSongSearchNetworkQuery(cleanQuery)).start();
    }

    private void handleCloseApp(String officialCommand) {
        log("Close JioSaavn command received. Pausing playback and closing JioSaavn...");
        pausePlaybackIfPlaying();
        if (BciAccessibilityService.getActiveService() != null) {
            log("Closing JioSaavn via AccessibilityService...");
            BciAccessibilityService.getActiveService().closeJioSaavn();
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
    }

    private void handleSearchCommand(String query, String songUrl, String title) {
        if (songUrl != null && !songUrl.trim().isEmpty()) {
            log("Search received with deep link URL: " + songUrl + ", title: " + title);
            openJioSaavnDeeplink(songUrl, title);
            return;
        }
        String effectiveQuery = (query != null && !query.trim().isEmpty()) ? query.trim() : DEFAULT_SEARCH_QUERY;
        log("Search received with query: '" + effectiveQuery + "'. Resolving URL...");
        searchAndPlaySong(effectiveQuery);
    }

    private boolean executeMediaTier1(MediaController controller, String officialCommand, int preState, long prePosition) {
        try {
            switch (officialCommand) {
                case CMD_PLAY_PAUSE: {
                    PlaybackState state = controller.getPlaybackState();
                    boolean isPlaying = state != null && state.getState() == PlaybackState.STATE_PLAYING;
                    if (isPlaying) {
                        controller.getTransportControls().pause();
                        log("Tier 1: MediaController.pause() sent to JioSaavn.");
                    } else {
                        controller.getTransportControls().play();
                        log("Tier 1: MediaController.play() sent to JioSaavn.");
                    }
                    verifyPlaybackStateChange(officialCommand, preState, TIER_MEDIA_CONTROLLER);
                    return true;
                }
                case CMD_NEXT_SONG:
                    controller.getTransportControls().skipToNext();
                    log("Tier 1: MediaController.skipToNext() sent to JioSaavn.");
                    verifyTrackChange(officialCommand, prePosition, TIER_MEDIA_CONTROLLER);
                    return true;

                case CMD_PREVIOUS_SONG:
                    controller.getTransportControls().skipToPrevious();
                    log("Tier 1: MediaController.skipToPrevious() sent to JioSaavn.");
                    verifyTrackChange(officialCommand, prePosition, TIER_MEDIA_CONTROLLER);
                    return true;

                default:
                    log("Tier 1: Unsupported command for MediaController: " + officialCommand);
                    return false;
            }
        } catch (Exception e) {
            log("Tier 1 MediaController call failed: " + e.getMessage() + ". Falling back to Tier 2...");
            return false;
        }
    }

    private boolean executeMediaTier2(String officialCommand, int preState, long prePosition) {
        if (BciAccessibilityService.getActiveService() == null) {
            log("Tier 2: Accessibility Service not available.");
            return false;
        }
        boolean result = false;
        switch (officialCommand) {
            case CMD_PLAY_PAUSE:
                result = BciAccessibilityService.getActiveService().clickPlayPause();
                if (result) verifyPlaybackStateChange(officialCommand, preState, TIER_ACCESSIBILITY_UI);
                break;
            case CMD_NEXT_SONG:
                result = BciAccessibilityService.getActiveService().clickNextSong();
                if (result) verifyTrackChange(officialCommand, prePosition, TIER_ACCESSIBILITY_UI);
                break;
            case CMD_PREVIOUS_SONG:
                result = BciAccessibilityService.getActiveService().clickPreviousSong();
                if (result) verifyTrackChange(officialCommand, prePosition, TIER_ACCESSIBILITY_UI);
                break;
            default:
                log("Tier 2: Unsupported command for Accessibility: " + officialCommand);
                break;
        }
        return result;
    }

    private void executeMediaTier3(String officialCommand, int preState, long prePosition) {
        log("Tier 3: JioSaavn session and UI click failed. Using AudioManager.dispatchMediaKeyEvent...");
        switch (officialCommand) {
            case CMD_PLAY_PAUSE:
                dispatchMediaKeyFallback(KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE);
                verifyPlaybackStateChange(officialCommand, preState, TIER_AUDIO_MANAGER);
                break;
            case CMD_NEXT_SONG:
                dispatchMediaKeyFallback(KeyEvent.KEYCODE_MEDIA_NEXT);
                verifyTrackChange(officialCommand, prePosition, TIER_AUDIO_MANAGER);
                break;
            case CMD_PREVIOUS_SONG:
                dispatchMediaKeyFallback(KeyEvent.KEYCODE_MEDIA_PREVIOUS);
                verifyTrackChange(officialCommand, prePosition, TIER_AUDIO_MANAGER);
                break;
            default:
                log("No handler for command: " + officialCommand);
                publishFailedAck(officialCommand, "No handler found for command");
                break;
        }
    }

    private boolean handleNonPlaybackCommand(String officialCommand, String query, String songUrl, String effectiveTitle) {
        if (officialCommand.equals(CMD_VOLUME_UP) || officialCommand.equals("Right_Volume_Down")) {
            handleVolume(officialCommand);
            return true;
        }
        if (officialCommand.equals("Right_Close_App") || officialCommand.equals("Right_Return_to_Home") || officialCommand.equals("Drop")) {
            handleCloseApp(officialCommand);
            return true;
        }
        if (officialCommand.equals("Right_JioSaavn")) {
            log("JioSaavn launch command received. Ensuring foreground...");
            ensureJioSaavnForeground();
            publishAck(officialCommand);
            return true;
        }
        if (officialCommand.equals(CMD_SEARCH_PLAYLIST) || officialCommand.equals("type_query") || officialCommand.equals(CMD_SEARCH)) {
            handleSearchCommand(query, songUrl, effectiveTitle);
            return true;
        }
        return false;
    }

    private void executePlaybackTierPipeline(String officialCommand) {
        final int preState = capturePlaybackState();
        final long prePosition = captureQueuePosition();
        log("Pre-execution state capture: PlaybackState=" + preState + ", Position=" + prePosition);

        MediaController controller = findJioSaavnController(1, 0);
        if (controller == null) {
            ensureJioSaavnForeground();
            controller = findJioSaavnController(2, 300);
        }

        if (controller != null && executeMediaTier1(controller, officialCommand, preState, prePosition)) {
            return;
        }

        if (executeMediaTier2(officialCommand, preState, prePosition)) {
            return;
        }

        executeMediaTier3(officialCommand, preState, prePosition);
    }

    private void translateBrainwaveToMedia(String officialCommand, String query, String songUrl, String title) {
        if (officialCommand == null || officialCommand.trim().isEmpty()) return;

        final String effectiveTitle = (title != null && !title.trim().isEmpty()) ? title.trim() : query;
        log("Translating command: " + officialCommand + (query.isEmpty() ? "" : " with query: " + query));

        if (handleNonPlaybackCommand(officialCommand, query, songUrl, effectiveTitle)) {
            return;
        }

        executePlaybackTierPipeline(officialCommand);
    }

    private void verifyPlaybackStateChange(String command, int preState, String tierName) {
        sleepQuietly(500);

        int postState = capturePlaybackState();
        log("Post-execution verification: PreState=" + preState + ", PostState=" + postState);

        if (preState == -1 || postState == -1) {
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

    private void verifyTrackChange(String command, long prePosition, String tierName) {
        sleepQuietly(500);

        long postPosition = captureQueuePosition();
        log("Post-execution verification: PrePosition=" + prePosition + ", PostPosition=" + postPosition);

        if (prePosition == -1 || postPosition == -1) {
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

    private boolean executeVolumeTier1(String officialCommand, int initialVolume) {
        log("Volume Control: Attempting Tier 1 (JioSaavn UI Volume Slider)...");
        if (tryAccessibilityVolume(officialCommand)) {
            if (verifyVolumeChange(officialCommand, initialVolume, "Tier 1: JioSaavn UI Volume Slider")) {
                return true;
            }
            log("Volume Control: Tier 1 executed but volume unchanged. Falling back to Tier 2...");
        } else {
            log("Volume Control: Tier 1 FAILED. Falling back to Tier 2...");
        }
        return false;
    }

    private boolean executeVolumeTier2(String officialCommand, int initialVolume, boolean isVolumeUp) {
        log("Volume Control: Attempting Tier 2 (MediaController Direct KeyEvent)...");
        MediaController controller = findJioSaavnController(1, 0);
        if (controller != null) {
            try {
                int keyCode = isVolumeUp ? KeyEvent.KEYCODE_VOLUME_UP : KeyEvent.KEYCODE_VOLUME_DOWN;
                controller.dispatchMediaButtonEvent(new KeyEvent(KeyEvent.ACTION_DOWN, keyCode));
                controller.dispatchMediaButtonEvent(new KeyEvent(KeyEvent.ACTION_UP, keyCode));
                log("Volume Control: MediaController KeyEvent dispatched.");
                if (verifyVolumeChange(officialCommand, initialVolume, "Tier 2: MediaController Direct KeyEvent")) {
                    return true;
                }
                log("Volume Control: Tier 2 executed but volume unchanged. Falling back to Tier 3...");
            } catch (Exception e) {
                log("Volume Control: Tier 2 Exception: " + e.getMessage());
            }
        } else {
            log("Volume Control: Tier 2 FAILED (no controller). Falling back to Tier 3...");
        }
        return false;
    }

    private boolean executeVolumeTier3(String officialCommand, int initialVolume, boolean isVolumeUp) {
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
                    return true;
                }
            } catch (Exception e) {
                log("Volume Control: Tier 3 Exception: " + e.getMessage());
            }
        }
        return false;
    }

    private void handleVolume(String officialCommand) {
        log("handleVolume invoked for: " + officialCommand);
        boolean isVolumeUp = officialCommand.equals(CMD_VOLUME_UP);

        AudioManager amCapture = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        int initialVolume = (amCapture != null) ? amCapture.getStreamVolume(AudioManager.STREAM_MUSIC) : -1;
        log("Volume Pre-Capture: initialVolume=" + initialVolume);

        if (executeVolumeTier1(officialCommand, initialVolume)) {
            return;
        }

        initialVolume = (amCapture != null) ? amCapture.getStreamVolume(AudioManager.STREAM_MUSIC) : -1;
        if (executeVolumeTier2(officialCommand, initialVolume, isVolumeUp)) {
            return;
        }

        initialVolume = (amCapture != null) ? amCapture.getStreamVolume(AudioManager.STREAM_MUSIC) : -1;
        if (executeVolumeTier3(officialCommand, initialVolume, isVolumeUp)) {
            return;
        }

        publishFailedAck(officialCommand, "All volume tiers exhausted without success");
    }

    private boolean verifyVolumeChange(String command, int initialVolume, String tierName) {
        sleepQuietly(400);

        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        if (am == null) {
            log("Volume verification: AudioManager null. Assuming success.");
            publishAck(command, tierName);
            return true;
        }

        int postVolume = am.getStreamVolume(AudioManager.STREAM_MUSIC);
        log("Volume Post-Verification: initial=" + initialVolume + ", post=" + postVolume);

        if (initialVolume == -1) {
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

    private boolean tryAccessibilityVolume(String command) {
        if (BciAccessibilityService.getActiveService() == null) {
            log("Tier 1: AccessibilityService instance is null/disabled.");
            return false;
        }
        try {
            boolean isVolumeUp = command.equals(CMD_VOLUME_UP);
            boolean result = BciAccessibilityService.getActiveService().adjustJioSaavnVolumeUi(isVolumeUp);
            log("Tier 1: Accessibility UI volume slider adjust returned = " + result);
            return result;
        } catch (Exception e) {
            log("Tier 1 AccessibilityService UI volume failed: " + e.getMessage());
            return false;
        }
    }

    private void log(String message) {
        Log.d(TAG, message);
        if (MainActivity.getInstance() != null) {
            MainActivity.getInstance().appendLog(message);
        }
    }

    private void dispatchMediaKeyFallback(int keyCode) {
        try {
            AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            if (am == null) {
                log("AudioManager null in dispatchMediaKeyFallback");
                return;
            }
            am.dispatchMediaKeyEvent(new KeyEvent(KeyEvent.ACTION_DOWN, keyCode));
            am.dispatchMediaKeyEvent(new KeyEvent(KeyEvent.ACTION_UP, keyCode));
            log("dispatchMediaKeyFallback: dispatched keycode=" + keyCode);
        } catch (Exception e) {
            log("dispatchMediaKeyFallback failed: " + e.getMessage());
        }
    }

    private boolean isJioSaavnPackage(String pkg) {
        return JIOSAAVN_PACKAGE_BEATS.equals(pkg) || JIOSAAVN_PACKAGE_SAAVN.equals(pkg);
    }

    private void logMetadataKeys(MediaMetadata metadata) {
        if (metadata == null) return;
        log("MediaMetadata Changed. Keys: " + metadata.keySet().toString());
        for (String key : metadata.keySet()) {
            try {
                Object val = metadata.getString(key);
                if (val == null) val = metadata.getLong(key);
                Log.d(TAG, "Metadata: " + key + " = " + val);
            } catch (Exception ignored) {
                // Ignored non-string non-long metadata value
            }
        }
    }

    private MediaController.Callback createMediaCallback() {
        return new MediaController.Callback() {
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
                logMetadataKeys(metadata);
                publishMediaUpdate();
            }
        };
    }

    private MediaSessionManager.OnActiveSessionsChangedListener createSessionsChangedListener() {
        return controllers -> {
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
    }

    private void setupMediaSessionListeners() {
        mediaCallback = createMediaCallback();
        sessionsChangedListener = createSessionsChangedListener();

        MediaSessionManager sessionManager = (MediaSessionManager) getSystemService(Context.MEDIA_SESSION_SERVICE);
        if (sessionManager != null) {
            ComponentName listenerComponent = new ComponentName(this, BciMediaService.class);
            sessionManager.addOnActiveSessionsChangedListener(sessionsChangedListener, listenerComponent);
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

    public void publishMediaUpdate() {
        publishMediaUpdate(null, null, null);
    }

    private void extractMetadataFields(MediaMetadata metadata) {
        String[] titleKeys = {
            MediaMetadata.METADATA_KEY_TITLE, "android.media.metadata.TITLE", KEY_TITLE, "track", "song", "song_name"
        };
        for (String key : titleKeys) {
            String title = metadata.getString(key);
            if (title != null && !title.isEmpty()) {
                lastKnownTitle = title;
                break;
            }
        }

        String[] artistKeys = {
            MediaMetadata.METADATA_KEY_ARTIST, "android.media.metadata.ARTIST", "artist", "artist_name", "singers"
        };
        for (String key : artistKeys) {
            String artist = metadata.getString(key);
            if (artist != null && !artist.isEmpty()) {
                lastKnownArtist = artist;
                break;
            }
        }

        String[] artKeys = {
            MediaMetadata.METADATA_KEY_ALBUM_ART_URI, MediaMetadata.METADATA_KEY_ART_URI,
            "android.media.metadata.ALBUM_ART_URI", "android.media.metadata.ART_URI", "album_art_uri"
        };
        for (String key : artKeys) {
            String art = metadata.getString(key);
            if (art != null && !art.isEmpty()) {
                lastKnownAlbumArt = art;
                break;
            }
        }
    }

    private void extractProgressFields(PlaybackState state, MediaMetadata metadata, JSONObject payload) throws Exception {
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
    }

    private void extractVolumeField(JSONObject payload) throws Exception {
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
                extractMetadataFields(metadata);
            }

            payload.put(KEY_TITLE, lastKnownTitle);
            payload.put("artist", lastKnownArtist);
            payload.put("album_art", lastKnownAlbumArt);

            String status = "Paused";
            if (state != null) {
                status = (state.getState() == PlaybackState.STATE_PLAYING) ? "Playing" : "Paused";
            } else if (overrideTitle != null) {
                status = "Playing";
            }
            payload.put(KEY_STATUS, status);

            extractProgressFields(state, metadata, payload);
            extractVolumeField(payload);

            updateUiDisplay(status, payload.optString("current_time"), payload.optString("total_time"),
                    payload.optInt("progress_percent"), payload.optInt("volume"));

            checkAndPublishMqttMedia(status, payload);
        } catch (Exception e) {
            Log.e(TAG, "Failed to update media telemetry", e);
        }
    }

    private void updateUiDisplay(String status, String currentTime, String totalTime, int progressPercent, int volumePercent) {
        if (MainActivity.getInstance() != null) {
            final String fTitle = lastKnownTitle;
            final String fArtist = lastKnownArtist;
            final String fStatus = status;
            final String fCurrent = currentTime;
            final String fTotal = totalTime;
            final int fProg = progressPercent;
            final int fVol = volumePercent;
            MainActivity.getInstance().runOnUiThread(() ->
                MainActivity.getInstance().updateMediaDisplay(fTitle, fArtist, fStatus, fCurrent, fTotal, fProg, fVol)
            );
        }
    }

    private void checkAndPublishMqttMedia(String status, JSONObject payload) {
        boolean mediaStateChanged = !java.util.Objects.equals(lastPublishedMqttTitle, lastKnownTitle)
                || !java.util.Objects.equals(lastPublishedMqttArtist, lastKnownArtist)
                || !java.util.Objects.equals(lastPublishedMqttStatus, status);

        if (mediaStateChanged && mqttClient != null && mqttClient.isConnected()) {
            lastPublishedMqttTitle = lastKnownTitle;
            lastPublishedMqttArtist = lastKnownArtist;
            lastPublishedMqttStatus = status;

            MqttMessage message = new MqttMessage(payload.toString().getBytes());
            message.setQos(1);
            publishMqttAsync(getMediaTopic(), message);
            Log.d(TAG, "Media Update Published: " + lastKnownTitle);
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
                } catch (Exception ignored) {
                    // Ignored controller acquisition failure
                }
            }
            publishMediaUpdate();
        }).start();
    }

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

    private void unsubscribeQuietly(String topic) {
        if (mqttClient != null && topic != null && !topic.contains("//")) {
            try {
                mqttClient.unsubscribe(topic);
            } catch (Exception ignored) {
                // Ignored unsubscribe error
            }
        }
    }

    private void resubscribeDeviceTopics(boolean usernameChanged, String oldCommands, String oldStatus) {
        try {
            if (usernameChanged) {
                unsubscribeQuietly(oldCommands);
                unsubscribeQuietly(oldStatus);

                mqttClient.subscribe(getCommandsTopic());
                mqttClient.subscribe(getStatusTopic());
                log("MQTT: Resubscribed to topics for " + deviceUsername);
            }

            if (this.deviceId != null && !this.deviceId.trim().isEmpty()) {
                mqttClient.subscribe(TOPIC_PREFIX + this.deviceId + SUFFIX_REGISTER_RESPONSE);
            }

            publishDeviceRegistration();
        } catch (Exception e) {
            Log.e(TAG, "Error resubscribing after device update", e);
        }
    }

    public void updateDeviceUsername(String newUsername, String newDeviceType, String newDeviceId) {
        boolean usernameChanged = (newUsername != null && !newUsername.trim().isEmpty() && !newUsername.equals(this.deviceUsername));

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
            new Thread(() -> resubscribeDeviceTopics(usernameChanged, oldCommands, oldStatus)).start();
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
