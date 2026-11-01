package ca.mlsoft.latencycam;

import android.Manifest;
import android.app.Activity;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.RectF;
import android.graphics.SurfaceTexture;
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
import android.widget.LinearLayout;
import android.widget.TextView;

import java.net.InetAddress;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Films the client display at 240 fps while keeping the phone's clock synced with the host, so the analyzer can
 * time what the camera saw on the host's clock.
 */
public class MainActivity extends Activity implements Recorder.Listener {
    private static final int CAMERA_REQUEST = 1;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ClockSync clockSync = new ClockSync();
    private Recorder recorder;
    private TextureView preview;
    private EditText nameField;
    private EditText hostField;
    private Button recordButton;
    private TextView cameraText;
    private TextView syncText;
    private TextView recordText;
    private WifiManager.WifiLock wifiLock;
    private SharedPreferences prefs;
    private boolean cameraOpen;
    private boolean resumed;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("latency-cam", MODE_PRIVATE);
        recorder = new Recorder(this, clockSync, this);
        buildViews();
        WifiManager wifi = getSystemService(WifiManager.class);
        wifiLock = wifi.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "latency-cam");
        applyHost(prefs.getString("host", ""));
    }

    private void buildViews() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        preview = new TextureView(this);
        root.addView(preview, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT, Gravity.START));
        preview.setSurfaceTextureListener(new TextureView.SurfaceTextureListener() {
            @Override
            public void onSurfaceTextureAvailable(SurfaceTexture texture, int width, int height) {
                fitPreview();
                openCameraIfReady();
            }

            @Override
            public void onSurfaceTextureSizeChanged(SurfaceTexture texture, int width, int height) {
                fitPreview();
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

        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(16), dp(12), dp(16), dp(12));
        panel.setBackgroundColor(0xCC000000);
        FrameLayout.LayoutParams panelParams = new FrameLayout.LayoutParams(dp(300), ViewGroup.LayoutParams.MATCH_PARENT, Gravity.END);
        root.addView(panel, panelParams);

        nameField = field("Test name, e.g. amf-av1-4ktv", InputType.TYPE_CLASS_TEXT);
        nameField.setText(prefs.getString("name", ""));
        panel.addView(label("Test name"));
        panel.addView(nameField);

        hostField = field("Host IP, blank to find it", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        hostField.setText(prefs.getString("host", ""));
        hostField.setImeOptions(EditorInfo.IME_ACTION_DONE);
        hostField.setOnEditorActionListener((v, actionId, event) -> {
            String host = hostField.getText().toString().trim();
            prefs.edit().putString("host", host).apply();
            applyHost(host);
            return false;
        });
        panel.addView(label("Host time server"));
        panel.addView(hostField);

        recordButton = new Button(this);
        recordButton.setText("Record");
        recordButton.setEnabled(false);
        recordButton.setOnClickListener(v -> toggleRecording());
        LinearLayout.LayoutParams buttonParams = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(64));
        buttonParams.topMargin = dp(12);
        panel.addView(recordButton, buttonParams);

        syncText = status();
        recordText = status();
        cameraText = status();
        panel.addView(syncText);
        panel.addView(recordText);
        panel.addView(cameraText);

        setContentView(root);
    }

    private EditText field(String hint, int inputType) {
        EditText field = new EditText(this);
        field.setHint(hint);
        field.setSingleLine(true);
        field.setInputType(inputType);
        field.setTextColor(Color.WHITE);
        field.setHintTextColor(0xFF8A8984);
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

    /** Size the preview to 16:9 at the left of the screen, and rotate the sensor's frames to match the display. */
    private void fitPreview() {
        View parent = (View) preview.getParent();
        int height = parent.getHeight();
        int width = height * Recorder.WIDTH / Recorder.HEIGHT;
        if (height == 0) {
            return;
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
            return;
        }
        String test = nameField.getText().toString().trim().replaceAll("[^A-Za-z0-9._-]+", "-");
        prefs.edit().putString("name", nameField.getText().toString().trim()).apply();
        String stamp = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date());
        recorder.startRecording((test.isEmpty() ? "latency" : test) + "_" + stamp);
        recordButton.setText("Stop");
    }

    @Override
    protected void onResume() {
        super.onResume();
        resumed = true;
        hideSystemBars();
        wifiLock.acquire();
        clockSync.start();
        ui.post(statusTick);
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
        if (cameraOpen) {
            recorder.close();
            cameraOpen = false;
        }
        recordButton.setEnabled(false);
        recordButton.setText("Record");
        clockSync.stop();
        if (wifiLock.isHeld()) {
            wifiLock.release();
        }
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        recorder.release();
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

    private void openCameraIfReady() {
        if (!resumed || cameraOpen || !preview.isAvailable() || checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            return;
        }
        cameraOpen = true;
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
                syncText.setText(s.host == null ? "Sync: looking for the host's time server…" : "Sync: no answer from " + s.host);
                syncText.setTextColor(0xFFFAB219);
            } else {
                syncText.setText(String.format(Locale.US, "Sync: %s\nfastest round trip %.2f ms\noffset wander %.3f ms over 10 s (%d samples)",
                    s.host, s.minRttMs, s.offsetSpreadMs, s.samples));
                syncText.setTextColor(s.minRttMs < 5 && s.offsetSpreadMs < 0.5 ? 0xFF0CA30C : 0xFFFAB219);
            }
            if (recorder.isRecording()) {
                double seconds = (SystemClock.elapsedRealtimeNanos() - recorder.recordingStartRealtimeNs()) / 1e9;
                int frames = recorder.framesRecorded();
                recordText.setText(String.format(Locale.US, "Recording %.1f s, %d frames (%.0f fps)", seconds, frames, seconds > 0.5 ? frames / seconds : 0.0));
            }
            ui.postDelayed(this, 500);
        }
    };

    @Override
    public void onReady(String description) {
        ui.post(() -> {
            cameraText.setText(description);
            recordButton.setEnabled(true);
            recordButton.setText("Record");
        });
    }

    @Override
    public void onError(String message) {
        ui.post(() -> {
            cameraText.setText(message);
            recordButton.setText(recorder.isRecording() ? "Stop" : "Record");
        });
    }

    @Override
    public void onClosed(String reason) {
        ui.post(() -> {
            cameraOpen = false;
            cameraText.setText(reason);
            recordButton.setEnabled(false);
            recordButton.setText("Record");
            // Try again in a moment, in case it was only taken briefly
            ui.postDelayed(this::openCameraIfReady, 2000);
        });
    }

    @Override
    public void onSaved(String name, int frames) {
        ui.post(() -> recordText.setText("Saved Download/LatencyCam/" + name + ".mp4 and .camera.json, " + frames + " frames"));
    }
}
