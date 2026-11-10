package ca.mlsoft.latencycam;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * What the QR code on latency-test's first screen says: where the host is and how big its screen is. The link looks
 * like https://moreorlesssoftware.github.io/c#h=10.0.0.5,192.168.1.2&p=48100&s=1920x1080 (see qr_url in app/pattern.h).
 */
final class SessionLink {
    private static final String PREFIX = "https://moreorlesssoftware.github.io/c#";

    final List<String> hosts;
    final int port;
    final int width;
    final int height;

    private SessionLink(List<String> hosts, int port, int width, int height) {
        this.hosts = Collections.unmodifiableList(hosts);
        this.port = port;
        this.width = width;
        this.height = height;
    }

    /**
     * Read a link. Only private addresses are kept, so a link can't send recordings off the local network; with none
     * left the host is found by broadcast as usual.
     *
     * @param url The text of the QR code.
     * @return The link, or null when the text isn't one.
     */
    static SessionLink parse(String url) {
        if (url == null || !url.startsWith(PREFIX)) {
            return null;
        }
        List<String> hosts = new ArrayList<>();
        int port = 0;
        int width = 0;
        int height = 0;
        try {
            for (String part : url.substring(PREFIX.length()).split("&")) {
                int equals = part.indexOf('=');
                if (equals < 0) {
                    return null;
                }
                String key = part.substring(0, equals);
                String value = part.substring(equals + 1);
                if (key.equals("h")) {
                    for (String host : value.split(",")) {
                        Long address = parseIpv4(host);
                        if (address != null && isPrivate(address)) {
                            hosts.add(host);
                        }
                    }
                } else if (key.equals("p")) {
                    port = Integer.parseInt(value);
                } else if (key.equals("s")) {
                    int x = value.indexOf('x');
                    width = Integer.parseInt(value.substring(0, x));
                    height = Integer.parseInt(value.substring(x + 1));
                }
            }
        } catch (RuntimeException e) {
            return null;
        }
        if (port <= 0 || port > 65535 || width <= 0 || height <= 0) {
            return null;
        }
        return new SessionLink(hosts, port, width, height);
    }

    /**
     * Pick the address to use: the first on one of this phone's networks, else the first listed.
     *
     * @param ownNetworks This phone's IPv4 addresses, as text.
     * @param prefixLengths The prefix length of each.
     * @return The address, or null when the link lists none.
     */
    String chooseHost(List<String> ownNetworks, List<Integer> prefixLengths) {
        for (String host : hosts) {
            long address = parseIpv4(host);
            for (int i = 0; i < ownNetworks.size(); i++) {
                Long own = parseIpv4(ownNetworks.get(i));
                if (own == null) {
                    continue;
                }
                long mask = (0xFFFFFFFFL << (32 - prefixLengths.get(i))) & 0xFFFFFFFFL;
                if ((address & mask) == (own & mask)) {
                    return host;
                }
            }
        }
        return hosts.isEmpty() ? null : hosts.get(0);
    }

    /**
     * @param text Dotted decimal address.
     * @return The address as a number, or null when the text isn't one.
     */
    private static Long parseIpv4(String text) {
        String[] parts = text.split("\\.", -1);
        if (parts.length != 4) {
            return null;
        }
        long address = 0;
        for (String part : parts) {
            int value;
            try {
                value = Integer.parseInt(part);
            } catch (NumberFormatException e) {
                return null;
            }
            if (value < 0 || value > 255) {
                return null;
            }
            address = address << 8 | value;
        }
        return address;
    }

    /** @return Whether an address is in 10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16. */
    private static boolean isPrivate(long address) {
        return (address >> 24) == 10 || (address >> 20) == 0xAC1 || (address >> 16) == 0xC0A8;
    }
}
