package ca.mlsoft.latencycam;

import android.os.SystemClock;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.List;

/**
 * Measures the offset between this phone's elapsedRealtimeNanos clock, which camera timestamps use, and the host's
 * QueryPerformanceCounter, by exchanging small UDP packets with the host's time server 20 times a second.
 *
 * Request, 16 bytes little endian: magic "MLCS", sequence number, t1 (phone ns when sent).
 * Reply, 40 bytes little endian: magic "MLCR", sequence number, t1 echoed, t2 (host QPC ticks when the request
 * arrived), t3 (host QPC ticks when the reply was sent), QPC ticks per second.
 *
 * Every exchange is kept as a raw sample (t1, t2, t3, t4) so the analyzer can fit the offset and drift itself.
 * Until a host answers, requests go to the broadcast address and the first host that answers is used.
 */
final class ClockSync {
    static final int PORT = 48100;
    static final int REQUEST_MAGIC = 0x53434C4D;  // "MLCS"
    static final int REPLY_MAGIC = 0x52434C4D;  // "MLCR"
    private static final long INTERVAL_NS = 50_000_000L;
    private static final long BROADCAST_INTERVAL_NS = 500_000_000L;
    private static final int MAX_SAMPLES = 200_000;

    /** One exchange: phone ns when sent, host QPC ticks on arrival and reply, phone ns when the reply came back. */
    static final class Sample {
        final long t1;
        final long t2;
        final long t3;
        final long t4;

        Sample(long t1, long t2, long t3, long t4) {
            this.t1 = t1;
            this.t2 = t2;
            this.t3 = t3;
            this.t4 = t4;
        }
    }

    /** Summary of the recent samples for the status line. */
    static final class Status {
        String host;
        int samples;
        double minRttMs;
        double offsetSpreadMs;
    }

    private final List<Sample> samples = new ArrayList<>();
    private volatile InetAddress host;
    private volatile int port = PORT;
    private volatile long qpcFrequency;
    private volatile boolean running;
    private Thread thread;
    private DatagramSocket socket;

    /**
     * Use a fixed host, or null to find one by broadcast.
     *
     * @param address Host address.
     */
    void setHost(InetAddress address) {
        synchronized (samples) {
            // Samples from another host are on another clock
            samples.clear();
        }
        host = address;
    }

    InetAddress getHost() {
        return host;
    }

    /** @param port The port the host answers on. */
    void setPort(int port) {
        this.port = port;
    }

    int getPort() {
        return port;
    }

    long getQpcFrequency() {
        return qpcFrequency;
    }

    void start() {
        if (running) {
            return;
        }
        running = true;
        thread = new Thread(this::run, "clock-sync");
        thread.start();
    }

    void stop() {
        running = false;
        DatagramSocket s = socket;
        if (s != null) {
            s.close();
        }
        if (thread != null) {
            try {
                thread.join(500);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
            thread = null;
        }
    }

    /**
     * The samples whose request was sent between two phone times.
     *
     * @param fromNs Earliest t1.
     * @param toNs Latest t1.
     * @return Copies of the samples, oldest first.
     */
    List<Sample> samplesBetween(long fromNs, long toNs) {
        List<Sample> out = new ArrayList<>();
        synchronized (samples) {
            for (Sample s : samples) {
                if (s.t1 >= fromNs && s.t1 <= toNs) {
                    out.add(s);
                }
            }
        }
        return out;
    }

    /**
     * Summarize the last ten seconds: the fastest round trip, and how far the offset measured by the fastest round
     * trip of each second wanders, which covers both noise and drift.
     *
     * @return The status, with samples 0 when nothing has been heard.
     */
    Status status() {
        Status status = new Status();
        InetAddress h = host;
        status.host = h == null ? null : h.getHostAddress();
        long frequency = qpcFrequency;
        long now = SystemClock.elapsedRealtimeNanos();
        List<Sample> recent = samplesBetween(now - 10_000_000_000L, now);
        status.samples = recent.size();
        if (recent.isEmpty() || frequency == 0) {
            return status;
        }
        double minRtt = Double.MAX_VALUE;
        double lowOffset = Double.MAX_VALUE;
        double highOffset = -Double.MAX_VALUE;
        for (int second = 0; second < 10; second++) {
            long start = now - (10 - second) * 1_000_000_000L;
            Sample best = null;
            double bestRtt = Double.MAX_VALUE;
            for (Sample s : recent) {
                if (s.t1 >= start && s.t1 < start + 1_000_000_000L) {
                    double rtt = rttNs(s, frequency);
                    if (rtt < bestRtt) {
                        bestRtt = rtt;
                        best = s;
                    }
                }
            }
            if (best != null) {
                minRtt = Math.min(minRtt, bestRtt);
                double offset = offsetNs(best, frequency);
                lowOffset = Math.min(lowOffset, offset);
                highOffset = Math.max(highOffset, offset);
            }
        }
        status.minRttMs = minRtt / 1e6;
        status.offsetSpreadMs = (highOffset - lowOffset) / 1e6;
        return status;
    }

    /**
     * Round trip time, less the time the host held the request.
     *
     * @param s Sample.
     * @param frequency QPC ticks per second.
     * @return Nanoseconds.
     */
    static double rttNs(Sample s, long frequency) {
        return (s.t4 - s.t1) - (s.t3 - s.t2) * 1e9 / frequency;
    }

    /**
     * Host QPC time in ns minus phone time, assuming the request and reply took equally long.
     *
     * @param s Sample.
     * @param frequency QPC ticks per second.
     * @return Nanoseconds.
     */
    static double offsetNs(Sample s, long frequency) {
        double t2 = s.t2 * 1e9 / frequency;
        double t3 = s.t3 * 1e9 / frequency;
        return ((t2 - s.t1) + (t3 - s.t4)) / 2;
    }

    private void run() {
        ByteBuffer request = ByteBuffer.allocate(16).order(ByteOrder.LITTLE_ENDIAN);
        byte[] replyBytes = new byte[64];
        DatagramPacket reply = new DatagramPacket(replyBytes, replyBytes.length);
        InetAddress broadcast;
        try {
            broadcast = InetAddress.getByName("255.255.255.255");
            socket = new DatagramSocket();
            socket.setBroadcast(true);
        } catch (IOException e) {
            running = false;
            return;
        }
        int sequence = 0;
        long nextSend = SystemClock.elapsedRealtimeNanos();
        while (running) {
            try {
                long now = SystemClock.elapsedRealtimeNanos();
                if (now >= nextSend) {
                    InetAddress target = host;
                    request.clear();
                    request.putInt(REQUEST_MAGIC).putInt(++sequence);
                    long t1 = SystemClock.elapsedRealtimeNanos();
                    request.putLong(t1);
                    DatagramPacket packet = new DatagramPacket(request.array(), 16, target != null ? target : broadcast, port);
                    socket.send(packet);
                    nextSend = t1 + (target != null ? INTERVAL_NS : BROADCAST_INTERVAL_NS);
                }
                long waitMs = Math.max(1, (nextSend - SystemClock.elapsedRealtimeNanos()) / 1_000_000L);
                socket.setSoTimeout((int) waitMs);
                reply.setLength(replyBytes.length);
                socket.receive(reply);
                long t4 = SystemClock.elapsedRealtimeNanos();
                if (reply.getLength() < 40) {
                    continue;
                }
                ByteBuffer in = ByteBuffer.wrap(replyBytes, 0, reply.getLength()).order(ByteOrder.LITTLE_ENDIAN);
                if (in.getInt() != REPLY_MAGIC) {
                    continue;
                }
                in.getInt();  // sequence
                long t1 = in.getLong();
                long t2 = in.getLong();
                long t3 = in.getLong();
                long frequency = in.getLong();
                if (frequency <= 0) {
                    continue;
                }
                if (host == null) {
                    host = reply.getAddress();
                }
                if (!reply.getAddress().equals(host)) {
                    continue;
                }
                qpcFrequency = frequency;
                synchronized (samples) {
                    if (samples.size() >= MAX_SAMPLES) {
                        samples.subList(0, MAX_SAMPLES / 10).clear();
                    }
                    samples.add(new Sample(t1, t2, t3, t4));
                }
            } catch (SocketTimeoutException ignored) {
                // Time to send the next request
            } catch (IOException e) {
                if (!running) {
                    break;
                }
                SystemClock.sleep(200);
            }
        }
        socket.close();
    }
}
