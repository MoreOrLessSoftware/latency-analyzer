package ca.mlsoft.latencycam;

import android.graphics.Bitmap;
import android.os.Handler;
import android.os.Looper;
import android.view.TextureView;

import com.google.zxing.BarcodeFormat;
import com.google.zxing.DecodeHintType;
import com.google.zxing.ReaderException;
import com.google.zxing.RGBLuminanceSource;
import com.google.zxing.ResultPoint;
import com.google.zxing.common.BitMatrix;
import com.google.zxing.common.DecoderResult;
import com.google.zxing.common.DetectorResult;
import com.google.zxing.common.HybridBinarizer;
import com.google.zxing.qrcode.decoder.Decoder;
import com.google.zxing.qrcode.detector.Detector;

import java.util.EnumMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Reads QR codes from the camera preview. The high speed camera session only allows the preview and encoder as
 * outputs, so frames come from the preview's TextureView rather than from an ImageReader.
 */
final class QrScanner {
    /** A portrait frame: the view's own transform is what turns the sensor's image upright, and grabs ignore it. */
    private static final int FRAME_WIDTH = Recorder.HEIGHT * 2 / 3;
    private static final int FRAME_HEIGHT = Recorder.WIDTH * 2 / 3;

    /** A code found in a frame. */
    static final class Scan {
        final String text;
        final int modules;
        final float[] finders;
        final int frameWidth = FRAME_WIDTH;
        final int frameHeight = FRAME_HEIGHT;

        /**
         * @param text What the code says.
         * @param modules Modules along one side of the code.
         * @param finders Finder pattern centres: bottom left x, y, top left x, y, top right x, y.
         */
        Scan(String text, int modules, float[] finders) {
            this.text = text;
            this.modules = modules;
            this.finders = finders;
        }
    }

    /** Told about each frame scanned, on the main thread. */
    interface Listener {
        /** @param scan The code found, or null when the frame had none. */
        void onScan(Scan scan);
    }

    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Listener listener;
    private final int[] pixels = new int[FRAME_WIDTH * FRAME_HEIGHT];
    private final Map<DecodeHintType, Object> hints = new EnumMap<>(DecodeHintType.class);
    private boolean busy;

    QrScanner(Listener listener) {
        this.listener = listener;
        hints.put(DecodeHintType.POSSIBLE_FORMATS, List.of(BarcodeFormat.QR_CODE));
    }

    /**
     * Scan the frame now in a preview, unless the last one is still being read. Call on the main thread.
     *
     * @param preview The camera preview.
     */
    void scan(TextureView preview) {
        if (busy) {
            return;
        }
        Bitmap frame = preview.getBitmap(FRAME_WIDTH, FRAME_HEIGHT);
        if (frame == null) {
            return;
        }
        busy = true;
        worker.execute(() -> {
            frame.getPixels(pixels, 0, FRAME_WIDTH, 0, 0, FRAME_WIDTH, FRAME_HEIGHT);
            frame.recycle();
            Scan scan = read();
            ui.post(() -> {
                busy = false;
                listener.onScan(scan);
            });
        });
    }

    void release() {
        worker.shutdown();
    }

    private Scan read() {
        try {
            BitMatrix image = new HybridBinarizer(new RGBLuminanceSource(FRAME_WIDTH, FRAME_HEIGHT, pixels)).getBlackMatrix();
            // Reading in two steps, rather than with QRCodeReader, also gives the code's size
            DetectorResult detected = new Detector(image).detect(hints);
            DecoderResult decoded = new Decoder().decode(detected.getBits(), hints);
            ResultPoint[] points = detected.getPoints();
            float[] finders = {points[0].getX(), points[0].getY(), points[1].getX(), points[1].getY(), points[2].getX(), points[2].getY()};
            return new Scan(decoded.getText(), detected.getBits().getHeight(), finders);
        } catch (ReaderException e) {
            return null;
        }
    }
}
