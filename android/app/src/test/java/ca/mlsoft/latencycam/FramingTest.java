package ca.mlsoft.latencycam;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;

import org.junit.Test;

public class FramingTest {
    private static final int FRAME_WIDTH = 720;
    private static final int FRAME_HEIGHT = 1280;
    private static final int SCREEN_WIDTH = 1920;
    private static final int SCREEN_HEIGHT = 1080;
    private static final int MODULES = 37;
    private static final int MODULE_PX = 12;
    private static final int CODE_X = 690 + 4 * MODULE_PX;  // As place_qr puts it, past the quiet zone
    private static final int CODE_Y = 270 + 4 * MODULE_PX;

    /**
     * The finder centres a frame would see of a screen rotated a quarter turn, as the portrait frames show it.
     *
     * @param scale Frame pixels per screen pixel.
     * @param centerX Where the middle of the screen is in the frame.
     * @param centerY Where the middle of the screen is in the frame.
     * @return The finders, as Framing.check takes them.
     */
    private static float[] finders(double scale, double centerX, double centerY) {
        double near = 3.5 * MODULE_PX;
        double far = (MODULES - 3.5) * MODULE_PX;
        double[][] screen = {{CODE_X + near, CODE_Y + far}, {CODE_X + near, CODE_Y + near}, {CODE_X + far, CODE_Y + near}};
        float[] out = new float[6];
        for (int i = 0; i < 3; i++) {
            double dx = screen[i][0] - SCREEN_WIDTH / 2.0;
            double dy = screen[i][1] - SCREEN_HEIGHT / 2.0;
            out[2 * i] = (float) (centerX - dy * scale);
            out[2 * i + 1] = (float) (centerY + dx * scale);
        }
        return out;
    }

    private static Framing.Result check(float[] finders) {
        return Framing.check(finders, MODULES, SCREEN_WIDTH, SCREEN_HEIGHT, FRAME_WIDTH, FRAME_HEIGHT);
    }

    @Test
    public void modulesMatchPlaceQr() {
        assertEquals(12, Framing.modulePixels(1920, 1080, 37));
        assertEquals(13, Framing.modulePixels(1920, 1200, 37));
        assertEquals(1, Framing.modulePixels(40, 30, 37));
    }

    @Test
    public void acceptsAWellFramedScreen() {
        Framing.Result result = check(finders(0.55, 360, 640));
        assertNull(result.advice);
        assertEquals(360, result.centerX, 1);
        assertEquals(640, result.centerY, 1);
    }

    @Test
    public void asksToMoveBackWhenTheScreenIsTooBig() {
        assertEquals("Move back so the whole screen is in view", check(finders(0.8, 360, 640)).advice);
    }

    @Test
    public void asksToCentreWhenACornerIsOutOfView() {
        assertEquals("Centre the screen in the view", check(finders(0.55, 360, 300)).advice);
    }

    @Test
    public void asksToMoveCloserWhenTheScreenIsSmall() {
        assertEquals("Move closer so the screen fills more of the view", check(finders(0.25, 360, 640)).advice);
    }

    @Test
    public void worksTheOtherWayUp() {
        float[] upright = finders(0.55, 360, 640);
        float[] flipped = new float[6];
        for (int i = 0; i < 3; i++) {
            flipped[2 * i] = FRAME_WIDTH - upright[2 * i];
            flipped[2 * i + 1] = FRAME_HEIGHT - upright[2 * i + 1];
        }
        Framing.Result result = check(flipped);
        assertNull(result.advice);
        assertEquals(360, result.centerX, 1);
    }

    @Test
    public void adviceIsNeverNullForAScreenCutOff() {
        assertNotNull(check(finders(0.55, 100, 640)).advice);
    }
}
