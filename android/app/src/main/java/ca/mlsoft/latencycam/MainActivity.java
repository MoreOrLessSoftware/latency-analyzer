package ca.mlsoft.latencycam;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.RectF;
import android.graphics.SurfaceTexture;
import android.graphics.Typeface;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.text.InputType;
import android.view.Gravity;
import android.view.Surface;
import android.view.TextureView;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * Films the client display at 240 fps while keeping the phone's clock synced with the host, so the analyzer can
 * time what the camera saw on the host's clock. After each recording the user uploads it to the host for analysis
 * or discards it; the results show on a second tab.
 *
 * While latency-test shows its QR code the app starts by itself: once the whole screen is steady in view it asks the
 * host for the test pattern, counts down and records.
 */
public class MainActivity extends Activity implements Recorder.Listener {
    private static final int CAMERA_REQUEST = 1;
    private static final int RED = 0xFFD03B3B;
    private static final int GREEN = 0xFF0CA30C;
    private static final int AMBER = 0xFFFAB219;
    private static final int MUTED = 0xFF8A8984;
    private static final int BLUE = 0xFF2A78D6;
    private static final int PANE_DP = 320;
    /** Recordings stop by themselves after this long. */
    private static final int AUTO_STOP_S = 20;
    private static final long SCAN_INTERVAL_MS = 250;
    /** The screen has to stay in place this long before the countdown starts. */
    private static final long STEADY_MS = 1000;
    /** How far the screen may wander while holding still, as a fraction of the frame's long side. */
    private static final double STEADY_TOLERANCE = 0.03;
    /** Frames in a row without the code before the framing advice goes away. */
    private static final int SCAN_MISSES_TO_CLEAR = 4;
    private static final int MIN_SYNC_SAMPLES = 10;
    private static final double MAX_SYNC_ROUND_TRIP_MS = 5;
    private static final int COUNTDOWN_S = 3;
    private static final int HTTP_TIMEOUT_MS = 2000;

    /** Where the automatic start has got to. */
    private enum AutoStart { SCANNING, ASKING_HOST, COUNTDOWN }

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ClockSync clockSync = new ClockSync();
    private Recorder recorder;
    private Uploader uploader;
    private QrScanner scanner;
    private AutoStart autoStart = AutoStart.SCANNING;
    private int countdown;
    private int scanMisses;
    private long steadySinceMs;  // 0 when the screen hasn't settled yet
    private double steadyX;
    private double steadyY;
    private WifiManager.WifiLock wifiLock;
    private SharedPreferences prefs;
    private boolean cameraOpen;
    private boolean resumed;
    private boolean onRecordingTab;

    private TextureView preview;
    private View cameraPlaceholder;
    private TextView placeholderText;
    private TextView scanText;
    private TextView countdownText;
    private TextView recordingTab;
    private TextView resultsTab;
    private View recordingPage;
    private View resultsPage;

    private EditText nameField;
    private EditText hostField;
    private Button recordButton;
    private ColorStateList recordTint;
    private LinearLayout decisionRow;
    private TextView syncText;
    private TextView recordText;
    private TextView cameraText;

    private TextView resultTitle;
    private TextView resultText;
    private Button reportButton;
    private Button retryButton;

    // The last recording saved and not yet uploaded or discarded
    private String pendingName;
    private Uri pendingVideo;
    private Uri pendingTiming;
    // The last recording handed to the uploader, for a retry
    private String uploadName;
    private Uri uploadVideo;
    private Uri uploadTiming;
    private String reportUrl;
    private Uri savedReport;  // The report downloaded to the phone, readable without the host

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("latency-cam", MODE_PRIVATE);
        recorder = new Recorder(this, clockSync, this);
        uploader = new Uploader(getContentResolver(), uploadListener);
        scanner = new QrScanner(this::onScan);
        buildViews();
        WifiManager wifi = getSystemService(WifiManager.class);
        wifiLock = wifi.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "latency-cam");
        applyHost(prefs.getString("host", ""));
    }

    // -----------------------------------------------------------------------------------------------------------------
    // Layout

    private void buildViews() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        preview = new TextureView(this);
        root.addView(preview, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT, Gravity.START | Gravity.CENTER_VERTICAL));
        preview.setSurfaceTextureListener(new TextureView.SurfaceTextureListener() {
            @Override
            public void onSurfaceTextureAvailable(SurfaceTexture texture, int width, int height) {
                // TextureView sizes its buffers to the view; the camera needs exactly the recording size
                texture.setDefaultBufferSize(Recorder.WIDTH, Recorder.HEIGHT);
                fitPreview();
                openCameraIfReady();
            }

            @Override
            public void onSurfaceTextureSizeChanged(SurfaceTexture texture, int width, int height) {
                // Resizing the view resets the buffer size to the view's, which the high speed session refuses
                texture.setDefaultBufferSize(Recorder.WIDTH, Recorder.HEIGHT);
                fitPreview();
                openCameraIfReady();
            }

            @Override
            public boolean onSurfaceTextureDestroyed(SurfaceTexture texture) {
                return true;
            }

            @Override
            public void onSurfaceTextureUpdated(SurfaceTexture texture) {
            }
        });
        root.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> fitPreview());

        // Covers the preview while the camera is closed, rather than leaving its last frame up
        cameraPlaceholder = buildCameraPlaceholder();
        FrameLayout.LayoutParams placeholderParams = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        placeholderParams.rightMargin = dp(PANE_DP);
        root.addView(cameraPlaceholder, placeholderParams);

        // The countdown covers the preview; tapping it cancels. The advice sits on top of everything over the preview.
        countdownText = new TextView(this);
        countdownText.setTextColor(Color.WHITE);
        countdownText.setTextSize(160);
        countdownText.setTypeface(Typeface.DEFAULT_BOLD);
        countdownText.setGravity(Gravity.CENTER);
        countdownText.setBackgroundColor(0x99000000);
        countdownText.setVisibility(View.GONE);
        countdownText.setOnClickListener(v -> cancelAutoStart());
        FrameLayout.LayoutParams countdownParams = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        countdownParams.rightMargin = dp(PANE_DP);
        root.addView(countdownText, countdownParams);

        scanText = new TextView(this);
        scanText.setTextColor(Color.WHITE);
        scanText.setTextSize(18);
        scanText.setGravity(Gravity.CENTER);
        scanText.setBackgroundColor(0xCC000000);
        scanText.setPadding(dp(16), dp(8), dp(16), dp(8));
        scanText.setVisibility(View.GONE);
        FrameLayout.LayoutParams scanParams = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
        scanParams.rightMargin = dp(PANE_DP);
        scanParams.bottomMargin = dp(24);
        root.addView(scanText, scanParams);

        // Right-hand pane: a tab bar over two pages
        LinearLayout pane = new LinearLayout(this);
        pane.setOrientation(LinearLayout.VERTICAL);
        // Nearly opaque: the 16:9 preview runs under the pane's left edge
        pane.setBackgroundColor(0xF2101010);
        root.addView(pane, new FrameLayout.LayoutParams(dp(PANE_DP), ViewGroup.LayoutParams.MATCH_PARENT, Gravity.END));

        LinearLayout tabs = new LinearLayout(this);
        tabs.setOrientation(LinearLayout.HORIZONTAL);
        recordingTab = tab("Recording");
        resultsTab = tab("Results");
        tabs.addView(recordingTab, new LinearLayout.LayoutParams(0, dp(48), 1));
        tabs.addView(resultsTab, new LinearLayout.LayoutParams(0, dp(48), 1));
        pane.addView(tabs);
        recordingTab.setOnClickListener(v -> showTab(true));
        resultsTab.setOnClickListener(v -> {
            // Leaving the tab closes the camera, which would end the recording
            if (!recorder.isRecording()) {
                showTab(false);
            }
        });

        FrameLayout pages = new FrameLayout(this);
        pane.addView(pages, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        recordingPage = buildRecordingPage();
        resultsPage = buildResultsPage();
        pages.addView(recordingPage);
        pages.addView(resultsPage);
        showTab(true);

        setContentView(root);
    }

    private View buildCameraPlaceholder() {
        LinearLayout placeholder = new LinearLayout(this);
        placeholder.setOrientation(LinearLayout.VERTICAL);
        placeholder.setGravity(Gravity.CENTER);
        placeholder.setBackgroundColor(Color.BLACK);
        ImageView icon = new ImageView(this);
        icon.setImageResource(R.drawable.ic_camera_paused);
        icon.setImageTintList(ColorStateList.valueOf(MUTED));
        placeholder.addView(icon, new LinearLayout.LayoutParams(dp(72), dp(72)));
        placeholderText = new TextView(this);
        placeholderText.setTextColor(MUTED);
        placeholderText.setTextSize(15);
        placeholderText.setGravity(Gravity.CENTER);
        placeholderText.setPadding(0, dp(8), 0, 0);
        placeholder.addView(placeholderText);
        return placeholder;
    }

    private void updateCameraPlaceholder() {
        cameraPlaceholder.setVisibility(cameraOpen ? View.GONE : View.VISIBLE);
        // On the Recording tab the status text below Record says why the camera is off
        placeholderText.setText(onRecordingTab ? "Camera off" : "Camera paused");
    }

    private View buildRecordingPage() {
        LinearLayout page = page();

        nameField = field("Test name, e.g. amf-av1-4ktv", InputType.TYPE_CLASS_TEXT);
        nameField.setText(prefs.getString("name", ""));
        page.addView(label("Test name"));
        page.addView(nameField);

        hostField = field("Host IP, blank to find it", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        hostField.setText(prefs.getString("host", ""));
        hostField.setImeOptions(EditorInfo.IME_ACTION_DONE);
        hostField.setOnEditorActionListener((v, actionId, event) -> {
            String host = hostField.getText().toString().trim();
            prefs.edit().putString("host", host).apply();
            applyHost(host);
            return false;
        });
        page.addView(label("Host"));
        page.addView(hostField);

        recordButton = new Button(this);
        recordButton.setText("Record");
        recordButton.setTextSize(18);
        recordButton.setEnabled(false);
        recordButton.setOnClickListener(v -> {
            cancelAutoStart();
            if (!recorder.isRecording()) {
                // In case the QR code is still up: filming that would be a wasted run
                askHostForPattern(null);
            }
            toggleRecording();
        });
        recordTint = recordButton.getBackgroundTintList();
        LinearLayout.LayoutParams recordParams = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(64));
        recordParams.topMargin = dp(12);
        page.addView(recordButton, recordParams);

        // Shown once a recording is saved: send it to the host, or throw it away
        decisionRow = new LinearLayout(this);
        decisionRow.setOrientation(LinearLayout.HORIZONTAL);
        Button upload = new Button(this);
        upload.setText("Upload & analyze");
        upload.setBackgroundTintList(ColorStateList.valueOf(BLUE));
        upload.setTextColor(Color.WHITE);
        upload.setOnClickListener(v -> uploadPending());
        Button discard = new Button(this);
        discard.setText("Discard");
        discard.setOnClickListener(v -> discardPending());
        LinearLayout.LayoutParams uploadParams = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 3);
        uploadParams.rightMargin = dp(6);
        decisionRow.addView(upload, uploadParams);
        decisionRow.addView(discard, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 2));
        decisionRow.setVisibility(View.GONE);
        page.addView(decisionRow);

        recordText = status();
        syncText = status();
        cameraText = status();
        cameraText.setTextColor(MUTED);
        page.addView(recordText);
        page.addView(syncText);
        page.addView(cameraText);
        return scroll(page);
    }

    private View buildResultsPage() {
        LinearLayout page = page();
        resultTitle = status();
        resultTitle.setTypeface(Typeface.DEFAULT_BOLD);
        resultTitle.setText("No results yet");
        resultText = status();
        resultText.setText("Record, then choose Upload & analyze to see the results here.");
        resultText.setTextColor(MUTED);
        page.addView(resultTitle);
        page.addView(resultText);

        reportButton = new Button(this);
        reportButton.setText("Open report");
        reportButton.setVisibility(View.GONE);
        reportButton.setOnClickListener(v -> {
            if (savedReport != null) {
                startActivity(new Intent(this, ReportActivity.class).setData(savedReport));
            } else if (reportUrl != null) {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(reportUrl)));
            }
        });
        retryButton = new Button(this);
        retryButton.setText("Retry upload");
        retryButton.setVisibility(View.GONE);
        retryButton.setOnClickListener(v -> startUpload(uploadName, uploadVideo, uploadTiming));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.topMargin = dp(12);
        page.addView(reportButton, params);
        page.addView(retryButton, new LinearLayout.LayoutParams(params));
        return scroll(page);
    }

    private LinearLayout page() {
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        page.setPadding(dp(16), dp(4), dp(16), dp(12));
        return page;
    }

    private ScrollView scroll(View content) {
        ScrollView scroller = new ScrollView(this);
        scroller.addView(content, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return scroller;
    }

    private TextView tab(String text) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextSize(15);
        view.setGravity(Gravity.CENTER);
        return view;
    }

    private void showTab(boolean recording) {
        onRecordingTab = recording;
        // The camera only runs while the Recording tab is showing
        if (recording) {
            openCameraIfReady();
        } else {
            closeCamera();
        }
        updateCameraPlaceholder();
        recordingPage.setVisibility(recording ? View.VISIBLE : View.GONE);
        resultsPage.setVisibility(recording ? View.GONE : View.VISIBLE);
        styleTab(recordingTab, recording);
        styleTab(resultsTab, !recording);
    }

    private void styleTab(TextView tab, boolean selected) {
        tab.setTextColor(selected ? Color.WHITE : MUTED);
        tab.setTypeface(selected ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
        tab.setBackgroundColor(selected ? 0x33FFFFFF : Color.TRANSPARENT);
    }

    private EditText field(String hint, int inputType) {
        EditText field = new EditText(this);
        field.setHint(hint);
        field.setSingleLine(true);
        field.setInputType(inputType);
        field.setTextColor(Color.WHITE);
        field.setHintTextColor(MUTED);
        return field;
    }

    private TextView label(String text) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextColor(0xFFC3C2B7);
        view.setTextSize(12);
        view.setPadding(0, dp(6), 0, 0);
        return view;
    }

    private TextView status() {
        TextView view = new TextView(this);
        view.setTextColor(Color.WHITE);
        view.setTextSize(13);
        view.setPadding(0, dp(10), 0, 0);
        return view;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    /**
     * Size the preview to the largest 16:9 area left of the panel, so all of the recorded frame is visible for lining
     * up the corners, and rotate the sensor's frames to match the display.
     */
    private void fitPreview() {
        View parent = (View) preview.getParent();
        int available = parent.getWidth() - dp(PANE_DP);
        int height = parent.getHeight();
        if (height == 0 || available <= 0) {
            return;
        }
        int width = height * Recorder.WIDTH / Recorder.HEIGHT;
        if (width > available) {
            width = available;
            height = width * Recorder.HEIGHT / Recorder.WIDTH;
        }
        ViewGroup.LayoutParams params = preview.getLayoutParams();
        if (params.width != width || params.height != height) {
            params.width = width;
            params.height = height;
            preview.setLayoutParams(params);
            return;
        }
        int rotation = getDisplay().getRotation();
        Matrix matrix = new Matrix();
        RectF view = new RectF(0, 0, width, height);
        RectF buffer = new RectF(0, 0, Recorder.HEIGHT, Recorder.WIDTH);
        float cx = view.centerX();
        float cy = view.centerY();
        if (rotation == Surface.ROTATION_90 || rotation == Surface.ROTATION_270) {
            buffer.offset(cx - buffer.centerX(), cy - buffer.centerY());
            matrix.setRectToRect(view, buffer, Matrix.ScaleToFit.FILL);
            float scale = Math.max((float) height / Recorder.HEIGHT, (float) width / Recorder.WIDTH);
            matrix.postScale(scale, scale, cx, cy);
            matrix.postRotate(90 * (rotation - 2), cx, cy);
        } else if (rotation == Surface.ROTATION_180) {
            matrix.postRotate(180, cx, cy);
        }
        preview.setTransform(matrix);
    }

    // -----------------------------------------------------------------------------------------------------------------
    // Recording

    private void applyHost(String host) {
        if (host.isEmpty()) {
            clockSync.setHost(null);
            return;
        }
        new Thread(() -> {
            try {
                clockSync.setHost(InetAddress.getByName(host));
            } catch (java.net.UnknownHostException e) {
                ui.post(() -> syncText.setText("Unknown host " + host));
            }
        }, "resolve").start();
    }

    private void toggleRecording() {
        if (recorder.isRecording()) {
            recorder.stopRecording();
            recordButton.setEnabled(false);
            recordButton.setText("Saving…");
            setRecordColor(false);
            return;
        }
        if (pendingName != null) {
            // Recording again keeps the last one on the phone without uploading it
            recordText.setText("Kept " + pendingName + " on the phone without uploading it.");
            clearPending();
        }
        String test = nameField.getText().toString().trim().replaceAll("[^A-Za-z0-9._-]+", "-");
        prefs.edit().putString("name", nameField.getText().toString().trim()).apply();
        String stamp = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date());
        recorder.startRecording((test.isEmpty() ? "latency" : test) + "_" + stamp);
        recordButton.setText(String.format(Locale.US, "Stop · %d s left", AUTO_STOP_S));
        setRecordColor(true);
    }

    private void setRecordColor(boolean recording) {
        recordButton.setBackgroundTintList(recording ? ColorStateList.valueOf(RED) : recordTint);
        recordButton.setTextColor(recording ? Color.WHITE : Color.BLACK);
    }

    private void clearPending() {
        pendingName = null;
        pendingVideo = null;
        pendingTiming = null;
        decisionRow.setVisibility(View.GONE);
    }

    private void uploadPending() {
        if (pendingName == null) {
            return;
        }
        String name = pendingName;
        Uri video = pendingVideo;
        Uri timing = pendingTiming;
        clearPending();
        recordText.setText("Sent " + name + " to the host; see Results.");
        startUpload(name, video, timing);
    }

    private void discardPending() {
        if (pendingName == null) {
            return;
        }
        String name = pendingName;
        int deleted = 0;
        for (Uri uri : new Uri[] {pendingVideo, pendingTiming}) {
            if (uri != null) {
                try {
                    deleted += getContentResolver().delete(uri, null, null);
                } catch (RuntimeException ignored) {
                    // Already gone
                }
            }
        }
        clearPending();
        recordText.setText(deleted > 0 ? "Discarded " + name + "." : "Could not delete " + name + "; it may already be gone.");
    }

    private void startUpload(String name, Uri video, Uri timing) {
        InetAddress host = clockSync.getHost();
        uploadName = name;
        uploadVideo = video;
        uploadTiming = timing;
        reportUrl = null;
        savedReport = null;
        reportButton.setVisibility(View.GONE);
        retryButton.setVisibility(View.GONE);
        resultTitle.setText(name);
        resultText.setTextColor(Color.WHITE);
        showTab(false);
        if (host == null) {
            resultText.setText("No host found to upload to. Check that latency-test.exe is running, or type its address on the Recording tab.");
            resultText.setTextColor(AMBER);
            retryButton.setVisibility(View.VISIBLE);
            return;
        }
        resultText.setText("Uploading…");
        uploader.start(host, clockSync.getPort(), name, video, timing);
    }

    // -----------------------------------------------------------------------------------------------------------------
    // Lifecycle

    @Override
    protected void onResume() {
        super.onResume();
        resumed = true;
        hideSystemBars();
        wifiLock.acquire();
        clockSync.start();
        ui.post(statusTick);
        ui.post(scanTick);
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.CAMERA}, CAMERA_REQUEST);
        } else {
            openCameraIfReady();
        }
    }

    @Override
    protected void onPause() {
        resumed = false;
        ui.removeCallbacks(statusTick);
        ui.removeCallbacks(scanTick);
        closeCamera();
        clockSync.stop();
        if (wifiLock.isHeld()) {
            wifiLock.release();
        }
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        recorder.release();
        scanner.release();
        super.onDestroy();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        if (requestCode == CAMERA_REQUEST && results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            openCameraIfReady();
        } else {
            cameraText.setText("The camera permission is needed to record.");
        }
    }

    private void closeCamera() {
        cancelAutoStart();
        if (cameraOpen) {
            recorder.close();
            cameraOpen = false;
        }
        updateCameraPlaceholder();
        recordButton.setEnabled(false);
        recordButton.setText("Record");
        setRecordColor(false);
    }

    private void openCameraIfReady() {
        if (!resumed || !onRecordingTab || cameraOpen || !preview.isAvailable() || checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            return;
        }
        // Wait for the preview's final size: resizing it after the session exists would change its buffers' size
        ViewGroup.LayoutParams params = preview.getLayoutParams();
        if (params.width <= 0 || preview.getWidth() != params.width || preview.getHeight() != params.height) {
            return;
        }
        cameraOpen = true;
        updateCameraPlaceholder();
        recorder.open(preview.getSurfaceTexture());
    }

    private void hideSystemBars() {
        WindowInsetsController controller = getWindow().getInsetsController();
        if (controller != null) {
            controller.hide(WindowInsets.Type.systemBars());
            controller.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
        }
    }

    private final Runnable statusTick = new Runnable() {
        @Override
        public void run() {
            ClockSync.Status s = clockSync.status();
            if (s.samples == 0) {
                syncText.setText(s.host == null ? "Sync: looking for the host…" : "Sync: no answer from " + s.host);
                syncText.setTextColor(AMBER);
            } else {
                syncText.setText(String.format(Locale.US, "Sync: %s\nfastest round trip %.2f ms\noffset wander %.3f ms over 10 s",
                    s.host, s.minRttMs, s.offsetSpreadMs));
                syncText.setTextColor(s.minRttMs < 5 && s.offsetSpreadMs < 0.75 ? GREEN : AMBER);
            }
            if (recorder.isRecording()) {
                double seconds = (SystemClock.elapsedRealtimeNanos() - recorder.recordingStartRealtimeNs()) / 1e9;
                int frames = recorder.framesRecorded();
                recordText.setText(String.format(Locale.US, "Recording: %d frames", frames));
                // The button is disabled once stopping, while the recording is saved
                if (recordButton.isEnabled()) {
                    if (seconds >= AUTO_STOP_S) {
                        toggleRecording();
                    } else {
                        recordButton.setText(String.format(Locale.US, "Stop · %d s left", (int) Math.ceil(AUTO_STOP_S - seconds)));
                    }
                }
            }
            ui.postDelayed(this, 250);
        }
    };

    // -----------------------------------------------------------------------------------------------------------------
    // Starting by itself from latency-test's QR code

    private final Runnable scanTick = new Runnable() {
        @Override
        public void run() {
            if (canScan()) {
                scanner.scan(preview);
            } else if (autoStart == AutoStart.SCANNING) {
                showScanText(null);
                steadySinceMs = 0;
            }
            ui.postDelayed(this, SCAN_INTERVAL_MS);
        }
    };

    private final Runnable countdownTick = new Runnable() {
        @Override
        public void run() {
            if (autoStart != AutoStart.COUNTDOWN) {
                return;
            }
            if (countdown > 0) {
                countdownText.setText(String.valueOf(countdown--));
                ui.postDelayed(this, 1000);
                return;
            }
            boolean ready = recordButton.isEnabled() && !recorder.isRecording();
            cancelAutoStart();
            if (ready) {
                toggleRecording();
            }
        }
    };

    private boolean canScan() {
        // A finished recording waits for Upload or Discard first; recording again would quietly set it aside
        return autoStart == AutoStart.SCANNING && cameraOpen && onRecordingTab && preview.isAvailable()
            && recordButton.isEnabled() && !recorder.isRecording() && pendingName == null;
    }

    private void onScan(QrScanner.Scan scan) {
        if (!canScan()) {
            return;
        }
        SessionLink link = scan == null ? null : SessionLink.parse(scan.text);
        if (link == null) {
            if (++scanMisses >= SCAN_MISSES_TO_CLEAR) {
                steadySinceMs = 0;
                showScanText(null);
            }
            return;
        }
        scanMisses = 0;
        useHostFrom(link);

        Framing.Result framing = Framing.check(scan.finders, scan.modules, link.width, link.height, scan.frameWidth, scan.frameHeight);
        if (framing.advice != null) {
            steadySinceMs = 0;
            showScanText(framing.advice);
            return;
        }
        ClockSync.Status sync = clockSync.status();
        if (sync.samples < MIN_SYNC_SAMPLES) {
            steadySinceMs = 0;
            showScanText("Waiting for clock sync…");
            return;
        }
        if (sync.minRttMs >= MAX_SYNC_ROUND_TRIP_MS) {
            steadySinceMs = 0;
            showScanText("Clock sync is too slow to start by itself. Check the network, or press Record.");
            return;
        }

        long now = SystemClock.elapsedRealtime();
        double tolerance = STEADY_TOLERANCE * Math.max(scan.frameWidth, scan.frameHeight);
        if (steadySinceMs == 0 || Math.hypot(framing.centerX - steadyX, framing.centerY - steadyY) > tolerance) {
            steadySinceMs = now;
            steadyX = framing.centerX;
            steadyY = framing.centerY;
        }
        if (now - steadySinceMs < STEADY_MS) {
            showScanText("Hold still…");
            return;
        }

        autoStart = AutoStart.ASKING_HOST;
        showScanText("Starting…");
        askHostForPattern(this::onHostAnswered);
    }

    private void onHostAnswered(boolean showing) {
        if (autoStart != AutoStart.ASKING_HOST) {
            return;  // Cancelled since
        }
        if (!showing) {
            autoStart = AutoStart.SCANNING;
            steadySinceMs = 0;
            showScanText("Couldn't reach the host. Check that the firewall allows latency-test.");
            return;
        }
        autoStart = AutoStart.COUNTDOWN;
        countdown = COUNTDOWN_S;
        countdownText.setVisibility(View.VISIBLE);
        showScanText("Tap to cancel");
        countdownTick.run();
    }

    private void cancelAutoStart() {
        ui.removeCallbacks(countdownTick);
        autoStart = AutoStart.SCANNING;
        steadySinceMs = 0;
        countdownText.setVisibility(View.GONE);
        showScanText(null);
    }

    private void showScanText(String text) {
        scanText.setText(text);
        scanText.setVisibility(text == null ? View.GONE : View.VISIBLE);
    }

    /** Point the clock sync at the address in a link, unless it is already talking to one of the link's addresses. */
    private void useHostFrom(SessionLink link) {
        clockSync.setPort(link.port);
        InetAddress current = clockSync.getHost();
        if (current != null && link.hosts.contains(current.getHostAddress())) {
            return;
        }
        List<String> addresses = new ArrayList<>();
        List<Integer> prefixes = new ArrayList<>();
        ConnectivityManager connectivity = getSystemService(ConnectivityManager.class);
        LinkProperties network = connectivity.getLinkProperties(connectivity.getActiveNetwork());
        if (network != null) {
            for (LinkAddress address : network.getLinkAddresses()) {
                if (address.getAddress() instanceof Inet4Address) {
                    addresses.add(address.getAddress().getHostAddress());
                    prefixes.add(address.getPrefixLength());
                }
            }
        }
        String host = link.chooseHost(addresses, prefixes);
        if (host != null) {
            hostField.setText(host);
            prefs.edit().putString("host", host).apply();
            applyHost(host);
        }
    }

    /** Told on the main thread whether the host took the request. */
    private interface PatternCallback {
        void onAnswer(boolean showing);
    }

    /**
     * Ask the host to leave its QR code for the test pattern.
     *
     * @param callback Told how it went; null not to care.
     */
    private void askHostForPattern(PatternCallback callback) {
        InetAddress host = clockSync.getHost();
        int port = clockSync.getPort();
        new Thread(() -> {
            boolean showing = false;
            if (host != null) {
                try {
                    HttpURLConnection connection = (HttpURLConnection) new URL("http", host.getHostAddress(), port, "/pattern").openConnection();
                    connection.setConnectTimeout(HTTP_TIMEOUT_MS);
                    connection.setReadTimeout(HTTP_TIMEOUT_MS);
                    connection.setRequestMethod("POST");
                    connection.setDoOutput(true);
                    connection.setFixedLengthStreamingMode(0);
                    showing = connection.getResponseCode() == HttpURLConnection.HTTP_OK;
                    connection.disconnect();
                } catch (IOException e) {
                    // Reported as not showing
                }
            }
            if (callback != null) {
                boolean answer = showing;
                ui.post(() -> callback.onAnswer(answer));
            }
        }, "ask-host").start();
    }

    // -----------------------------------------------------------------------------------------------------------------
    // Recorder and uploader callbacks

    @Override
    public void onReady(String description) {
        ui.post(() -> {
            if (!cameraOpen) {
                return;  // Closed since, e.g. by leaving the Recording tab
            }
            cameraText.setText(description);
            recordButton.setEnabled(true);
            recordButton.setText("Record");
            setRecordColor(false);
        });
    }

    @Override
    public void onError(String message) {
        ui.post(() -> {
            cameraText.setText(message);
            boolean recording = recorder.isRecording();
            recordButton.setText(recording ? "Stop" : "Record");
            setRecordColor(recording);
        });
    }

    @Override
    public void onClosed(String reason) {
        ui.post(() -> {
            cameraOpen = false;
            updateCameraPlaceholder();
            cameraText.setText(reason);
            recordButton.setEnabled(false);
            recordButton.setText("Record");
            setRecordColor(false);
            // Try again in a moment, in case it was only taken briefly
            ui.postDelayed(this::openCameraIfReady, 2000);
        });
    }

    @Override
    public void onSaved(String name, int frames, Uri video, Uri timing) {
        ui.post(() -> {
            pendingName = name;
            pendingVideo = video;
            pendingTiming = timing;
            recordText.setText(String.format(Locale.US, "Saved %s (%d frames, %.1f s) in Download/LatencyCam.", name, frames, frames / (double) Recorder.FPS));
            decisionRow.setVisibility(timing != null ? View.VISIBLE : View.GONE);
        });
    }

    private final Uploader.Listener uploadListener = new Uploader.Listener() {
        @Override
        public void onProgress(String text) {
            ui.post(() -> resultText.setText(text));
        }

        @Override
        public void onDone(JSONObject summary, Uri report, String url) {
            ui.post(() -> {
                String text = Uploader.describe(summary);
                text += report != null ? "\n\nReport saved in Download/LatencyCam." : "\n\nThe report couldn't be saved; it opens from the host while latency-test runs.";
                resultText.setText(text);
                reportUrl = url;
                savedReport = report;
                reportButton.setVisibility(View.VISIBLE);
            });
        }

        @Override
        public void onFailed(String message) {
            ui.post(() -> {
                resultText.setText(message);
                resultText.setTextColor(AMBER);
                retryButton.setVisibility(View.VISIBLE);
            });
        }
    };
}
