package ca.mlsoft.latencycam;

/**
 * Works out from the QR code on latency-test's first screen where the rest of the screen is in the camera's view. The
 * analyzer needs all four corner bullseyes, and the code's position on the screen is fixed, so its three finder
 * patterns are enough to tell whether the whole screen is in shot before the recording starts.
 */
final class Framing {
    private static final int QUIET_MODULES = 4;
    /** Finder pattern centres are this many modules in from the code's edge. */
    private static final double FINDER_INSET = 3.5;
    /** Keep the screen this far from the edge of the frame, as a fraction of the frame's long side. */
    private static final double MARGIN = 0.02;
    /** The screen's longest side has to span at least this much of the frame, or the patches get too small to read. */
    private static final double MIN_FILL = 0.5;

    /** The result of a check. */
    static final class Result {
        /** What the user should do, or null when the screen is framed well. */
        final String advice;
        final double centerX;
        final double centerY;

        Result(String advice, double centerX, double centerY) {
            this.advice = advice;
            this.centerX = centerX;
            this.centerY = centerY;
        }
    }

    private Framing() {
    }

    /**
     * Side of one QR module on the screen, as place_qr in app/pattern.h works it out.
     *
     * @param screenWidth Screen width in pixels.
     * @param screenHeight Screen height in pixels.
     * @param modules Modules along one side of the code.
     * @return Pixels per module.
     */
    static int modulePixels(int screenWidth, int screenHeight, int modules) {
        return Math.max(Math.min(screenWidth, screenHeight) / 2 / (modules + 2 * QUIET_MODULES), 1);
    }

    /**
     * Check how the screen sits in the frame. The frame may be rotated any way relative to the screen: the finder
     * patterns say which way up the code is.
     *
     * @param finders The centres of the finder patterns in frame pixels: bottom left x, y, top left x, y, top right x, y.
     * @param modules Modules along one side of the code.
     * @param screenWidth Screen width in pixels.
     * @param screenHeight Screen height in pixels.
     * @param frameWidth Frame width in pixels.
     * @param frameHeight Frame height in pixels.
     * @return The result.
     */
    static Result check(float[] finders, int modules, int screenWidth, int screenHeight, int frameWidth, int frameHeight) {
        int modulePx = modulePixels(screenWidth, screenHeight, modules);
        int side = (modules + 2 * QUIET_MODULES) * modulePx;
        // Integer division on purpose, to match place_qr
        double codeX = (screenWidth - side) / 2 + QUIET_MODULES * modulePx;
        double codeY = (screenHeight - side) / 2 + QUIET_MODULES * modulePx;

        double span = modules - 2 * FINDER_INSET;
        double acrossX = (finders[4] - finders[2]) / span;
        double acrossY = (finders[5] - finders[3]) / span;
        double downX = (finders[0] - finders[2]) / span;
        double downY = (finders[1] - finders[3]) / span;

        double minX = Double.MAX_VALUE;
        double minY = Double.MAX_VALUE;
        double maxX = -Double.MAX_VALUE;
        double maxY = -Double.MAX_VALUE;
        for (int corner = 0; corner < 4; corner++) {
            double u = ((corner & 1) * screenWidth - codeX) / modulePx - FINDER_INSET;
            double v = ((corner >> 1) * screenHeight - codeY) / modulePx - FINDER_INSET;
            double x = finders[2] + u * acrossX + v * downX;
            double y = finders[3] + u * acrossY + v * downY;
            minX = Math.min(minX, x);
            maxX = Math.max(maxX, x);
            minY = Math.min(minY, y);
            maxY = Math.max(maxY, y);
        }

        double margin = MARGIN * Math.max(frameWidth, frameHeight);
        double centerX = (minX + maxX) / 2;
        double centerY = (minY + maxY) / 2;
        String advice = null;
        if (maxX - minX > frameWidth - 2 * margin || maxY - minY > frameHeight - 2 * margin) {
            advice = "Move back so the whole screen is in view";
        } else if (minX < margin || minY < margin || maxX > frameWidth - margin || maxY > frameHeight - margin) {
            advice = "Centre the screen in the view";
        } else if (Math.max((maxX - minX) / frameWidth, (maxY - minY) / frameHeight) < MIN_FILL) {
            advice = "Move closer so the screen fills more of the view";
        }
        return new Result(advice, centerX, centerY);
    }
}
