package ca.mlsoft.latencycam;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraConstrainedHighSpeedCaptureSession;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.CaptureResult;
import android.hardware.camera2.TotalCaptureResult;
import android.hardware.camera2.params.OutputConfiguration;
import android.hardware.camera2.params.SessionConfiguration;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.media.MediaMuxer;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.provider.MediaStore;
import android.util.Range;
import android.util.Size;
import android.view.Surface;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.Executor;

/**
 * Records high speed video with Camera2 and writes, next to the MP4, the sensor timestamp, exposure and rolling
 * shutter skew of every frame and the clock sync samples taken while recording.
 *
 * Capture results carry SENSOR_TIMESTAMP on the elapsedRealtimeNanos clock when the timestamp source is REALTIME.
 * The camera service moves the timestamps of the buffers it hands to the encoder onto CLOCK_MONOTONIC (System.nanoTime),
 * so the encoder's presentation times are on that clock. The offset between the two clocks is measured at the start
 * and end of each recording, which lets the analyzer match video frame N (the Nth smallest presentation time) to its
 * capture result.
 */
final class Recorder {
    static final int WIDTH = 1920;
    static final int HEIGHT = 1080;
    static final int FPS = 240;
    private static final int BITRATE = 120_000_000;
    private static final long SYNC_TAIL_MS = 2000;
    private static final long SYNC_LEAD_NS = 10_000_000_000L;

    /** Callbacks, made on the camera thread. */
    interface Listener {
        void onReady(String description);

        void onError(String message);

        void onSaved(String name, int frames);
    }

    private final Context context;
    private final ClockSync clockSync;
    private final Listener listener;
    private final HandlerThread cameraThread = new HandlerThread("camera");
    private final HandlerThread codecThread = new HandlerThread("codec");
    private final Handler cameraHandler;
    private final Handler codecHandler;
    private final Executor cameraExecutor;

    private String cameraId;
    private CameraCharacteristics characteristics;
    private CameraDevice device;
    private CameraConstrainedHighSpeedCaptureSession session;
    private Surface previewSurface;
    private MediaCodec codec;
    private Surface codecSurface;

    // Recording state. Captures and video times are touched on the camera and codec threads.
    private volatile boolean recording;
    private volatile boolean finishing;
    private final List<long[]> captures = new ArrayList<>();
    private final List<Long> videoTimesUs = new ArrayList<>();
    private MediaFormat codecFormat;
    private MediaMuxer muxer;
    private int track = -1;
    private Uri videoUri;
    private ParcelFileDescriptor videoFile;
    private String name;
    private long startRealtimeNs;
    private long stopRealtimeNs;
    private long startWallMs;
    private long[] startClocks;
    private long[] stopClocks;

    Recorder(Context context, ClockSync clockSync, Listener listener) {
        this.context = context;
        this.clockSync = clockSync;
        this.listener = listener;
        cameraThread.start();
        codecThread.start();
        cameraHandler = new Handler(cameraThread.getLooper());
        codecHandler = new Handler(codecThread.getLooper());
        cameraExecutor = cameraHandler::post;
    }

    boolean isRecording() {
        return recording;
    }

    int framesRecorded() {
        synchronized (videoTimesUs) {
            return videoTimesUs.size();
        }
    }

    long recordingStartRealtimeNs() {
        return startRealtimeNs;
    }

    /**
     * Open the camera and show its preview on a texture.
     *
     * @param texture Preview texture.
     */
    void open(SurfaceTexture texture) {
        cameraHandler.post(() -> {
            try {
                openOnCameraThread(texture);
            } catch (CameraAccessException | SecurityException | IOException e) {
                listener.onError("Could not open the camera: " + e.getMessage());
            }
        });
    }

    /** Close the camera, finishing a recording first. */
    void close() {
        cameraHandler.post(() -> {
            if (recording) {
                stopOnCameraThread();
            }
            closeSession();
            if (device != null) {
                device.close();
                device = null;
            }
        });
    }

    void release() {
        close();
        cameraThread.quitSafely();
        codecThread.quitSafely();
    }

    /**
     * Start recording to Download/LatencyCam/NAME.mp4 and NAME.camera.json.
     *
     * @param fileName File name without the extension.
     */
    void startRecording(String fileName) {
        cameraHandler.post(() -> {
            if (recording || finishing || session == null) {
                return;
            }
            try {
                name = fileName;
                synchronized (captures) {
                    captures.clear();
                }
                synchronized (videoTimesUs) {
                    videoTimesUs.clear();
                }
                videoUri = insertDownload(name + ".mp4", "video/mp4");
                videoFile = context.getContentResolver().openFileDescriptor(videoUri, "rw");
                muxer = new MediaMuxer(videoFile.getFileDescriptor(), MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
                track = -1;
                startWallMs = System.currentTimeMillis();
                startClocks = clockPair();
                startRealtimeNs = SystemClock.elapsedRealtimeNanos();
                recording = true;
                codecHandler.post(this::startMuxerIfReady);
                repeat(true);
            } catch (IOException | CameraAccessException | RuntimeException e) {
                recording = false;
                listener.onError("Could not start recording: " + e.getMessage());
            }
        });
    }

    void stopRecording() {
        cameraHandler.post(this::stopOnCameraThread);
    }

    private void stopOnCameraThread() {
        if (!recording) {
            return;
        }
        stopRealtimeNs = SystemClock.elapsedRealtimeNanos();
        stopClocks = clockPair();
        try {
            repeat(false);
        } catch (CameraAccessException | RuntimeException ignored) {
            // The encoder is told the stream ended either way
        }
        finishing = true;
        recording = false;
        codec.signalEndOfInputStream();
    }

    /**
     * Read elapsedRealtimeNanos and System.nanoTime close together: the tightest of several readings.
     *
     * @return {realtime ns, monotonic ns, uncertainty ns}.
     */
    private static long[] clockPair() {
        long[] best = null;
        for (int i = 0; i < 20; i++) {
            long before = SystemClock.elapsedRealtimeNanos();
            long monotonic = System.nanoTime();
            long after = SystemClock.elapsedRealtimeNanos();
            if (best == null || after - before < best[2]) {
                best = new long[] {before + (after - before) / 2, monotonic, after - before};
            }
        }
        return best;
    }

    private void openOnCameraThread(SurfaceTexture texture) throws CameraAccessException, IOException {
        CameraManager manager = context.getSystemService(CameraManager.class);
        cameraId = pickCamera(manager);
        if (cameraId == null) {
            listener.onError("No back camera records " + WIDTH + "x" + HEIGHT + " at " + FPS + " fps");
            return;
        }
        characteristics = manager.getCameraCharacteristics(cameraId);
        texture.setDefaultBufferSize(WIDTH, HEIGHT);
        previewSurface = new Surface(texture);
        manager.openCamera(cameraId, cameraExecutor, new CameraDevice.StateCallback() {
            @Override
            public void onOpened(CameraDevice camera) {
                device = camera;
                createSession();
            }

            @Override
            public void onDisconnected(CameraDevice camera) {
                camera.close();
                device = null;
            }

            @Override
            public void onError(CameraDevice camera, int error) {
                camera.close();
                device = null;
                listener.onError("Camera error " + error);
            }
        });
    }

    /**
     * The first back camera that can record WIDTH x HEIGHT at exactly FPS in a high speed session.
     *
     * @param manager Camera manager.
     * @return Camera ID, or null.
     */
    private static String pickCamera(CameraManager manager) throws CameraAccessException {
        for (String id : manager.getCameraIdList()) {
            CameraCharacteristics c = manager.getCameraCharacteristics(id);
            Integer facing = c.get(CameraCharacteristics.LENS_FACING);
            int[] capabilities = c.get(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES);
            StreamConfigurationMap map = c.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            if (facing == null || facing != CameraCharacteristics.LENS_FACING_BACK || capabilities == null || map == null) {
                continue;
            }
            boolean highSpeed = false;
            for (int capability : capabilities) {
                highSpeed |= capability == CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_CONSTRAINED_HIGH_SPEED_VIDEO;
            }
            if (!highSpeed || !Arrays.asList(map.getHighSpeedVideoSizes()).contains(new Size(WIDTH, HEIGHT))) {
                continue;
            }
            for (Range<Integer> range : map.getHighSpeedVideoFpsRangesFor(new Size(WIDTH, HEIGHT))) {
                if (range.getLower() == FPS && range.getUpper() == FPS) {
                    return id;
                }
            }
        }
        return null;
    }

    /** Make a new encoder and a high speed session feeding it and the preview. */
    private void createSession() {
        try {
            createCodec();
        } catch (IOException | RuntimeException e) {
            listener.onError("Could not create the encoder: " + e.getMessage());
            return;
        }
        List<OutputConfiguration> outputs = List.of(new OutputConfiguration(previewSurface), new OutputConfiguration(codecSurface));
        SessionConfiguration config = new SessionConfiguration(SessionConfiguration.SESSION_HIGH_SPEED, outputs, cameraExecutor,
            new CameraCaptureSession.StateCallback() {
                @Override
                public void onConfigured(CameraCaptureSession s) {
                    session = (CameraConstrainedHighSpeedCaptureSession) s;
                    try {
                        repeat(false);
                        listener.onReady(describeCamera());
                    } catch (CameraAccessException | RuntimeException e) {
                        listener.onError("Could not start the preview: " + e.getMessage());
                    }
                }

                @Override
                public void onConfigureFailed(CameraCaptureSession s) {
                    listener.onError("The camera refused a " + WIDTH + "x" + HEIGHT + " " + FPS + " fps session");
                }
            });
        try {
            device.createCaptureSession(config);
        } catch (CameraAccessException | RuntimeException e) {
            listener.onError("Could not create the session: " + e.getMessage());
        }
    }

    private void closeSession() {
        if (session != null) {
            session.close();
            session = null;
        }
        if (codec != null) {
            codec.release();
            codec = null;
        }
        if (codecSurface != null) {
            codecSurface.release();
            codecSurface = null;
        }
    }

    private String describeCamera() {
        Float focal = null;
        float[] focals = characteristics.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS);
        if (focals != null && focals.length > 0) {
            focal = focals[0];
        }
        return "Camera " + cameraId + (focal != null ? String.format(" (%.1f mm)", focal) : "") + ", " + WIDTH + "x" + HEIGHT
            + " at " + FPS + " fps, timestamps " + timestampSource();
    }

    private String timestampSource() {
        Integer source = characteristics.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE);
        return source != null && source == CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME ? "REALTIME" : "UNKNOWN";
    }

    /**
     * Send the repeating burst: preview only, or preview and encoder while recording.
     *
     * @param toEncoder Whether frames go to the encoder.
     */
    private void repeat(boolean toEncoder) throws CameraAccessException {
        CaptureRequest.Builder builder = device.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
        builder.addTarget(previewSurface);
        if (toEncoder) {
            builder.addTarget(codecSurface);
        }
        // A fixed rate while recording; the preview alone may run slower in low light
        builder.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, toEncoder ? new Range<>(FPS, FPS) : new Range<>(30, FPS));
        builder.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO);
        List<CaptureRequest> burst = session.createHighSpeedRequestList(builder.build());
        session.setRepeatingBurst(burst, captureCallback, cameraHandler);
    }

    private final CameraCaptureSession.CaptureCallback captureCallback = new CameraCaptureSession.CaptureCallback() {
        @Override
        public void onCaptureCompleted(CameraCaptureSession s, CaptureRequest request, TotalCaptureResult result) {
            if (!recording) {
                return;
            }
            Long timestamp = result.get(CaptureResult.SENSOR_TIMESTAMP);
            if (timestamp == null) {
                return;
            }
            long[] row = {
                timestamp,
                orZero(result.get(CaptureResult.SENSOR_EXPOSURE_TIME)),
                orZero(result.get(CaptureResult.SENSOR_ROLLING_SHUTTER_SKEW)),
                orZero(result.get(CaptureResult.SENSOR_FRAME_DURATION)),
            };
            synchronized (captures) {
                captures.add(row);
            }
        }
    };

    private static long orZero(Long value) {
        return value == null ? 0 : value;
    }

    private void createCodec() throws IOException {
        MediaFormat format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, WIDTH, HEIGHT);
        format.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        format.setInteger(MediaFormat.KEY_BIT_RATE, BITRATE);
        format.setInteger(MediaFormat.KEY_FRAME_RATE, FPS);
        format.setInteger(MediaFormat.KEY_OPERATING_RATE, FPS);
        format.setInteger(MediaFormat.KEY_PRIORITY, 0);
        format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1);
        // Display order must equal decode order so frame N of the video is the Nth timestamp
        format.setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0);
        codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
        codec.setCallback(codecCallback, codecHandler);
        codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        codecSurface = codec.createInputSurface();
        codecFormat = null;
        codec.start();
    }

    private final MediaCodec.Callback codecCallback = new MediaCodec.Callback() {
        @Override
        public void onInputBufferAvailable(MediaCodec c, int index) {
            // Input comes from the surface
        }

        @Override
        public void onOutputBufferAvailable(MediaCodec c, int index, MediaCodec.BufferInfo info) {
            if (c != codec) {
                return;
            }
            boolean end = (info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
            if ((info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0 && info.size > 0 && track >= 0) {
                ByteBuffer data = c.getOutputBuffer(index);
                muxer.writeSampleData(track, data, info);
                synchronized (videoTimesUs) {
                    videoTimesUs.add(info.presentationTimeUs);
                }
            }
            c.releaseOutputBuffer(index, false);
            if (end) {
                cameraHandler.postDelayed(Recorder.this::finishRecording, SYNC_TAIL_MS);
            }
        }

        @Override
        public void onError(MediaCodec c, MediaCodec.CodecException e) {
            listener.onError("Encoder error: " + e.getMessage());
        }

        @Override
        public void onOutputFormatChanged(MediaCodec c, MediaFormat format) {
            codecFormat = format;
            startMuxerIfReady();
        }
    };

    /** On the codec thread: start the muxer once there is both a file and the encoder's format. */
    private void startMuxerIfReady() {
        if (muxer != null && codecFormat != null && track < 0 && recording) {
            track = muxer.addTrack(codecFormat);
            muxer.start();
        }
    }

    /** On the camera thread, once the encoder has drained and the clock sync has run a little longer. */
    private void finishRecording() {
        int frames = framesRecorded();
        String error = null;
        // The muxer is only used on the codec thread, which has finished with it once the end of stream was seen
        try {
            if (track >= 0) {
                muxer.stop();
            }
            muxer.release();
        } catch (RuntimeException e) {
            error = "Could not finish the video: " + e.getMessage();
        }
        muxer = null;
        try {
            videoFile.close();
        } catch (IOException ignored) {
            // Nothing to do
        }
        ContentValues done = new ContentValues();
        done.put(MediaStore.MediaColumns.IS_PENDING, 0);
        context.getContentResolver().update(videoUri, done, null, null);

        try {
            writeTiming();
        } catch (IOException | JSONException | RuntimeException e) {
            error = "Could not write the timing file: " + e.getMessage();
        }
        finishing = false;
        closeSession();
        if (device != null) {
            createSession();
        }
        if (error != null) {
            listener.onError(error);
        } else {
            listener.onSaved(name, frames);
        }
    }

    private void writeTiming() throws IOException, JSONException {
        JSONObject root = new JSONObject();
        root.put("format", "ml-latency-camera");
        root.put("version", 1);
        root.put("video", name + ".mp4");
        root.put("device", Build.MANUFACTURER + " " + Build.MODEL);
        root.put("android", Build.VERSION.RELEASE);
        root.put("camera_id", cameraId);
        root.put("width", WIDTH);
        root.put("height", HEIGHT);
        root.put("fps", FPS);
        root.put("timestamp_source", timestampSource());
        Integer orientation = characteristics.get(CameraCharacteristics.SENSOR_ORIENTATION);
        root.put("sensor_orientation", orientation == null ? JSONObject.NULL : orientation);
        root.put("start_utc_ms", startWallMs);
        root.put("start_realtime_ns", startRealtimeNs);
        root.put("stop_realtime_ns", stopRealtimeNs);
        root.put("clock_fields", new JSONArray(List.of("realtime_ns", "monotonic_ns", "uncertainty_ns")));
        root.put("clocks_at_start", new JSONArray().put(startClocks[0]).put(startClocks[1]).put(startClocks[2]));
        root.put("clocks_at_stop", new JSONArray().put(stopClocks[0]).put(stopClocks[1]).put(stopClocks[2]));
        root.put("note", "Times are ns of elapsedRealtimeNanos, except video_pts_us, which is us of CLOCK_MONOTONIC "
            + "(add realtime_ns - monotonic_ns from the clock pairs), and the host QPC ticks in sync. Video frame N in "
            + "display order has the Nth smallest video_pts_us.");

        JSONArray pts = new JSONArray();
        synchronized (videoTimesUs) {
            for (long t : videoTimesUs) {
                pts.put(t);
            }
        }
        root.put("video_pts_us", pts);

        root.put("capture_fields", new JSONArray(List.of("sensor_timestamp_ns", "exposure_ns", "rolling_shutter_skew_ns", "frame_duration_ns")));
        JSONArray rows = new JSONArray();
        synchronized (captures) {
            for (long[] row : captures) {
                JSONArray r = new JSONArray();
                for (long v : row) {
                    r.put(v);
                }
                rows.put(r);
            }
        }
        root.put("captures", rows);

        JSONObject sync = new JSONObject();
        java.net.InetAddress host = clockSync.getHost();
        sync.put("host", host == null ? JSONObject.NULL : host.getHostAddress());
        sync.put("qpc_frequency", clockSync.getQpcFrequency());
        sync.put("fields", new JSONArray(List.of("t1_phone_ns", "t2_host_qpc", "t3_host_qpc", "t4_phone_ns")));
        JSONArray samples = new JSONArray();
        for (ClockSync.Sample s : clockSync.samplesBetween(startRealtimeNs - SYNC_LEAD_NS, Long.MAX_VALUE)) {
            samples.put(new JSONArray().put(s.t1).put(s.t2).put(s.t3).put(s.t4));
        }
        sync.put("samples", samples);
        root.put("sync", sync);

        Uri uri = insertDownload(name + ".camera.json", "application/json");
        ContentResolver resolver = context.getContentResolver();
        try (OutputStream out = resolver.openOutputStream(uri)) {
            if (out == null) {
                throw new IOException("no output stream");
            }
            out.write(root.toString().getBytes(StandardCharsets.UTF_8));
        }
        ContentValues done = new ContentValues();
        done.put(MediaStore.MediaColumns.IS_PENDING, 0);
        resolver.update(uri, done, null, null);
    }

    /**
     * Create a pending file in Download/LatencyCam.
     *
     * @param fileName Name with extension.
     * @param mime MIME type.
     * @return The new file's URI.
     */
    private Uri insertDownload(String fileName, String mime) throws IOException {
        ContentValues values = new ContentValues();
        values.put(MediaStore.MediaColumns.DISPLAY_NAME, fileName);
        values.put(MediaStore.MediaColumns.MIME_TYPE, mime);
        values.put(MediaStore.MediaColumns.RELATIVE_PATH, "Download/LatencyCam");
        values.put(MediaStore.MediaColumns.IS_PENDING, 1);
        Uri uri = context.getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
        if (uri == null) {
            throw new IOException("could not create " + fileName);
        }
        return uri;
    }
}
