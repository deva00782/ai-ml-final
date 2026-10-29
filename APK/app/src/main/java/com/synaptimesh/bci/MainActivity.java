package com.synaptimesh.bci;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

@SuppressLint({"SetTextI18n", "SimpleDateFormat"})
@SuppressWarnings("java:S6548")
public class MainActivity extends AppCompatActivity {

    public static final String PREFS_NAME = "BciPrefs";
    public static final String KEY_USERNAME = "bci_username";
    public static final String DEFAULT_USERNAME = "";
    public static final String KEY_DEVICE_TYPE = "bci_device_type";
    public static final String DEFAULT_DEVICE_TYPE = "android";
    public static final String KEY_DEVICE_ID = "bci_device_id";
    public static final String KEY_USER_DISCONNECTED = "bci_user_disconnected";

    private static final String COLOR_AMBER = "#FCCA73";
    private static final String COLOR_MINT_GREEN = "#60E8BB";
    private static final String COLOR_MUTED_BLUE = "#9EAFC3";
    private static final String COLOR_SLATE = "#94A3B8";
    private static final String PREFIX_SEARCH = "Search: ";

    private static MainActivity instance;

    public static synchronized MainActivity getInstance() {
        return instance;
    }

    private static synchronized void setInstance(MainActivity activity) {
        instance = activity;
    }

    // Existing UI references
    private TextView tvStatus;
    private Button btnConnect;
    private Button btnDisconnect;
    private ScrollView consoleWrapper;
    private TextView tvDeviceUsername;

    // Enhanced UI references
    private TextView tvNowPlayingTitle;
    private TextView tvNowPlayingArtist;
    private TextView tvPlaybackStatus;
    private ProgressBar pbTrackProgress;
    private TextView tvTrackTimeline;
    private TextView tvVolumeLevel;
    private TextView tvPermissionsSummary;
    private Button btnPermissionNotification;
    private Button btnPermissionAccessibility;
    private boolean isConsoleExpanded = false;

    // Periodic telemetry refresh while active
    private final Handler uiRefreshHandler = new Handler(Looper.getMainLooper());
    private final Runnable uiRefreshRunnable = new Runnable() {
        @Override
        public void run() {
            if (BciMediaService.getInstance() != null) {
                BciMediaService.getInstance().refreshMediaTelemetry();
            }
            uiRefreshHandler.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        setInstance(this);

        // Bind existing views
        tvStatus = findViewById(R.id.tvStatus);
        btnConnect = findViewById(R.id.btnConnect);
        btnDisconnect = findViewById(R.id.btnDisconnect);
        TextView tvConsoleLog = findViewById(R.id.tvConsoleLog);
        consoleWrapper = findViewById(R.id.consoleWrapper);
        tvDeviceUsername = findViewById(R.id.tvDeviceUsername);
        Button btnChangeUsername = findViewById(R.id.btnChangeUsername);

        // Bind enhanced views
        TextView tvDeviceUuid = findViewById(R.id.tvDeviceUuid);
        tvNowPlayingTitle = findViewById(R.id.tvNowPlayingTitle);
        tvNowPlayingArtist = findViewById(R.id.tvNowPlayingArtist);
        tvPlaybackStatus = findViewById(R.id.tvPlaybackStatus);
        pbTrackProgress = findViewById(R.id.pbTrackProgress);
        tvTrackTimeline = findViewById(R.id.tvTrackTimeline);
        tvVolumeLevel = findViewById(R.id.tvVolumeLevel);
        Button btnOpenJioSaavn = findViewById(R.id.btnOpenJioSaavn);
        tvPermissionsSummary = findViewById(R.id.tvPermissionsSummary);
        btnPermissionNotification = findViewById(R.id.btnPermissionNotification);
        btnPermissionAccessibility = findViewById(R.id.btnPermissionAccessibility);
        Button btnClearLogs = findViewById(R.id.btnClearLogs);
        Button btnToggleConsole = findViewById(R.id.btnToggleConsole);

        // Ensure persistent unique hardware UUID exists and display it
        String deviceId = getSavedDeviceId();
        if (tvDeviceUuid != null) {
            tvDeviceUuid.setText("UUID: " + deviceId);
        }

        String currentUsername = getSavedUsername();
        String currentDeviceType = getSavedDeviceType();
        updateUsernameDisplay(currentUsername, currentDeviceType);

        android.content.SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        if (!prefs.contains(KEY_USERNAME) || currentUsername.isEmpty()) {
            promptUsernameDialog(true);
        }

        btnConnect.setEnabled(true);
        btnDisconnect.setEnabled(false);
        setupButtonListeners(btnChangeUsername, btnOpenJioSaavn, btnClearLogs, btnToggleConsole);
    }

    private void handleConnectClicked() {
        if (!isNotificationServiceEnabled()) {
            appendLog("SYSTEM WARNING: Notification Listener permission not granted.");
            startActivity(new Intent("android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS"));
            return;
        }
        if (!isAccessibilityServiceEnabled()) {
            appendLog("SYSTEM WARNING: Accessibility Service not enabled.");
            startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
            return;
        }

        getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            .edit().putBoolean(KEY_USER_DISCONNECTED, false).apply();
        updateStatusConnecting();
        appendLog("SYSTEM ACTION: Connecting to server...");

        if (BciMediaService.getInstance() != null) {
            BciMediaService.getInstance().connectMqtt();
        } else {
            appendLog("SYSTEM WARNING: BciMediaService is initializing...");
        }
    }

    private void handleDisconnectClicked() {
        getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            .edit().putBoolean(KEY_USER_DISCONNECTED, true).apply();
        updateStatusDisconnecting();
        appendLog("SYSTEM ACTION: Disconnecting from server...");

        if (BciMediaService.getInstance() != null) {
            BciMediaService.getInstance().disconnectMqtt();
        } else {
            updateStatusOffline("Service stopped");
        }
    }

    private void toggleConsoleExpansion(Button btnToggleConsole) {
        isConsoleExpanded = !isConsoleExpanded;
        ViewGroup.LayoutParams params = consoleWrapper.getLayoutParams();
        float density = getResources().getDisplayMetrics().density;
        params.height = isConsoleExpanded ? (int) (440 * density) : (int) (220 * density);
        consoleWrapper.setLayoutParams(params);
        btnToggleConsole.setText(isConsoleExpanded ? "COLLAPSE" : "EXPAND");
    }

    private void setupButtonListeners(Button btnChangeUsername, Button btnOpenJioSaavn, Button btnClearLogs, Button btnToggleConsole) {
        btnChangeUsername.setOnClickListener(v -> promptUsernameDialog(false));
        btnConnect.setOnClickListener(v -> handleConnectClicked());
        btnDisconnect.setOnClickListener(v -> handleDisconnectClicked());

        if (btnOpenJioSaavn != null) {
            btnOpenJioSaavn.setOnClickListener(v -> openJioSaavn());
        }
        if (btnPermissionNotification != null) {
            btnPermissionNotification.setOnClickListener(v -> startActivity(new Intent("android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS")));
        }
        if (btnPermissionAccessibility != null) {
            btnPermissionAccessibility.setOnClickListener(v -> startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));
        }
        if (btnClearLogs != null) {
            btnClearLogs.setOnClickListener(v -> {
                TextView tvConsole = findViewById(R.id.tvConsoleLog);
                if (tvConsole != null) tvConsole.setText("");
            });
        }
        if (btnToggleConsole != null && consoleWrapper != null) {
            btnToggleConsole.setOnClickListener(v -> toggleConsoleExpansion(btnToggleConsole));
        }
    }

    private void checkServiceConnection() {
        boolean userDisconnected = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            .getBoolean(KEY_USER_DISCONNECTED, false);

        if (BciMediaService.getInstance() != null) {
            if (BciMediaService.getInstance().isMqttConnected()) {
                updateStatusOnline();
            } else if (!userDisconnected) {
                updateStatusConnecting();
                BciMediaService.getInstance().connectMqtt();
            } else {
                updateStatusOffline("Disconnected");
            }
            BciMediaService.getInstance().refreshMediaTelemetry();
        } else {
            updateStatusOffline(null);
        }
    }

    private void showPermissionStandby(boolean notificationEnabled) {
        if (tvStatus != null) {
            String msg = notificationEnabled ? "●  STANDBY (Enable Accessibility)" : "●  STANDBY (Grant Notification Access)";
            tvStatus.setText(msg);
            tvStatus.setTextColor(Color.parseColor(COLOR_AMBER));
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        boolean notificationEnabled = isNotificationServiceEnabled();
        boolean accessibilityEnabled = isAccessibilityServiceEnabled();

        updatePermissionsUI(notificationEnabled, accessibilityEnabled);

        if (notificationEnabled && accessibilityEnabled) {
            checkServiceConnection();
        } else {
            showPermissionStandby(notificationEnabled);
        }

        uiRefreshHandler.removeCallbacks(uiRefreshRunnable);
        uiRefreshHandler.post(uiRefreshRunnable);
    }

    @Override
    protected void onPause() {
        super.onPause();
        uiRefreshHandler.removeCallbacks(uiRefreshRunnable);
    }

    private void updatePermissionsUI(boolean notificationEnabled, boolean accessibilityEnabled) {
        if (btnPermissionNotification != null) {
            if (notificationEnabled) {
                btnPermissionNotification.setText("✓  Notification Access Enabled");
                btnPermissionNotification.setTextColor(Color.parseColor(COLOR_MINT_GREEN));
            } else {
                btnPermissionNotification.setText("Grant Notification Access");
                btnPermissionNotification.setTextColor(Color.parseColor(COLOR_AMBER));
            }
        }
        if (btnPermissionAccessibility != null) {
            if (accessibilityEnabled) {
                btnPermissionAccessibility.setText("✓  Accessibility Service Enabled");
                btnPermissionAccessibility.setTextColor(Color.parseColor(COLOR_MINT_GREEN));
            } else {
                btnPermissionAccessibility.setText("Enable Accessibility");
                btnPermissionAccessibility.setTextColor(Color.parseColor(COLOR_AMBER));
            }
        }
        if (tvPermissionsSummary != null) {
            if (notificationEnabled && accessibilityEnabled) {
                tvPermissionsSummary.setText("All required phone permissions are active and verified.");
                tvPermissionsSummary.setTextColor(Color.parseColor(COLOR_MINT_GREEN));
            } else {
                tvPermissionsSummary.setText("Media access enables playback telemetry. Accessibility enables search and touch gestures.");
                tvPermissionsSummary.setTextColor(Color.parseColor(COLOR_SLATE));
            }
        }
    }

    private void updateTitleAndArtist(String title, String artist) {
        if (tvNowPlayingTitle != null) {
            tvNowPlayingTitle.setText((title != null && !title.trim().isEmpty()) ? title : "Ready when you are");
        }
        if (tvNowPlayingArtist != null) {
            tvNowPlayingArtist.setText((artist != null && !artist.trim().isEmpty()) ? artist : "Open JioSaavn and play a track");
        }
    }

    private void updatePlaybackStatusDisplay(String status) {
        if (tvPlaybackStatus == null) return;
        if ("Playing".equalsIgnoreCase(status)) {
            tvPlaybackStatus.setText("●  Playing");
            tvPlaybackStatus.setTextColor(Color.parseColor(COLOR_MINT_GREEN));
        } else if ("Paused".equalsIgnoreCase(status)) {
            tvPlaybackStatus.setText("●  Paused");
            tvPlaybackStatus.setTextColor(Color.parseColor(COLOR_AMBER));
        } else {
            tvPlaybackStatus.setText("No active session");
            tvPlaybackStatus.setTextColor(Color.parseColor(COLOR_MUTED_BLUE));
        }
    }

    private void updateTimelineAndVolume(String currentTime, String totalTime, int progressPercent, int volumePercent) {
        if (pbTrackProgress != null) {
            pbTrackProgress.setProgress(Math.max(0, Math.min(100, progressPercent)));
        }
        if (tvTrackTimeline != null) {
            String cur = (currentTime != null && !currentTime.isEmpty()) ? currentTime : "--:--";
            String tot = (totalTime != null && !totalTime.isEmpty()) ? totalTime : "--:--";
            tvTrackTimeline.setText(cur + " / " + tot);
        }
        if (tvVolumeLevel != null) {
            tvVolumeLevel.setText("Vol: " + volumePercent + "%");
        }
    }

    public void updateMediaDisplay(final String title, final String artist, final String status, final String currentTime, final String totalTime, final int progressPercent, final int volumePercent) {
        runOnUiThread(() -> {
            if (isFinishing() || isDestroyed()) return;
            updateTitleAndArtist(title, artist);
            updatePlaybackStatusDisplay(status);
            updateTimelineAndVolume(currentTime, totalTime, progressPercent, volumePercent);
        });
    }

    public void openJioSaavn() {
        if (BciMediaService.getInstance() != null) {
            BciMediaService.getInstance().launchJioSaavn();
            return;
        }
        String[] packages = {"com.jio.media.jiobeats", "com.jio.media.jiosaavn"};
        for (String pkg : packages) {
            Intent intent = getPackageManager().getLaunchIntentForPackage(pkg);
            if (intent != null) {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(intent);
                return;
            }
        }
        Toast.makeText(this, "JioSaavn is not installed", Toast.LENGTH_SHORT).show();
    }

    private boolean isAccessibilityServiceEnabled() {
        if (BciAccessibilityService.getInstance() != null) {
            return true;
        }
        int accessibilityEnabled = 0;
        try {
            accessibilityEnabled = Settings.Secure.getInt(
                getContentResolver(),
                Settings.Secure.ACCESSIBILITY_ENABLED);
        } catch (Settings.SettingNotFoundException e) {
            // ignored
        }
        if (accessibilityEnabled == 1) {
            String settingValue = Settings.Secure.getString(
                getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
            if (settingValue != null) {
                return settingValue.contains("BciAccessibilityService");
            }
        }
        return false;
    }

    public void updateStatusConnecting() {
        runOnUiThread(() -> {
            if (isFinishing() || isDestroyed()) return;
            if (tvStatus != null) {
                tvStatus.setText("●  CONNECTING...");
                tvStatus.setTextColor(Color.parseColor("#FBBF24")); // Amber/Yellow
            }
            if (btnConnect != null) btnConnect.setEnabled(false);
            if (btnDisconnect != null) btnDisconnect.setEnabled(true);
        });
    }

    public void updateStatusOnline() {
        runOnUiThread(() -> {
            if (isFinishing() || isDestroyed()) return;
            if (tvStatus != null) {
                tvStatus.setText("●  ONLINE");
                tvStatus.setTextColor(Color.parseColor(COLOR_MINT_GREEN)); // Mint Green
            }
            if (btnConnect != null) btnConnect.setEnabled(false);
            if (btnDisconnect != null) btnDisconnect.setEnabled(true);
            appendLog("STATUS: Connected. ONLINE & LISTENING.");
        });
    }

    public void updateStatusDisconnecting() {
        runOnUiThread(() -> {
            if (isFinishing() || isDestroyed()) return;
            if (tvStatus != null) {
                tvStatus.setText("●  DISCONNECTING...");
                tvStatus.setTextColor(Color.parseColor("#FBBF24")); // Amber/Yellow
            }
            if (btnConnect != null) btnConnect.setEnabled(false);
            if (btnDisconnect != null) btnDisconnect.setEnabled(false);
        });
    }

    private void applyOfflineStatusUi(String reason) {
        if (tvStatus != null) {
            String suffix = (reason != null && !reason.isEmpty()) ? " (" + reason + ")" : "";
            tvStatus.setText("●  STANDBY" + suffix);
            tvStatus.setTextColor(Color.parseColor(COLOR_SLATE));
        }
        if (btnConnect != null) btnConnect.setEnabled(true);
        if (btnDisconnect != null) btnDisconnect.setEnabled(false);
        if (reason != null && !reason.isEmpty()) {
            appendLog("STATUS: OFFLINE (" + reason + ")");
        }
    }

    public void updateStatusOffline(String reason) {
        runOnUiThread(() -> {
            if (isFinishing() || isDestroyed()) return;
            applyOfflineStatusUi(reason);
        });
    }

    public void updatePythonStatus(final boolean online) {
        if (online) {
            updateStatusOnline();
        } else {
            updateStatusOffline("Server offline");
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        uiRefreshHandler.removeCallbacks(uiRefreshRunnable);
        if (getInstance() == this) {
            setInstance(null);
        }
    }

    private boolean isSearchBciCommand(String matchCmd) {
        return matchCmd.contains("SEARCH_ALBUM") || matchCmd.contains("PLAYLIST")
                || matchCmd.contains("SEARCH_PLAYLIST") || matchCmd.contains("TYPE_QUERY")
                || matchCmd.contains("SEARCH");
    }

    private String formatBciCommand(String command, String query) {
        String matchCmd = command.toUpperCase().trim();
        if (matchCmd.contains("NEXTTRACK") || matchCmd.contains("NEXT_SONG")) return "nexttrack";
        if (matchCmd.contains("PLAY/PAUSE") || matchCmd.contains("PLAY_PAUSE")) return "play/pause";
        if (matchCmd.contains("VOLUMEUP") || matchCmd.contains("VOLUME_UP")) return "volumeup";
        if (matchCmd.contains("VOLUMEDOWN") || matchCmd.contains("VOLUME_DOWN")) return "volumedown";
        if (isSearchBciCommand(matchCmd)) {
            String searchStr = (query != null && !query.trim().isEmpty()) ? query.trim() : "Inaam";
            return PREFIX_SEARCH + searchStr;
        }
        if (matchCmd.contains("JIOSAAVN")) return "open jiosaavn";
        if (matchCmd.contains("RETURN_TO_HOME") || "DROP".equals(matchCmd)) return "return to home";
        return command.toLowerCase();
    }

    public void processIncomingBciPacket(final String command, final String query) {
        runOnUiThread(() -> {
            if (isFinishing() || isDestroyed()) return;
            String formattedCmd = formatBciCommand(command, query);
            appendLog("MQTT RECEIVED: " + formattedCmd);
            if (formattedCmd.startsWith(PREFIX_SEARCH)) {
                appendLog("Executed Search for: " + formattedCmd.substring(PREFIX_SEARCH.length()));
            }
        });
    }

    public void appendLog(final String logLine) {
        android.util.Log.d("MainActivity", logLine);
        runOnUiThread(() -> {
            if (isFinishing() || isDestroyed()) return;
            TextView tvConsole = findViewById(R.id.tvConsoleLog);
            if (tvConsole != null && consoleWrapper != null) {
                String timeStamp = java.time.LocalTime.now().format(java.time.format.DateTimeFormatter.ofPattern("HH:mm:ss"));
                CharSequence current = tvConsole.getText();
                if (current != null && current.length() > 50000) {
                    tvConsole.setText(current.subSequence(current.length() - 25000, current.length()));
                }
                tvConsole.append("[" + timeStamp + "] " + logLine + "\n");
                consoleWrapper.post(() -> {
                    if (!isFinishing() && !isDestroyed()) {
                        consoleWrapper.fullScroll(View.FOCUS_DOWN);
                    }
                });
            }
        });
    }

    public void onUsernameConflict(final String username, final String message) {
        runOnUiThread(() -> {
            if (tvStatus != null) {
                tvStatus.setText("●  NAME CONFLICT");
                tvStatus.setTextColor(Color.parseColor("#EF4444")); // Red
            }
            appendLog("REGISTRATION REJECTED: Username '" + username + "' is already registered by another device.");

            if (!isFinishing() && !isDestroyed()) {
                new androidx.appcompat.app.AlertDialog.Builder(this)
                    .setTitle("Username Already Registered")
                    .setMessage((message != null && !message.isEmpty()) ? message :
                        "The username '" + username + "' is already in use by another device.\nPlease choose a unique username so commands can reach your device.")
                    .setIcon(android.R.drawable.ic_dialog_alert)
                    .setCancelable(false)
                    .setPositiveButton("Change Username", (dialog, which) -> promptUsernameDialog(false))
                    .setNegativeButton("Disconnect", (dialog, which) -> {
                        updateStatusOffline("Name conflict");
                        if (BciMediaService.getInstance() != null) {
                            BciMediaService.getInstance().disconnectMqtt();
                        }
                    })
                    .show();
            }
        });
    }

    public void onUsernameApproved(final String username) {
        runOnUiThread(() -> {
            if (tvStatus != null) {
                tvStatus.setText("●  ONLINE");
                tvStatus.setTextColor(Color.parseColor(COLOR_MINT_GREEN)); // Mint Green
            }
            appendLog("REGISTRATION APPROVED: Device verified as '" + username + "'.");
            Toast.makeText(this, "Device verified as '" + username + "'", Toast.LENGTH_SHORT).show();
        });
    }

    public String getSavedDeviceId() {
        android.content.SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        String deviceId = prefs.getString(KEY_DEVICE_ID, null);
        if (deviceId == null || deviceId.trim().isEmpty()) {
            deviceId = java.util.UUID.randomUUID().toString();
            prefs.edit().putString(KEY_DEVICE_ID, deviceId).apply();
        }
        return deviceId;
    }

    public String getSavedUsername() {
        android.content.SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        String username = prefs.getString(KEY_USERNAME, DEFAULT_USERNAME);
        return username != null ? username.trim() : "";
    }

    public String getSavedDeviceType() {
        return DEFAULT_DEVICE_TYPE;
    }

    private void updateUsernameDisplay(String username, String deviceType) {
        if (tvDeviceUsername != null) {
            String displayUser = (username != null && !username.trim().isEmpty()) ? username : "Not Set";
            String dType = (deviceType != null && !deviceType.trim().isEmpty()) ? deviceType.toUpperCase() : "ANDROID";
            tvDeviceUsername.setText("Device ID: " + displayUser + " [" + dType + "]");
        }
    }

    public void promptUsernameDialog(boolean isInitial) {
        androidx.appcompat.app.AlertDialog.Builder builder = new androidx.appcompat.app.AlertDialog.Builder(this);
        builder.setTitle(getString(R.string.enter_device_username));

        android.widget.LinearLayout layout = new android.widget.LinearLayout(this);
        layout.setOrientation(android.widget.LinearLayout.VERTICAL);
        layout.setPadding(50, 20, 50, 20);

        final android.widget.EditText input = new android.widget.EditText(this);
        input.setInputType(android.text.InputType.TYPE_CLASS_TEXT);
        input.setHint(getString(R.string.device_username_hint));
        String saved = getSavedUsername();
        if (saved != null && !saved.trim().isEmpty()) {
            input.setText(saved);
            input.setSelection(input.getText().length());
        } else {
            input.setText("");
        }
        layout.addView(input);

        builder.setView(layout);

        builder.setPositiveButton("Save", (dialog, which) -> {
            String raw = input.getText().toString().trim().toLowerCase();
            String cleaned = raw.replaceAll("[^a-z0-9_]", "_");
            if (cleaned.isEmpty()) {
                Toast.makeText(this, "Please enter a valid device username", Toast.LENGTH_SHORT).show();
                if (isInitial) {
                    promptUsernameDialog(true);
                }
                return;
            }
            String selectedType = DEFAULT_DEVICE_TYPE;
            String deviceId = getSavedDeviceId();

            android.content.SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            prefs.edit()
                .putString(KEY_USERNAME, cleaned)
                .putString(KEY_DEVICE_TYPE, selectedType)
                .putString(KEY_DEVICE_ID, deviceId)
                .putBoolean(KEY_USER_DISCONNECTED, false)
                .apply();

            updateUsernameDisplay(cleaned, selectedType);
            TextView tvUuid = findViewById(R.id.tvDeviceUuid);
            if (tvUuid != null) {
                tvUuid.setText("UUID: " + deviceId);
            }
            appendLog("DEVICE ID: Set to '" + cleaned + "' [ANDROID] UUID=" + deviceId.substring(0, Math.min(8, deviceId.length())) + "...");

            if (BciMediaService.getInstance() != null) {
                BciMediaService.getInstance().updateDeviceUsername(cleaned, selectedType, deviceId);
            }
        });

        if (!isInitial) {
            builder.setNegativeButton("Cancel", (dialog, which) -> dialog.cancel());
        }

        builder.setCancelable(!isInitial);
        builder.show();
    }

    private boolean isNotificationServiceEnabled() {
        String flat = Settings.Secure.getString(getContentResolver(), "enabled_notification_listeners");
        if (flat != null && !flat.isEmpty()) {
            return flat.contains("BciMediaService");
        }
        return false;
    }
}
