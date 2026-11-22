package ca.mlsoft.latencycam;

import android.app.Activity;
import android.graphics.Insets;
import android.net.Uri;
import android.os.Bundle;
import android.view.WindowInsets;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.FrameLayout;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

/**
 * Shows a report saved on the phone. Reports are single HTML files with their images embedded, so they need nothing
 * from the host.
 */
public class ReportActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        WebView view = new WebView(this);
        WebSettings settings = view.getSettings();
        // The charts' tooltips use a little JavaScript
        settings.setJavaScriptEnabled(true);
        settings.setBuiltInZoomControls(true);
        settings.setDisplayZoomControls(false);
        // Apps are drawn edge to edge, so keep the report clear of the system bars and any camera cutout. A web
        // view doesn't keep its page inside its own padding, so it goes in a padded frame.
        FrameLayout frame = new FrameLayout(this);
        frame.setOnApplyWindowInsetsListener((v, insets) -> {
            Insets safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            v.setPadding(safe.left, safe.top, safe.right, safe.bottom);
            return insets;
        });
        frame.addView(view);
        setContentView(frame);

        Uri report = getIntent().getData();
        String html;
        try {
            html = read(report);
        } catch (IOException | RuntimeException e) {
            html = "<p style='font-family:sans-serif'>Could not open the report: " + e.getMessage() + "</p>";
        }
        view.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    private String read(Uri uri) throws IOException {
        if (uri == null) {
            throw new IOException("no report given");
        }
        try (InputStream in = getContentResolver().openInputStream(uri)) {
            if (in == null) {
                throw new IOException("it is missing");
            }
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[64 * 1024];
            int read;
            while ((read = in.read(buffer)) > 0) {
                out.write(buffer, 0, read);
            }
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }
}
