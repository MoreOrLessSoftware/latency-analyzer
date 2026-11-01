package ca.mlsoft.latencycam;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.provider.MediaStore;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

/**
 * Hands a recording to latency-test.exe on the host: uploads the video and its timing file, starts the analysis
 * and waits for its result. See app/http.h on the host side for the API.
 */
final class Uploader {
    private static final int CHUNK = 256 * 1024;
    private static final long ANALYSIS_TIMEOUT_MS = 10 * 60 * 1000;

    /** Progress and results, called on the upload thread. */
    interface Listener {
        void onProgress(String text);

        /**
         * The analysis finished.
         *
         * @param summary The analyzer's summary.
         * @param report The report saved on the phone, or null when it couldn't be downloaded.
         * @param reportUrl The report on the host, while latency-test.exe runs.
         */
        void onDone(JSONObject summary, Uri report, String reportUrl);

        void onFailed(String message);
    }

    private final ContentResolver resolver;
    private final Listener listener;

    Uploader(ContentResolver resolver, Listener listener) {
        this.resolver = resolver;
        this.listener = listener;
    }

    /**
     * Upload and analyze a recording on a background thread.
     *
     * @param host Host running latency-test.exe.
     * @param port Its port.
     * @param name Recording name, without extension.
     * @param video The MP4.
     * @param timing The .camera.json.
     */
    void start(InetAddress host, int port, String name, Uri video, Uri timing) {
        String base = "http://" + host.getHostAddress() + ":" + port + "/recordings/";
        new Thread(() -> {
            try {
                put(base + name + ".camera.json", timing, "application/json", "timing");
                put(base + name + ".mp4", video, "video/mp4", "video");
                listener.onProgress("Starting the analysis on the host…");
                JSONObject status = request("POST", base + name + "/analyze");
                long deadline = SystemClock.elapsedRealtime() + ANALYSIS_TIMEOUT_MS;
                long started = SystemClock.elapsedRealtime();
                while ("analyzing".equals(status.optString("state"))) {
                    if (SystemClock.elapsedRealtime() > deadline) {
                        throw new IOException("the analysis took more than 10 minutes");
                    }
                    listener.onProgress(String.format(Locale.US, "Analyzing on the host… %d s", (SystemClock.elapsedRealtime() - started) / 1000));
                    SystemClock.sleep(1000);
                    status = request("GET", base + name + "/status");
                }
                String state = status.optString("state");
                if ("done".equals(state)) {
                    String reportUrl = "http://" + host.getHostAddress() + ":" + port + status.optString("report");
                    // Keep the report on the phone, so it can be read after latency-test.exe has closed
                    listener.onProgress("Downloading the report…");
                    Uri report = null;
                    try {
                        report = download(reportUrl, name + ".report.html", "text/html");
                    } catch (IOException e) {
                        listener.onProgress("Could not download the report: " + e.getMessage());
                    }
                    listener.onDone(status.optJSONObject("summary"), report, reportUrl);
                } else if ("failed".equals(state)) {
                    listener.onFailed("The analysis failed:\n" + status.optString("output").trim());
                } else {
                    listener.onFailed("Unexpected state from the host: " + state);
                }
            } catch (IOException | JSONException e) {
                listener.onFailed("Upload to " + host.getHostAddress() + " failed: " + e.getMessage());
            }
        }, "upload").start();
    }

    private void put(String url, Uri file, String type, String label) throws IOException {
        long size;
        try (ParcelFileDescriptor fd = resolver.openFileDescriptor(file, "r")) {
            if (fd == null) {
                throw new IOException("cannot open the " + label);
            }
            size = fd.getStatSize();
        }
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        try {
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(60000);
            connection.setRequestMethod("PUT");
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(size);
            connection.setRequestProperty("Content-Type", type);
            long sent = 0;
            long lastReport = 0;
            try (InputStream in = resolver.openInputStream(file); OutputStream out = connection.getOutputStream()) {
                if (in == null) {
                    throw new IOException("cannot read the " + label);
                }
                byte[] buffer = new byte[CHUNK];
                int read;
                while ((read = in.read(buffer)) > 0) {
                    out.write(buffer, 0, read);
                    sent += read;
                    long now = SystemClock.elapsedRealtime();
                    if (now - lastReport > 300) {
                        lastReport = now;
                        listener.onProgress(String.format(Locale.US, "Uploading the %s: %.0f of %.0f MB", label, sent / 1e6, size / 1e6));
                    }
                }
            }
            int code = connection.getResponseCode();
            if (code != 201) {
                throw new IOException("the host answered " + code + " " + readBody(connection));
            }
        } finally {
            connection.disconnect();
        }
    }

    private static JSONObject request(String method, String url) throws IOException, JSONException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        try {
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(30000);
            connection.setRequestMethod(method);
            int code = connection.getResponseCode();
            String body = readBody(connection);
            if (code >= 300) {
                String error = body;
                try {
                    error = new JSONObject(body).optString("error", body);
                } catch (JSONException ignored) {
                    // Not JSON; show it as it is
                }
                throw new IOException(error);
            }
            return new JSONObject(body);
        } finally {
            connection.disconnect();
        }
    }

    /**
     * Download a file into Download/LatencyCam.
     *
     * @param url Where to get it.
     * @param fileName Name to save it as.
     * @param mime Its MIME type.
     * @return The saved file.
     */
    private Uri download(String url, String fileName, String mime) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        try {
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(60000);
            int code = connection.getResponseCode();
            if (code != 200) {
                throw new IOException("the host answered " + code);
            }
            ContentValues values = new ContentValues();
            values.put(MediaStore.MediaColumns.DISPLAY_NAME, fileName);
            values.put(MediaStore.MediaColumns.MIME_TYPE, mime);
            values.put(MediaStore.MediaColumns.RELATIVE_PATH, "Download/LatencyCam");
            values.put(MediaStore.MediaColumns.IS_PENDING, 1);
            Uri uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
            if (uri == null) {
                throw new IOException("could not create " + fileName);
            }
            try (InputStream in = connection.getInputStream(); OutputStream out = resolver.openOutputStream(uri)) {
                if (out == null) {
                    throw new IOException("could not write " + fileName);
                }
                byte[] buffer = new byte[CHUNK];
                int read;
                while ((read = in.read(buffer)) > 0) {
                    out.write(buffer, 0, read);
                }
            } catch (IOException e) {
                resolver.delete(uri, null, null);
                throw e;
            }
            ContentValues done = new ContentValues();
            done.put(MediaStore.MediaColumns.IS_PENDING, 0);
            resolver.update(uri, done, null, null);
            return uri;
        } finally {
            connection.disconnect();
        }
    }

    private static String readBody(HttpURLConnection connection) throws IOException {
        InputStream in = connection.getResponseCode() >= 400 ? connection.getErrorStream() : connection.getInputStream();
        if (in == null) {
            return "";
        }
        try (InputStream stream = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int read;
            while ((read = stream.read(buffer)) > 0) {
                out.write(buffer, 0, read);
            }
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }

    /**
     * The headline numbers of a summary, for the status panel.
     *
     * @param summary The analyzer's summary.json.
     * @return A few lines of text.
     */
    static String describe(JSONObject summary) {
        if (summary == null) {
            return "Done, but the host sent no summary.";
        }
        // "Host present" is the game handing its frame to Windows on the host; "client present" is Moonlight doing so
        // on the client, where Moonlight's own "capture to present" ends
        StringBuilder text = new StringBuilder("Host present → photon, median:");
        JSONObject flips = summary.optJSONObject("flips");
        JSONObject frames = summary.optJSONObject("frames");
        text.append(flips != null ? String.format(Locale.US, "\n  flips %.2f ms (%d)", flips.optDouble("median"), flips.optInt("n")) : "\n  flips: none matched");
        text.append(frames != null ? String.format(Locale.US, "\n  every frame %.2f ms (%d)", frames.optDouble("median"), frames.optInt("n")) : "\n  every frame: none read");
        JSONObject toWhite = summary.optJSONObject("frames_to_white");
        JSONObject toBlack = summary.optJSONObject("frames_to_black");
        if (toWhite != null && toBlack != null) {
            text.append(String.format(Locale.US, "\n    to white %.2f, to black %.2f ms", toWhite.optDouble("median"), toBlack.optDouble("median")));
        }
        // With Sunshine's and Moonlight's logs, the parts in the same words as Moonlight's statistics
        JSONObject pipeline = summary.optJSONObject("pipeline");
        JSONArray stages = pipeline == null ? null : pipeline.optJSONArray("stages");
        if (stages != null) {
            String[][] shown = {
                {"capture -> photon, to white", "Capture → photon, to white"},
                {"capture -> client present (Moonlight's capture to present)", "Capture → client present (Moonlight)"},
                {"client present -> photon, to white", "Client present → photon, to white"},
            };
            for (String[] entry : shown) {
                for (int i = 0; i < stages.length(); i++) {
                    JSONObject stage = stages.optJSONObject(i);
                    if (stage != null && entry[0].equals(stage.optString("label"))) {
                        text.append(String.format(Locale.US, "\n%s: %.2f ms", entry[1], stage.optDouble("median")));
                    }
                }
            }
        }
        JSONObject pacing = summary.optJSONObject("pacing");
        if (pacing != null && pacing.optJSONObject("mismatch") != null) {
            JSONObject mismatch = pacing.optJSONObject("mismatch");
            int pairs = Math.max(pacing.optInt("pairs"), 1);
            text.append(String.format(Locale.US, "\nPacing: client − host sd %.2f ms, %.0f%% uneven", mismatch.optDouble("sd"), 100.0 * pacing.optInt("uneven") / pairs));
            text.append(String.format(Locale.US, "\nFrames not seen: %d of %d", pacing.optInt("not_seen"), pacing.optInt("frames_presented")));
        }
        int notes = summary.optJSONArray("notes") == null ? 0 : summary.optJSONArray("notes").length();
        if (notes > 0) {
            text.append(String.format(Locale.US, "\n%d note%s in the report", notes, notes == 1 ? "" : "s"));
        }
        return text.toString();
    }
}
