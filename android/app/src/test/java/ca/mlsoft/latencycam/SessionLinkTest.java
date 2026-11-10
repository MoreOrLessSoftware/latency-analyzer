package ca.mlsoft.latencycam;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.List;

public class SessionLinkTest {
    private static final String BASE = "https://moreorlesssoftware.github.io/c#";

    @Test
    public void readsWhatLatencyTestWrites() {
        SessionLink link = SessionLink.parse(BASE + "h=192.168.1.20,10.0.0.5&p=48100&s=3840x2160");
        assertEquals(List.of("192.168.1.20", "10.0.0.5"), link.hosts);
        assertEquals(48100, link.port);
        assertEquals(3840, link.width);
        assertEquals(2160, link.height);
    }

    @Test
    public void keepsOnlyPrivateAddresses() {
        SessionLink link = SessionLink.parse(BASE + "h=8.8.8.8,172.15.0.1,172.16.0.1,172.32.0.1,not.an.address.x,10.1.2.3&p=48100&s=1920x1080");
        assertEquals(List.of("172.16.0.1", "10.1.2.3"), link.hosts);
    }

    @Test
    public void acceptsNoAddresses() {
        assertTrue(SessionLink.parse(BASE + "h=&p=48100&s=1920x1080").hosts.isEmpty());
    }

    @Test
    public void rejectsOtherText() {
        assertNull(SessionLink.parse(null));
        assertNull(SessionLink.parse("https://example.com/c#h=10.0.0.1&p=48100&s=1920x1080"));
        assertNull(SessionLink.parse(BASE + "h=10.0.0.1&p=99999&s=1920x1080"));
        assertNull(SessionLink.parse(BASE + "h=10.0.0.1&p=48100"));
        assertNull(SessionLink.parse(BASE + "h=10.0.0.1&p=48100&s=wide"));
        assertNull(SessionLink.parse(BASE + "garbage"));
    }

    @Test
    public void prefersAnAddressOnThePhonesNetwork() {
        SessionLink link = SessionLink.parse(BASE + "h=10.21.93.18,10.0.0.29,172.27.144.1&p=48100&s=1920x1080");
        assertEquals("10.0.0.29", link.chooseHost(List.of("10.0.0.222"), List.of(24)));
        assertEquals("10.21.93.18", link.chooseHost(List.of("192.168.5.5"), List.of(24)));
        assertEquals("10.21.93.18", link.chooseHost(List.of(), List.of()));
        assertNull(SessionLink.parse(BASE + "h=&p=48100&s=1920x1080").chooseHost(List.of("10.0.0.222"), List.of(24)));
    }
}
