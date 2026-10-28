package com.synaptimesh.bci;

import android.accessibilityservice.AccessibilityService;
import android.content.Context;
import android.content.Intent;
import android.media.AudioManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

public class BciAccessibilityService extends AccessibilityService {

    private static final String TAG = "BciAccessibilityService";
    private static final String JIOSAAVN_PACKAGE_BEATS = "com.jio.media.jiobeats";
    private static final String JIOSAAVN_PACKAGE_SAAVN = "com.jio.media.jiosaavn";
    private static final String KEYWORD_SEARCH = "search";
    private static final String CLASS_EDIT_TEXT = "EditText";
    
    private static BciAccessibilityService instance;

    public static synchronized BciAccessibilityService getInstance() {
        return instance;
    }

    private static synchronized void setInstance(BciAccessibilityService service) {
        instance = service;
    }

    private String currentPackageName = "";

    private final Handler searchHandler = new Handler(Looper.getMainLooper());
    private final Object searchLock = new Object();
    private String pendingSearchQuery = "";

    private final Runnable searchRunnable = () -> {
        final String queryToExecute = pendingSearchQuery;
        new Thread(() -> {
            synchronized (searchLock) {
                executeSearchSequence(queryToExecute);
            }
        }).start();
    };

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        setInstance(this);
        log("Accessibility Service connected successfully.");
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event.getEventType() == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            CharSequence pkg = event.getPackageName();
            if (pkg != null) {
                currentPackageName = pkg.toString();
            }
        }
    }

    @Override
    public void onInterrupt() {
        log("Accessibility Service interrupted.");
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        if (searchHandler != null) {
            searchHandler.removeCallbacksAndMessages(null);
        }
        setInstance(null);
    }

    public boolean checkJioSaavnForeground() {
        if (isJioSaavnPackage(currentPackageName)) {
            return true;
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root != null) {
            CharSequence pkg = root.getPackageName();
            if (pkg != null && isJioSaavnPackage(pkg.toString())) {
                return true;
            }
        }
        return false;
    }

    private boolean isJioSaavnPackage(String pkg) {
        return JIOSAAVN_PACKAGE_BEATS.equals(pkg) || JIOSAAVN_PACKAGE_SAAVN.equals(pkg);
    }

    public void launchJioSaavn() {
        try {
            Intent intent = getPackageManager().getLaunchIntentForPackage(JIOSAAVN_PACKAGE_BEATS);
            if (intent == null) {
                intent = getPackageManager().getLaunchIntentForPackage(JIOSAAVN_PACKAGE_SAAVN);
            }
            if (intent != null) {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                startActivity(intent);
                log("JioSaavn launch intent sent successfully.");
            } else {
                log("getLaunchIntentForPackage returned null, trying fallback component launch...");
                Intent fallback = new Intent(Intent.ACTION_MAIN);
                fallback.setComponent(new android.content.ComponentName(JIOSAAVN_PACKAGE_BEATS, JIOSAAVN_PACKAGE_BEATS + ".MainActivity"));
                fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                startActivity(fallback);
                log("Fallback component launch intent sent.");
            }
        } catch (Exception e) {
            log("Error launching JioSaavn: " + e.getMessage());
        }
    }

    public void closeJioSaavn() {
        new Thread(() -> {
            try {
                log("Executing close JioSaavn sequence...");
                // 1. Pause playback
                if (BciMediaService.getInstance() != null) {
                    BciMediaService.getInstance().pausePlaybackIfPlaying();
                }

                // 2. Perform global HOME action to minimize JioSaavn and return to Android home screen
                boolean homeSuccess = performGlobalAction(GLOBAL_ACTION_HOME);
                log("Global HOME action executed: " + homeSuccess);

                // 3. Fallback: if HOME action is unavailable or fails, send home intent
                if (!homeSuccess) {
                    Intent homeIntent = new Intent(Intent.ACTION_MAIN);
                    homeIntent.addCategory(Intent.CATEGORY_HOME);
                    homeIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(homeIntent);
                    log("Fallback HOME intent sent.");
                }
            } catch (Exception e) {
                log("Exception during close JioSaavn: " + e.getMessage());
            }
        }).start();
    }

    private boolean waitForJioSaavnForeground(int timeoutMs) throws InterruptedException {
        long startTime = System.currentTimeMillis();
        while (System.currentTimeMillis() - startTime < timeoutMs) {
            if (checkJioSaavnForeground()) {
                return true;
            }
            Thread.sleep(250);
        }
        return false;
    }

    /**
     * Unified entry point for typing and backspacing.
     * Optimized for responsiveness: 250ms delay for typing to avoid jitter, 0ms for clear.
     */
    public void handleSearchUpdate(String query) {
        pendingSearchQuery = (query != null) ? query : "";
        searchHandler.removeCallbacks(searchRunnable);
        
        if (pendingSearchQuery.isEmpty()) {
            log("BciSearch: Empty query received. Executing immediate clear...");
            // Execute on background thread immediately
            new Thread(searchRunnable).start();
        } else {
            // Increased to 250ms to prevent "backspacing/flickering" jitter during rapid typing
            searchHandler.postDelayed(searchRunnable, 250);
        }
    }

    private AccessibilityNodeInfo resolveSearchEditText(AccessibilityNodeInfo root) throws InterruptedException {
        if (root == null) return null;
        AccessibilityNodeInfo searchEditText = findSearchEditText(root);
        if (searchEditText != null) return searchEditText;

        log("BciSearch: [Step 1] Search field not visible. Finding Search button...");
        AccessibilityNodeInfo searchButton = null;
        for (int i = 0; i < 3; i++) {
            AccessibilityNodeInfo currentRoot = getRootInActiveWindow();
            if (currentRoot != null) {
                searchButton = findSearchButton(currentRoot);
                if (searchButton != null) break;
            }
            Thread.sleep(500);
        }
        if (searchButton == null) {
            log("BciSearch: [ERROR] Search button not found.");
            return null;
        }
        performClick(searchButton);
        return waitForSearchEditText(5000);
    }

    private void executeSearchSequence(String playlistName) {
        try {
            if (playlistName.isEmpty()) {
                clearSearchInternal();
                return;
            }

            log("BciSearch: Syncing query: '" + playlistName + "'");
            if (!checkJioSaavnForeground()) {
                ensureJioSaavnForeground();
            }

            AccessibilityNodeInfo searchEditText = resolveSearchEditText(getRootInActiveWindow());
            if (searchEditText == null) {
                log("BciSearch: [ERROR] Search field not found.");
                return;
            }

            insertTextFast(searchEditText, playlistName);
            log("BciSearch: SUCCESS - Query synced.");
            Thread.sleep(600);
            triggerSearch(searchEditText, playlistName);
            playSearchedSong(playlistName);

        } catch (InterruptedException e) {
            log("BciSearch: [INTERRUPTED] " + e.getMessage());
            Thread.currentThread().interrupt();
        } catch (Exception e) {
            log("BciSearch: [EXCEPTION] " + e.getMessage());
        }
    }

    public void searchPlaylist(final String playlistName) {
        handleSearchUpdate(playlistName);
    }

    private void clearSearchInternal() {
        try {
            ensureJioSaavnForeground();
            
            AccessibilityNodeInfo searchEditText = null;
            // Retry finding the search field a few times
            for (int i = 0; i < 3; i++) {
                AccessibilityNodeInfo root = getRootInActiveWindow();
                if (root != null) {
                    searchEditText = findSearchEditText(root);
                    if (searchEditText != null) break;
                    
                    // If not found, maybe we need to click the search button again
                    AccessibilityNodeInfo searchBtn = findSearchButton(root);
                    if (searchBtn != null) {
                        performClick(searchBtn);
                        Thread.sleep(250);
                    }
                }
                Thread.sleep(150);
            }

            if (searchEditText != null) {
                clearSearchField(searchEditText);
            } else {
                log("BciSearch: [ERROR] Could not find search field to clear after retries.");
            }
        } catch (InterruptedException e) {
            log("Interrupted in clearSearchInternal: " + e.getMessage());
            Thread.currentThread().interrupt();
        } catch (Exception e) {
            log("Error in clearSearchInternal: " + e.getMessage());
        }
    }

    private void clearSearchField(AccessibilityNodeInfo searchEditText) {
        if (searchEditText == null) return;
        
        try {
            // Try 1: Find and click the 'X' / Clear button if visible
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (root != null) {
                AccessibilityNodeInfo clearBtn = findClearButton(root);
                if (clearBtn != null) {
                    log("Found clear 'X' button. Clicking it...");
                    performClick(clearBtn);
                    Thread.sleep(300);
                    if (isSearchFieldEmpty(searchEditText)) return;
                    log("Clear button didn't fully wipe field. Trying SET_TEXT...");
                }
            }

            // Try 2: ACTION_SET_TEXT to empty string
            Bundle arguments = new Bundle();
            arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "");
            searchEditText.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments);
            Thread.sleep(200);
            if (isSearchFieldEmpty(searchEditText)) {
                log("Cleared search field via ACTION_SET_TEXT.");
                return;
            }

            // Try 3: Aggressive Selection + Cut
            log("ACTION_SET_TEXT failed. Attempting Selection + Cut...");
            if (performSelectionAndCut(searchEditText)) {
                log("Cleared search field via Selection + Cut.");
                return;
            }

            // Try 4: Brute-force Backspace Loop (for stubborn single characters like 'h')
            log("Still not empty. Starting Brute-force Backspace Loop...");
            int safetyLimit = 50; 
            while (!isSearchFieldEmpty(searchEditText) && safetyLimit-- > 0) {
                searchEditText.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
                // Move cursor to end if possible
                Bundle moveArgs = new Bundle();
                moveArgs.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, 100); 
                moveArgs.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, 100);
                searchEditText.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, moveArgs);
                
                // Perform a physical backspace if the device supports it via Global Actions
                // but since we can't reliably do that without root/shell, we try ACTION_SET_TEXT "" again after focus
                searchEditText.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments);
                Thread.sleep(100);
            }

            if (isSearchFieldEmpty(searchEditText)) {
                log("BciSearch: Field finally cleared via Brute-force.");
            } else {
                log("BciSearch: [WARNING] Field still not empty. Content: '" + searchEditText.getText() + "'");
            }
        } catch (InterruptedException e) {
            log("Interrupted in clearSearchField: " + e.getMessage());
            Thread.currentThread().interrupt();
        } catch (Exception e) {
            log("Error clearing search field: " + e.getMessage());
        }
    }

    private boolean performSelectionAndCut(AccessibilityNodeInfo node) {
        CharSequence currentText = node.getText();
        int length = (currentText != null) ? currentText.length() : 0;
        if (length > 0) {
            Bundle selectionArgs = new Bundle();
            selectionArgs.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, 0);
            selectionArgs.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, length);
            node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, selectionArgs);
            return node.performAction(AccessibilityNodeInfo.ACTION_CUT);
        }
        return true;
    }

    private AccessibilityNodeInfo findClearButton(AccessibilityNodeInfo node) {
        if (node == null) return null;
        
        CharSequence desc = node.getContentDescription();
        if (desc != null) {
            String d = desc.toString().toLowerCase();
            if (d.contains("clear") || d.equals("x") || d.contains("cancel") || d.contains("delete query")) {
                return node;
            }
        }

        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findClearButton(child);
            if (result != null) return result;
        }
        return null;
    }

    private boolean isSearchFieldEmpty(AccessibilityNodeInfo node) {
        if (node == null) return true;
        CharSequence text = node.getText();
        return text == null || text.toString().trim().isEmpty();
    }

    private void ensureJioSaavnForeground() {
        if (!checkJioSaavnForeground()) {
            launchJioSaavn();
            try {
                waitForJioSaavnForeground(10000);
                Thread.sleep(400);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    private AccessibilityNodeInfo findSearchButton(AccessibilityNodeInfo node) {
        if (node == null) return null;

        // Priority 1: contentDescription
        CharSequence contentDesc = node.getContentDescription();
        if (contentDesc != null) {
            String desc = contentDesc.toString().toLowerCase().trim();
            if (desc.contains(KEYWORD_SEARCH) || desc.contains("explore")) {
                return node;
            }
        }

        // Priority 2: text
        CharSequence text = node.getText();
        if (text != null) {
            String txt = text.toString().toLowerCase().trim();
            if (txt.contains(KEYWORD_SEARCH) || txt.contains("explore")) {
                return node;
            }
        }

        // Priority 3: Traverse children
        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findSearchButton(child);
            if (result != null) {
                return result;
            }
        }
        return null;
    }

    private boolean performClick(AccessibilityNodeInfo node) {
        if (node == null) return false;
        AccessibilityNodeInfo target = node;
        while (target != null) {
            if (target.isClickable()) {
                return target.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            }
            target = target.getParent();
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_CLICK);
    }

    private AccessibilityNodeInfo waitForSearchEditText(int timeoutMs) throws InterruptedException {
        long startTime = System.currentTimeMillis();
        boolean placeholderClicked = false;
        while (System.currentTimeMillis() - startTime < timeoutMs) {
            AccessibilityNodeInfo rootNode = getRootInActiveWindow();
            if (rootNode != null) {
                AccessibilityNodeInfo editText = findSearchEditText(rootNode);
                if (editText != null) {
                    return editText;
                }
                
                if (!placeholderClicked) {
                    AccessibilityNodeInfo placeholder = findSearchPlaceholder(rootNode);
                    if (placeholder != null) {
                        log("Found a search placeholder/bar. Clicking it to reveal the EditText...");
                        performClick(placeholder);
                        placeholderClicked = true;
                        Thread.sleep(350);
                        continue;
                    }
                }
            }
            Thread.sleep(250);
        }
        return null;
    }

    private AccessibilityNodeInfo findSearchEditText(AccessibilityNodeInfo node) {
        if (node == null) return null;

        String className = node.getClassName() != null ? node.getClassName().toString() : "";
        boolean isEditField = className.contains(CLASS_EDIT_TEXT) || className.contains("android.widget." + CLASS_EDIT_TEXT) || node.isEditable();

        if (isEditField) {
            return node;
        }

        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findSearchEditText(child);
            if (result != null) {
                return result;
            }
        }
        return null;
    }

    private AccessibilityNodeInfo findSearchPlaceholder(AccessibilityNodeInfo node) {
        if (node == null) return null;

        CharSequence desc = node.getContentDescription();
        if (desc != null) {
            String descStr = desc.toString().toLowerCase();
            if (descStr.contains("search songs") || descStr.contains("search music") || descStr.equals(KEYWORD_SEARCH)) {
                return node;
            }
        }

        CharSequence text = node.getText();
        if (text != null) {
            String textStr = text.toString().toLowerCase();
            if (textStr.contains("search songs") || textStr.contains("search music") || textStr.equals(KEYWORD_SEARCH)) {
                return node;
            }
        }

        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findSearchPlaceholder(child);
            if (result != null) {
                return result;
            }
        }
        return null;
    }

    private boolean insertTextFast(AccessibilityNodeInfo editText, String text) {
        if (editText == null) return false;
        Bundle arguments = new Bundle();
        arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        return editText.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments);
    }

    private void triggerSearch(AccessibilityNodeInfo editText, String playlistName) {
        AccessibilityNodeInfo rootNode = getRootInActiveWindow();
        if (rootNode != null) {
            // Priority 1: Check for screen Search trigger button
            AccessibilityNodeInfo searchTrigger = findSearchTriggerButton(rootNode);
            if (searchTrigger != null) {
                log("Found clickable screen search button. Clicking it...");
                performClick(searchTrigger);
                return;
            }

            // Priority 2: Look for suggestions list and click the first suggestion
            AccessibilityNodeInfo suggestion = findFirstSearchSuggestion(rootNode, playlistName);
            if (suggestion != null) {
                log("Found search suggestion. Clicking it...");
                performClick(suggestion);
                return;
            }
        }

        // Priority 3: Fallback click on EditText to trigger default input IME/submit
        log("No specific search button or suggestion found. Attempting IME search click fallback...");
        editText.performAction(AccessibilityNodeInfo.ACTION_CLICK);

        // Priority 4: Try a global Enter action if possible (Android 7.0+)
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.N) {
            log("Attempting Global Action Enter via KeyEvents...");
            // Since we can't easily inject specific KeyEvents without instrumentation, 
            // we rely on the above clicks. But we can try to focus and then perform ACTION_CLICK again.
        }
    }

    private AccessibilityNodeInfo findSearchTriggerButton(AccessibilityNodeInfo node) {
        if (node == null) return null;

        String className = node.getClassName() != null ? node.getClassName().toString() : "";
        boolean isEditText = className.contains(CLASS_EDIT_TEXT) || className.contains("android.widget." + CLASS_EDIT_TEXT) || node.isEditable();

        if (!isEditText) {
            CharSequence desc = node.getContentDescription();
            if (desc != null && desc.toString().toLowerCase().trim().equals(KEYWORD_SEARCH)) {
                return node;
            }
            CharSequence text = node.getText();
            if (text != null && text.toString().toLowerCase().trim().equals(KEYWORD_SEARCH)) {
                return node;
            }
        }

        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findSearchTriggerButton(child);
            if (result != null) {
                return result;
            }
        }
        return null;
    }

    private AccessibilityNodeInfo findClickableTextMatch(AccessibilityNodeInfo node, String playlistName) {
        CharSequence text = node.getText();
        if (text != null && text.toString().toLowerCase().contains(playlistName.toLowerCase())) {
            if (node.isClickable()) return node;
            AccessibilityNodeInfo parent = node.getParent();
            while (parent != null) {
                if (parent.isClickable()) return parent;
                parent = parent.getParent();
            }
        }
        return null;
    }

    private AccessibilityNodeInfo findListChildSuggestion(AccessibilityNodeInfo node) {
        String className = node.getClassName() != null ? node.getClassName().toString() : "";
        if (className.contains("RecyclerView") || className.contains("ListView")) {
            for (int i = 0; i < node.getChildCount(); i++) {
                AccessibilityNodeInfo child = node.getChild(i);
                if (child != null && child.isClickable()) return child;
            }
        }
        return null;
    }

    private AccessibilityNodeInfo findFirstSearchSuggestion(AccessibilityNodeInfo node, String playlistName) {
        if (node == null) return null;
        AccessibilityNodeInfo match = findClickableTextMatch(node, playlistName);
        if (match != null) return match;
        AccessibilityNodeInfo listMatch = findListChildSuggestion(node);
        if (listMatch != null) return listMatch;

        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo result = findFirstSearchSuggestion(node.getChild(i), playlistName);
            if (result != null) return result;
        }
        return null;
    }

    /**
     * Executes the 3-Step JioSaavn Volume UI Navigation:
     * Step 1: If on Home Screen mini-player, click mini-player bar to expand Full-Screen Player.
     * Step 2: Find and click the bottom-left Speaker Icon button [ 🔊 ].
     * Step 3: Find the green Volume SeekBar in the bottom sheet, scroll it UP or DOWN, AND adjust actual hardware sound stream volume.
     */
    private AccessibilityNodeInfo findOrExpandSpeakerButton() throws InterruptedException {
        AccessibilityNodeInfo speakerBtn = findSpeakerIconButtonInAllWindows();
        if (speakerBtn != null) {
            return speakerBtn;
        }
        log("Volume UI: Speaker icon button not found. Checking if on Home screen mini-player...");
        AccessibilityNodeInfo miniPlayer = findMiniPlayerBarInAllWindows();
        if (miniPlayer != null) {
            log("Volume UI: Found Mini-Player bar. Clicking to expand to Full-Screen Player...");
            performClick(miniPlayer);
            Thread.sleep(1200);
            return findSpeakerIconButtonInAllWindows();
        }
        return null;
    }

    private AccessibilityNodeInfo locateVolumeSeekBar(String musicSeekBarId) throws InterruptedException {
        for (int attempt = 1; attempt <= 3; attempt++) {
            Thread.sleep(600);
            AccessibilityNodeInfo volumeSeekBar = findSeekBarExcludingInAllWindows(musicSeekBarId);
            if (volumeSeekBar != null) {
                log("Volume UI: Found NEW volume SeekBar on attempt " + attempt + ".");
                return volumeSeekBar;
            }
            log("Volume UI: Volume panel not yet open. Attempt " + attempt + "/3...");
        }
        return null;
    }

    public boolean adjustJioSaavnVolumeUi(boolean isVolumeUp) {
        try {
            log("Volume UI: Starting UI navigation sequence for volume " + (isVolumeUp ? "UP" : "DOWN"));
            if (!checkJioSaavnForeground()) {
                ensureJioSaavnForeground();
            }
            if (!checkJioSaavnForeground()) {
                log("Volume UI: JioSaavn could not be foregrounded, deferring to hardware volume.");
                return false;
            }

            AccessibilityNodeInfo musicSeekBar = findVolumeSeekBarInAllWindows();
            String musicSeekBarId = (musicSeekBar != null && musicSeekBar.getViewIdResourceName() != null)
                    ? musicSeekBar.getViewIdResourceName() : "";

            AccessibilityNodeInfo speakerBtn = findOrExpandSpeakerButton();
            if (speakerBtn == null) {
                log("Volume UI: Could not locate Speaker Icon button [ \uD83D\uDD0A ] in current layout.");
                return false;
            }

            log("Volume UI: Clicking Speaker Icon button to open Listening On volume panel...");
            performClick(speakerBtn);

            AccessibilityNodeInfo volumeSeekBar = locateVolumeSeekBar(musicSeekBarId);
            if (volumeSeekBar == null) {
                log("Volume UI: Speaker clicked but volume SeekBar not found after 3 attempts.");
                return false;
            }

            boolean result = applyVolumeChange(volumeSeekBar, isVolumeUp);
            log("Volume UI: Adjusted revealed Volume SeekBar + hardware sound (" + (isVolumeUp ? "UP" : "DOWN") + "), result = " + result);
            Thread.sleep(700);
            dismissBottomSheetSafely(speakerBtn);
            Thread.sleep(300);
            return result;
        } catch (InterruptedException e) {
            log("Volume UI Interrupted: " + e.getMessage());
            Thread.currentThread().interrupt();
        } catch (Exception e) {
            log("Volume UI Exception: " + e.getMessage());
        }
        return false;
    }

    private void dismissBottomSheetSafely(AccessibilityNodeInfo speakerBtn) {
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.N) {
            try {
                android.util.DisplayMetrics dm = getResources().getDisplayMetrics();
                int tapX = dm.widthPixels / 2;
                int tapY = (int) (dm.heightPixels * 0.15); // Top 15% is the dim scrim area outside the bottom sheet
                
                android.graphics.Path path = new android.graphics.Path();
                path.moveTo(tapX, tapY);
                android.accessibilityservice.GestureDescription.StrokeDescription stroke =
                        new android.accessibilityservice.GestureDescription.StrokeDescription(path, 0, 50);
                android.accessibilityservice.GestureDescription.Builder builder =
                        new android.accessibilityservice.GestureDescription.Builder();
                builder.addStroke(stroke);
                dispatchGesture(builder.build(), null, null);
                log("Volume UI: Safely dismissed bottom sheet panel via scrim tap (no back action).");
                return;
            } catch (Exception e) {
                log("Volume UI: Scrim tap exception: " + e.getMessage());
            }
        }
        if (speakerBtn != null) {
            performClick(speakerBtn);
        }
    }

    private boolean applyVolumeChange(AccessibilityNodeInfo seekBar, boolean isVolumeUp) {
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        if (am == null) return false;

        // 1. Retrieve current media volume state dynamically from device
        int curVol = am.getStreamVolume(AudioManager.STREAM_MUSIC);
        int maxVol = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        if (maxVol <= 0) maxVol = 15;

        // 2. Compute symmetric 15% proportional step for both Volume Up and Volume Down
        // Solves edge case of different maxVol scales across devices (Samsung 150 vs Stock 15 vs Xiaomi 30)
        int step = Math.max(1, (int) Math.round(maxVol * 0.15)); // Equal 15% step (~2 steps on 15 scale)

        // 3. Compute target volume state bounded strictly between 0 and maxVol
        int targetVol = isVolumeUp ? Math.min(maxVol, curVol + step) : Math.max(0, curVol - step);

        log("Volume Ratio Logic: Read state curVol=" + curVol + "/" + maxVol + " -> Computed targetVol=" + targetVol + " (Step: " + (isVolumeUp ? "+" : "-") + step + ")");

        // 4. Update device volume state
        try {
            am.setStreamVolume(AudioManager.STREAM_MUSIC, targetVol, AudioManager.FLAG_SHOW_UI);
            log("Volume Ratio Logic: Device volume updated to " + targetVol);
        } catch (Exception e) {
            log("Volume Ratio Logic: Device volume update exception: " + e.getMessage());
        }

        // 5. Update UI slider to match new volume ratio state with inset edge clamping
        return performSeekBarScrollRatio(seekBar, curVol, targetVol, maxVol, isVolumeUp);
    }

    private boolean performSeekBarScrollRatio(AccessibilityNodeInfo seekBar, int curVol, int targetVol, int maxVol, boolean isVolumeUp) {
        if (seekBar == null) return false;
        boolean result = false;

        if (maxVol <= 0) maxVol = 15;
        float curRatio = Math.max(0.0f, Math.min(1.0f, (float) curVol / (float) maxVol));
        float targetRatio = Math.max(0.0f, Math.min(1.0f, (float) targetVol / (float) maxVol));

        // 1. Simulate exact finger drag from curRatio position to targetRatio position on slider bar
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.N) {
            try {
                android.graphics.Rect rect = new android.graphics.Rect();
                seekBar.getBoundsInScreen(rect);
                if (!rect.isEmpty() && rect.width() > 0) {
                    float startX = rect.left + (rect.width() * curRatio);
                    float endX = rect.left + (rect.width() * targetRatio);

                    // Clamp within slider boundaries using 10px inset buffer (Solves Edge Case 2: Thumb Knob Clipping)
                    float minX = rect.left + 10;
                    float maxX = rect.right - 10;
                    startX = Math.max(minX, Math.min(maxX, startX));
                    endX = Math.max(minX, Math.min(maxX, endX));

                    float startY = rect.centerY();
                    float endY = rect.centerY();

                    android.graphics.Path path = new android.graphics.Path();
                    path.moveTo(startX, startY);
                    path.lineTo(endX, endY);

                    // 280ms duration (Solves Edge Case 3: Touch Slop & OEM Drag Speed thresholds)
                    android.accessibilityservice.GestureDescription.StrokeDescription stroke =
                            new android.accessibilityservice.GestureDescription.StrokeDescription(path, 0, 280);
                    android.accessibilityservice.GestureDescription.Builder builder =
                            new android.accessibilityservice.GestureDescription.Builder();
                    builder.addStroke(stroke);
                    boolean gestureDispatched = dispatchGesture(builder.build(), null, null);
                    log("Volume UI: Proportional drag from vol " + curVol + " (" + String.format("%.1f", curRatio * 100) + "%) to vol " + targetVol + " (" + String.format("%.1f", targetRatio * 100) + "%)");
                    result |= gestureDispatched;
                }
            } catch (Exception e) {
                log("Volume UI: Gesture dispatch exception: " + e.getMessage());
            }

            // 2. Set progress action fallback matching target ratio
            try {
                android.os.Bundle args = new android.os.Bundle();
                args.putFloat(AccessibilityNodeInfo.ACTION_ARGUMENT_PROGRESS_VALUE, targetRatio * 100.0f);
                result |= seekBar.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_PROGRESS.getId(), args);
            } catch (Exception ignored) {
            // Ignored; fallback search in active window handles this
        }
        }

        // 3. Scroll action fallback
        int action = isVolumeUp
                ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                : AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD;
        result |= seekBar.performAction(action);
        return result;
    }

    private AccessibilityNodeInfo findVolumeSeekBarInAllWindows() {
        try {
            java.util.List<android.view.accessibility.AccessibilityWindowInfo> windows = getWindows();
            if (windows != null && !windows.isEmpty()) {
                for (android.view.accessibility.AccessibilityWindowInfo w : windows) {
                    AccessibilityNodeInfo root = w.getRoot();
                    if (root != null) {
                        AccessibilityNodeInfo match = findVolumeSeekBar(root);
                        if (match != null) return match;
                    }
                }
            }
        } catch (Exception ignored) {
            // Ignored; fallback search in active window handles this
        }
        AccessibilityNodeInfo activeRoot = getRootInActiveWindow();
        return activeRoot != null ? findVolumeSeekBar(activeRoot) : null;
    }

    private AccessibilityNodeInfo findSpeakerIconButtonInAllWindows() {
        try {
            java.util.List<android.view.accessibility.AccessibilityWindowInfo> windows = getWindows();
            if (windows != null && !windows.isEmpty()) {
                for (android.view.accessibility.AccessibilityWindowInfo w : windows) {
                    AccessibilityNodeInfo root = w.getRoot();
                    if (root != null) {
                        AccessibilityNodeInfo match = findSpeakerIconButton(root);
                        if (match != null) return match;
                    }
                }
            }
        } catch (Exception ignored) {
            // Ignored; fallback search in active window handles this
        }
        AccessibilityNodeInfo activeRoot = getRootInActiveWindow();
        return activeRoot != null ? findSpeakerIconButton(activeRoot) : null;
    }

    private AccessibilityNodeInfo findMiniPlayerBarInAllWindows() {
        try {
            java.util.List<android.view.accessibility.AccessibilityWindowInfo> windows = getWindows();
            if (windows != null && !windows.isEmpty()) {
                for (android.view.accessibility.AccessibilityWindowInfo w : windows) {
                    AccessibilityNodeInfo root = w.getRoot();
                    if (root != null) {
                        AccessibilityNodeInfo match = findMiniPlayerBar(root);
                        if (match != null) return match;
                    }
                }
            }
        } catch (Exception ignored) {
            // Ignored; fallback search in active window handles this
        }
        AccessibilityNodeInfo activeRoot = getRootInActiveWindow();
        return activeRoot != null ? findMiniPlayerBar(activeRoot) : null;
    }

    private AccessibilityNodeInfo findMiniPlayerBar(AccessibilityNodeInfo node) {
        if (node == null) return null;

        String viewId = node.getViewIdResourceName() != null ? node.getViewIdResourceName().toLowerCase() : "";
        if (viewId.contains("mini_player") || viewId.contains("miniplayer") || viewId.contains("play_bar") || viewId.contains("bottom_player") || viewId.contains("player_mini")) {
            return findClickableAncestor(node);
        }

        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findMiniPlayerBar(child);
            if (result != null) return result;
        }
        return null;
    }



    /**
     * Finds any SeekBar/Slider in the accessibility tree.
     * Used to initially detect the music progress bar before the volume panel opens.
     */
    private AccessibilityNodeInfo findVolumeSeekBar(AccessibilityNodeInfo node) {
        if (node == null) return null;

        String className = node.getClassName() != null ? node.getClassName().toString() : "";

        if (className.contains("SeekBar") || className.contains("AbsSeekBar")
                || className.contains("Slider")) {
            return node;
        }

        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findVolumeSeekBar(child);
            if (result != null) return result;
        }
        return null;
    }

    /**
     * Finds a SeekBar/Slider whose resource ID is DIFFERENT from the excluded ID.
     * This is used after clicking the Speaker button to find the VOLUME slider
     * while ignoring the MUSIC PROGRESS BAR that was already on screen.
     *
     * JioSaavn uses obfuscated numeric resource IDs (e.g. "2131364647") so we
     * cannot check for readable keywords like "volume". Instead we use this
     * exclusion-based approach.
     */
    private AccessibilityNodeInfo findSeekBarExcluding(AccessibilityNodeInfo node, String excludedId) {
        if (node == null) return null;

        String className = node.getClassName() != null ? node.getClassName().toString() : "";

        if (className.contains("SeekBar") || className.contains("AbsSeekBar")
                || className.contains("Slider")) {
            String nodeId = node.getViewIdResourceName() != null
                    ? node.getViewIdResourceName() : "";
            // Only return this SeekBar if it is NOT the excluded one (the music bar)
            if (!nodeId.equals(excludedId) || excludedId.isEmpty()) {
                return node;
            }
        }

        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findSeekBarExcluding(child, excludedId);
            if (result != null) return result;
        }
        return null;
    }

    /**
     * Searches all accessibility windows for a SeekBar that is NOT the excluded one.
     */
    private AccessibilityNodeInfo findSeekBarExcludingInAllWindows(String excludedId) {
        try {
            java.util.List<android.view.accessibility.AccessibilityWindowInfo> windows = getWindows();
            if (windows != null && !windows.isEmpty()) {
                for (android.view.accessibility.AccessibilityWindowInfo w : windows) {
                    AccessibilityNodeInfo root = w.getRoot();
                    if (root != null) {
                        AccessibilityNodeInfo match = findSeekBarExcluding(root, excludedId);
                        if (match != null) return match;
                    }
                }
            }
        } catch (Exception ignored) {
            // Ignored; fallback search in active window handles this
        }
        AccessibilityNodeInfo activeRoot = getRootInActiveWindow();
        return activeRoot != null ? findSeekBarExcluding(activeRoot, excludedId) : null;
    }

    private AccessibilityNodeInfo findSpeakerIconButton(AccessibilityNodeInfo node) {
        if (node == null) return null;

        CharSequence desc = node.getContentDescription();
        if (desc != null) {
            String d = desc.toString().toLowerCase().trim();
            if (d.contains("speaker") || d.contains("device") || d.contains("listening on") || d.contains("this phone") || d.contains("audio output") || d.contains("output") || d.contains("cast") || d.contains("source")) {
                return findClickableAncestor(node);
            }
        }

        CharSequence text = node.getText();
        if (text != null) {
            String t = text.toString().toLowerCase().trim();
            if (t.contains("listening on") || t.contains("this phone") || t.contains("device settings")) {
                return findClickableAncestor(node);
            }
        }

        String viewId = node.getViewIdResourceName() != null ? node.getViewIdResourceName().toLowerCase() : "";
        if (viewId.contains("device") || viewId.contains("speaker") || viewId.contains("listening_on") || viewId.contains("output") || viewId.contains("source") || viewId.contains("tv_device") || viewId.contains("iv_device")) {
            return findClickableAncestor(node);
        }

        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findSpeakerIconButton(child);
            if (result != null) return result;
        }
        return null;
    }

    private AccessibilityNodeInfo findClickableAncestor(AccessibilityNodeInfo node) {
        AccessibilityNodeInfo current = node;
        while (current != null) {
            if (current.isClickable()) {
                return current;
            }
            current = current.getParent();
        }
        return node;
    }

    /**
     * Attempts to find and click the Play/Pause button in the JioSaavn UI.
     */
    public boolean clickPlayPause() {
        String[] keywords = {"play", "pause", "toggle playback"};
        return findAndClickByKeywords(keywords, "Play/Pause");
    }

    /**
     * Attempts to find and click the Next button in the JioSaavn UI.
     */
    public boolean clickNextSong() {
        String[] keywords = {"next", "skip forward", "fast forward", "forward"};
        return findAndClickByKeywords(keywords, "Next");
    }

    /**
     * Attempts to find and click the Previous button in the JioSaavn UI.
     */
    public boolean clickPreviousSong() {
        String[] keywords = {"previous", "skip back", "rewind", "back"};
        return findAndClickByKeywords(keywords, "Previous");
    }

    private boolean findAndClickByKeywords(String[] keywords, String label) {
        log("Searching for " + label + " button in UI...");
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return false;

        AccessibilityNodeInfo node = findNodeByKeywords(root, keywords);
        if (node != null) {
            log("Found " + label + " button. Performing click...");
            return performClick(node);
        }

        log(label + " button not found in active window.");
        return false;
    }

    private AccessibilityNodeInfo findNodeByKeywords(AccessibilityNodeInfo node, String[] keywords) {
        if (node == null) return null;

        CharSequence desc = node.getContentDescription();
        CharSequence text = node.getText();

        for (String keyword : keywords) {
            if (desc != null && desc.toString().toLowerCase().contains(keyword)) {
                return node;
            }
            if (text != null && text.toString().toLowerCase().contains(keyword)) {
                return node;
            }
        }

        int childCount = node.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findNodeByKeywords(child, keywords);
            if (result != null) return result;
        }
        return null;
    }

    /**
     * After a deep link or search opens the song detail page in JioSaavn, find and click the
     * prominent circular teal Play button on that page (circled in user screenshot) to start playback.
     * 
     * Uses a multi-tiered approach:
     * 1. Keyword & ID matching in song page region.
     * 2. Neighbor detection: finds Heart/Favorite icon and clicks the circular button to its right.
     * 3. Geometric detection: identifies the circular/square button in [X: 65%-98%, Y: 30%-58%].
     * 4. Coordinate tap fallback: taps at (X: 0.86*width, Y: 0.44*height) if tree search is exhausted.
     * 5. Strictly ignores bottom mini-player to avoid playing previous cached songs.
     */
    private boolean tryFindAndClickPlay(AccessibilityNodeInfo root, String songTitle, int screenWidth, int screenHeight) {
        AccessibilityNodeInfo playBtn = findSongPagePlayButton(root, screenWidth, screenHeight);
        if (playBtn != null) {
            log("playSearchedSong: Found song page play button. Clicking...");
            return clickNodeOrTap(playBtn);
        }
        if (songTitle != null && !songTitle.trim().isEmpty()) {
            AccessibilityNodeInfo songItem = findSongItemByTitle(root, songTitle, screenHeight);
            if (songItem != null) {
                log("playSearchedSong: Found song item matching '" + songTitle + "'. Clicking...");
                return clickNodeOrTap(songItem);
            }
        }
        return false;
    }

    public void playSearchedSong(final String songTitle) {
        new Thread(() -> {
            try {
                log("playSearchedSong: Waiting for JioSaavn song page to render for '" + songTitle + "'...");
                Thread.sleep(600);

                android.util.DisplayMetrics dm = getResources().getDisplayMetrics();
                int screenWidth = dm != null ? dm.widthPixels : 1080;
                int screenHeight = dm != null ? dm.heightPixels : 2400;
                boolean clicked = false;

                for (int attempt = 1; attempt <= 15 && !clicked; attempt++) {
                    AccessibilityNodeInfo root = getRootInActiveWindow();
                    if (root == null) {
                        log("playSearchedSong: Attempt " + attempt + " — root is null. Retrying...");
                    } else {
                        if (attempt == 1) {
                            logNodesInTargetArea(root, screenWidth, screenHeight);
                        }
                        clicked = tryFindAndClickPlay(root, songTitle, screenWidth, screenHeight);
                    }
                    if (!clicked) {
                        Thread.sleep(350);
                    }
                }

                if (!clicked && screenWidth > 0 && screenHeight > 0) {
                    log("playSearchedSong: Accessibility node search exhausted. Attempting physical coordinate tap...");
                    clicked = tapCoordinates((int) (screenWidth * 0.86), (int) (screenHeight * 0.44));
                }

                log("playSearchedSong: " + (clicked ? "Successfully triggered play!" : "Could not trigger play."));
            } catch (InterruptedException e) {
                log("playSearchedSong interrupted: " + e.getMessage());
                Thread.currentThread().interrupt();
            } catch (Exception e) {
                log("playSearchedSong error: " + e.getMessage());
            }
        }).start();
    }

    /**
     * Finds the prominent Play button on JioSaavn's song detail page (the circled teal button).
     * Strictly ignores any view in the bottom mini-player area (bottom 28% of screen)
     * and focuses on the upper-middle right area below the album artwork.
     */
    private AccessibilityNodeInfo findSongPagePlayButton(AccessibilityNodeInfo root, int screenWidth, int screenHeight) {
        if (root == null) return null;

        // Pass 1: Try keyword and ID match in the upper-middle song page region
        AccessibilityNodeInfo keywordMatch = findPlayButtonByKeyword(root, screenWidth, screenHeight);
        if (keywordMatch != null) return keywordMatch;

        // Pass 2: Look for the Heart/Favorite button and find the Play button directly to its right
        AccessibilityNodeInfo neighborMatch = findPlayButtonNextToHeart(root, screenWidth, screenHeight);
        if (neighborMatch != null) return neighborMatch;

        // Pass 3: Geometric detection of circular/square clickable view in the target quadrant
        AccessibilityNodeInfo geometricMatch = findPlayButtonByGeometry(root, screenWidth, screenHeight);
        if (geometricMatch != null) return geometricMatch;

        return null;
    }

    private boolean matchesPlayDescription(CharSequence desc) {
        if (desc == null) return false;
        String d = desc.toString().toLowerCase().trim();
        return !d.contains("previous") && !d.contains("next") && !d.contains("rewind") && !d.contains("forward")
                && (d.equals("play icon") || d.equals("play") || d.equals("play song") || d.equals("play now")
                || d.equals("play music") || d.equals("play all") || d.contains("play"));
    }

    private boolean matchesPlayText(CharSequence text) {
        if (text == null) return false;
        String t = text.toString().toLowerCase().trim();
        return t.equals("play") || t.equals("play song") || t.equals("play now") || t.equals("play all");
    }

    private boolean matchesPlayResId(String resId) {
        if (resId == null || resId.isEmpty()) return false;
        return (resId.contains("play_button") || resId.contains("btn_play") || resId.contains("play_icon")
                || resId.contains("playcurrentsong") || resId.contains("fab_play") || resId.contains("floating_play")
                || resId.contains("iv_play")) && !resId.contains("next") && !resId.contains("prev");
    }

    private AccessibilityNodeInfo checkPlayNodeMatch(AccessibilityNodeInfo node) {
        if (matchesPlayDescription(node.getContentDescription()) || matchesPlayText(node.getText())) {
            AccessibilityNodeInfo clickable = findClickableAncestor(node);
            return (clickable != null) ? clickable : (node.isClickable() ? node : null);
        }
        if (matchesPlayResId(node.getViewIdResourceName() != null ? node.getViewIdResourceName().toLowerCase() : "")) {
            return findClickableAncestor(node);
        }
        return null;
    }

    private AccessibilityNodeInfo findPlayButtonByKeyword(AccessibilityNodeInfo node, int screenWidth, int screenHeight) {
        if (node == null) return null;
        android.graphics.Rect bounds = new android.graphics.Rect();
        node.getBoundsInScreen(bounds);
        if (screenHeight > 0 && bounds.top > screenHeight * 0.72) {
            return null;
        }
        AccessibilityNodeInfo match = checkPlayNodeMatch(node);
        if (match != null) return match;

        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo result = findPlayButtonByKeyword(node.getChild(i), screenWidth, screenHeight);
            if (result != null) return result;
        }
        return null;
    }

    private AccessibilityNodeInfo findPlayButtonNextToHeart(AccessibilityNodeInfo root, int screenWidth, int screenHeight) {
        if (root == null || screenHeight <= 0) return null;

        // Step A: Locate the Heart/Favorite icon
        AccessibilityNodeInfo heartNode = findHeartIconNode(root, screenHeight);
        if (heartNode == null) return null;

        android.graphics.Rect heartBounds = new android.graphics.Rect();
        heartNode.getBoundsInScreen(heartBounds);
        log("findPlayButtonNextToHeart: Found Heart icon at bounds=" + heartBounds.toShortString());

        // Step B: Find a clickable view to the right of the heart at approximately the same vertical level
        return findNodeToRightOf(root, heartBounds, screenWidth, screenHeight);
    }

    private boolean isHeartIcon(AccessibilityNodeInfo node) {
        CharSequence desc = node.getContentDescription();
        if (desc != null) {
            String d = desc.toString().toLowerCase().trim();
            if (d.contains("favorite") || d.contains("like") || d.contains("heart") || d.contains("favourite")) {
                return true;
            }
        }
        String resId = node.getViewIdResourceName() != null ? node.getViewIdResourceName().toLowerCase() : "";
        return resId.contains("favorite") || resId.contains("like") || resId.contains("heart") || resId.contains("fav");
    }

    private AccessibilityNodeInfo findHeartIconNode(AccessibilityNodeInfo node, int screenHeight) {
        if (node == null) return null;
        android.graphics.Rect bounds = new android.graphics.Rect();
        node.getBoundsInScreen(bounds);
        if (bounds.top > screenHeight * 0.72) return null;

        if (bounds.centerY() >= screenHeight * 0.25 && bounds.centerY() <= screenHeight * 0.60 && isHeartIcon(node)) {
            return node;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo result = findHeartIconNode(node.getChild(i), screenHeight);
            if (result != null) return result;
        }
        return null;
    }

    private AccessibilityNodeInfo findNodeToRightOf(AccessibilityNodeInfo node, android.graphics.Rect anchor, int screenWidth, int screenHeight) {
        if (node == null) return null;

        android.graphics.Rect bounds = new android.graphics.Rect();
        node.getBoundsInScreen(bounds);

        // Check if this node is to the right of the anchor, roughly same vertical alignment, and in right half
        if (bounds.left >= anchor.right - 20
                && bounds.centerX() <= screenWidth
                && Math.abs(bounds.centerY() - anchor.centerY()) < (anchor.height() * 1.5 + 40)
                && bounds.width() > 30 && bounds.height() > 30) {
            AccessibilityNodeInfo clickable = findClickableAncestor(node);
            if (clickable != null) {
                log("findNodeToRightOf: Found target button to right of heart: bounds=" + bounds.toShortString());
                return clickable;
            }
            if (node.isClickable()) {
                return node;
            }
        }

        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo result = findNodeToRightOf(child, anchor, screenWidth, screenHeight);
            if (result != null) return result;
        }
        return null;
    }

    private boolean isTargetPlayButtonZone(android.graphics.Rect bounds, int screenWidth, int screenHeight) {
        return bounds.centerY() >= screenHeight * 0.30 && bounds.centerY() <= screenHeight * 0.58
                && bounds.centerX() >= screenWidth * 0.65 && bounds.centerX() <= screenWidth * 0.98;
    }

    private boolean isCircularButtonDimensions(int w, int h) {
        return w >= 50 && w <= 350 && h >= 50 && h <= 350 && Math.abs(w - h) < w * 0.45;
    }

    private AccessibilityNodeInfo findPlayButtonByGeometry(AccessibilityNodeInfo node, int screenWidth, int screenHeight) {
        if (node == null || screenWidth <= 0 || screenHeight <= 0) return null;

        android.graphics.Rect bounds = new android.graphics.Rect();
        node.getBoundsInScreen(bounds);

        if (isTargetPlayButtonZone(bounds, screenWidth, screenHeight) && isCircularButtonDimensions(bounds.width(), bounds.height())) {
            AccessibilityNodeInfo clickable = findClickableAncestor(node);
            if (clickable != null) {
                log("findPlayButtonByGeometry: Matched circular view in target zone: bounds=" + bounds.toShortString());
                return clickable;
            }
            if (node.isClickable()) {
                log("findPlayButtonByGeometry: Matched clickable view: bounds=" + bounds.toShortString());
                return node;
            }
        }

        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo result = findPlayButtonByGeometry(node.getChild(i), screenWidth, screenHeight);
            if (result != null) return result;
        }
        return null;
    }

    private void logNodesInTargetArea(AccessibilityNodeInfo node, int screenWidth, int screenHeight) {
        if (node == null || screenHeight <= 0) return;
        android.graphics.Rect bounds = new android.graphics.Rect();
        node.getBoundsInScreen(bounds);
        if (bounds.centerY() >= screenHeight * 0.28 && bounds.centerY() <= screenHeight * 0.60
                && bounds.width() > 20 && bounds.height() > 20) {
            CharSequence d = node.getContentDescription();
            CharSequence t = node.getText();
            String id = node.getViewIdResourceName();
            log("TargetZoneNode: [" + node.getClassName() + "] bounds=" + bounds.toShortString()
                    + " click=" + node.isClickable()
                    + (d != null ? " desc='" + d + "'" : "")
                    + (t != null ? " text='" + t + "'" : "")
                    + (id != null ? " id='" + id + "'" : ""));
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            logNodesInTargetArea(node.getChild(i), screenWidth, screenHeight);
        }
    }

    /**
     * Finds a clickable song title or row matching the searched song's name in the active view,
     * strictly excluding the bottom mini-player area.
     */
    private boolean isTitleMatched(AccessibilityNodeInfo node, String queryClean) {
        CharSequence text = node.getText();
        if (text != null) {
            String t = text.toString().toLowerCase().trim();
            if (t.length() >= 3 && (t.equals(queryClean) || t.contains(queryClean) || queryClean.contains(t))) {
                return true;
            }
        }
        CharSequence desc = node.getContentDescription();
        if (desc != null) {
            String d = desc.toString().toLowerCase().trim();
            return d.length() >= 3 && (d.contains(queryClean) || queryClean.contains(d));
        }
        return false;
    }

    private AccessibilityNodeInfo findSongItemByTitle(AccessibilityNodeInfo node, String songTitle, int screenHeight) {
        if (node == null || songTitle == null || songTitle.trim().isEmpty()) return null;

        if (screenHeight > 0) {
            android.graphics.Rect bounds = new android.graphics.Rect();
            node.getBoundsInScreen(bounds);
            if (bounds.top > screenHeight * 0.72) return null;
        }

        String className = node.getClassName() != null ? node.getClassName().toString() : "";
        if (className.contains(CLASS_EDIT_TEXT) || node.isEditable()) return null;

        String queryClean = songTitle.toLowerCase().trim();
        if (isTitleMatched(node, queryClean)) {
            AccessibilityNodeInfo clickable = findClickableAncestor(node);
            if (clickable != null) return clickable;
        }

        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo result = findSongItemByTitle(node.getChild(i), songTitle, screenHeight);
            if (result != null) return result;
        }
        return null;
    }

    public boolean clickNodeOrTap(AccessibilityNodeInfo node) {
        if (node == null) return false;

        // Try 1: Click clickable ancestor
        AccessibilityNodeInfo clickable = findClickableAncestor(node);
        if (clickable != null && clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            log("clickNodeOrTap: ACTION_CLICK succeeded on " + clickable.getClassName());
            return true;
        }

        // Try 2: Click node directly if distinct
        if (node != clickable && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            log("clickNodeOrTap: ACTION_CLICK succeeded on node " + node.getClassName());
            return true;
        }

        // Try 3: Physical tap gesture at center coordinates
        android.graphics.Rect rect = new android.graphics.Rect();
        node.getBoundsInScreen(rect);
        if (rect.width() > 0 && rect.height() > 0) {
            int x = rect.centerX();
            int y = rect.centerY();
            return tapCoordinates(x, y);
        }
        return false;
    }

    public boolean tapCoordinates(int x, int y) {
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.N) {
            android.graphics.Path path = new android.graphics.Path();
            path.moveTo(x, y);
            android.accessibilityservice.GestureDescription stroke =
                    new android.accessibilityservice.GestureDescription.Builder()
                            .addStroke(new android.accessibilityservice.GestureDescription.StrokeDescription(path, 0, 80))
                            .build();
            boolean dispatched = dispatchGesture(stroke, null, null);
            log("tapCoordinates: Physical gesture tap dispatched at (" + x + ", " + y + "): " + dispatched);
            return dispatched;
        }
        return false;
    }

    private static void log(String message) {
        Log.d(TAG, message);
        if (MainActivity.getInstance() != null) {
            MainActivity.getInstance().appendLog("ACCESSIBILITY: " + message);
        }
    }
}
