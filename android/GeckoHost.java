package pl.unitytranslator.rpgm;

import android.content.res.AssetFileDescriptor;
import android.content.res.AssetManager;
import android.graphics.Color;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;
import org.mozilla.geckoview.AllowOrDeny;
import org.mozilla.geckoview.GeckoResult;
import org.mozilla.geckoview.GeckoRuntime;
import org.mozilla.geckoview.GeckoRuntimeSettings;
import org.mozilla.geckoview.GeckoSession;
import org.mozilla.geckoview.GeckoSessionSettings;
import org.mozilla.geckoview.GeckoView;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketException;
import java.net.SocketTimeoutException;
import java.net.URLDecoder;
import java.security.SecureRandom;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;

/**
 * The game in GeckoView (Mozilla's engine, bundled in the APK) instead of the system WebView.
 *
 * Why: the system WebView renderer can be a 32-bit process (e.g. AYN Thor: WebView 109,
 * armeabi-v7a) with only ~3-4 GB of address space; big MV/MZ games exhaust it and the
 * renderer crashes. GeckoView's content process belongs to this app and is 64-bit.
 *
 * GeckoView cannot intercept requests and has no addJavascriptInterface, so the game is
 * served by a tiny HTTP server on 127.0.0.1 (random token in every URL, so other apps
 * cannot read it) and window.UTAndroid is provided by UT_GeckoHost.js, which calls the
 * same MainActivity.Bridge methods through synchronous XHR (/__ut__/rpc).
 * Pause / resume / Back reach the page as Server-Sent Events (/__ut__/events).
 *
 * This class is only loaded in APKs built with the GeckoView renderer.
 */
final class GeckoHost {
    static final String TAG = MainActivity.TAG;
    private static GeckoRuntime runtime;        // one per process

    private final MainActivity activity;
    private final MainActivity.Bridge bridge;
    private final AssetManager am;
    private final String token;
    private final ExecutorService pool = Executors.newCachedThreadPool(new ThreadFactory() {
        public Thread newThread(Runnable r) {
            Thread t = new Thread(r, "ut-http");
            t.setDaemon(true);
            return t;
        }
    });
    private final Set<BlockingQueue<String>> listeners =
            java.util.Collections.newSetFromMap(new ConcurrentHashMap<BlockingQueue<String>, Boolean>());
    private ServerSocket socket;
    private GeckoSession session;
    private GeckoView view;
    private volatile boolean closed;

    GeckoHost(MainActivity activity) {
        this.activity = activity;
        this.bridge = activity.new Bridge();
        this.am = activity.getAssets();
        byte[] random = new byte[16];
        new SecureRandom().nextBytes(random);
        StringBuilder t = new StringBuilder("t");
        for (byte b : random) t.append(String.format(Locale.ROOT, "%02x", b));
        token = t.toString();
    }

    // ------------------------------------------------------------------ lifecycle

    void start() throws IOException {
        socket = bind(20000 + ((activity.getPackageName().hashCode() & 0x7fffffff) % 29000));
        pool.execute(new Runnable() {
            public void run() {
                acceptLoop();
            }
        });
        if (runtime == null) {
            // console.log -> logcat (tag GeckoConsole); remote debugging over USB like the
            // WebView version (about:debugging in desktop Firefox).
            runtime = GeckoRuntime.create(activity.getApplicationContext(), new GeckoRuntimeSettings.Builder()
                    .consoleOutput(true).remoteDebuggingEnabled(true).javaScriptEnabled(true).build());
        }
        session = new GeckoSession(new GeckoSessionSettings.Builder()
                .viewportMode(GeckoSessionSettings.VIEWPORT_MODE_MOBILE)
                .userAgentMode(GeckoSessionSettings.USER_AGENT_MODE_MOBILE)
                .usePrivateMode(false).build());
        final String allowed = url("");
        session.setNavigationDelegate(new GeckoSession.NavigationDelegate() {
            @Override
            public GeckoResult<AllowOrDeny> onLoadRequest(GeckoSession s, LoadRequest request) {
                if (request.uri.startsWith(allowed) || request.uri.startsWith("about:")) {
                    return GeckoResult.fromValue(AllowOrDeny.ALLOW);
                }
                activity.openExternal(request.uri);          // links from the game -> real browser
                return GeckoResult.fromValue(AllowOrDeny.DENY);
            }
        });
        session.setPermissionDelegate(new GeckoSession.PermissionDelegate() {
            @Override
            public GeckoResult<Integer> onContentPermissionRequest(GeckoSession s, ContentPermission p) {
                boolean autoplay = p.permission == PERMISSION_AUTOPLAY_AUDIBLE || p.permission == PERMISSION_AUTOPLAY_INAUDIBLE;
                return GeckoResult.fromValue(autoplay ? ContentPermission.VALUE_ALLOW : ContentPermission.VALUE_DENY);
            }
        });
        session.setContentDelegate(new GeckoSession.ContentDelegate() {
            @Override
            public void onCrash(GeckoSession s) {
                restart("crash");
            }

            @Override
            public void onKill(GeckoSession s) {
                restart("killed");
            }
        });
        session.open(runtime);
        view = new GeckoView(activity);
        view.setBackgroundColor(Color.BLACK);
        view.setSession(session);
        activity.setContentView(view);
        view.requestFocus();
        session.loadUri(url("index.html"));
        Log.i(TAG, "GeckoView " + org.mozilla.geckoview.BuildConfig.MOZ_APP_VERSION + " on " + url(""));
    }

    /** Content process gone (usually out of memory): start over, like the WebView version. */
    private void restart(String why) {
        Log.e(TAG, "Gecko content process " + why + " - restarting");
        activity.runOnUiThread(new Runnable() {
            public void run() {
                if (!activity.isFinishing()) activity.recreate();
            }
        });
    }

    void pause() {
        command("pause");
        if (session != null) session.setActive(false);
    }

    void resume() {
        if (session != null) session.setActive(true);
        command("resume");
        if (view != null) view.requestFocus();
    }

    void back() {
        command("back");
    }

    void destroy() {
        closed = true;
        try {
            if (socket != null) socket.close();
        } catch (IOException ignored) {
        }
        for (BlockingQueue<String> q : listeners) q.offer("");
        pool.shutdownNow();
        if (session != null) {
            session.close();
            session = null;
        }
    }

    // ------------------------------------------------------------------ http server

    private String url(String path) {
        return "http://127.0.0.1:" + socket.getLocalPort() + "/" + token + "/" + path;
    }

    private static ServerSocket bind(int preferred) throws IOException {
        IOException last = null;
        for (int i = 0; i < 20; i++) {
            try {
                ServerSocket s = new ServerSocket();
                s.setReuseAddress(true);
                s.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), preferred + i), 64);
                return s;
            } catch (IOException e) {
                last = e;
            }
        }
        throw last;
    }

    private void command(String name) {
        for (BlockingQueue<String> q : listeners) q.offer("event: cmd\ndata: " + name + "\n\n");
    }

    private void acceptLoop() {
        while (!closed) {
            try {
                final Socket client = socket.accept();
                pool.execute(new Runnable() {
                    public void run() {
                        serve(client);
                    }
                });
            } catch (IOException e) {
                if (!closed) Log.w(TAG, "accept: " + e);
            }
        }
    }

    private static final class Request {
        String method, path;
        final Map<String, String> headers = new HashMap<>();
        byte[] body = new byte[0];
    }

    private void serve(Socket client) {
        try {
            client.setSoTimeout(30000);
            InputStream in = new BufferedInputStream(client.getInputStream());
            OutputStream out = new BufferedOutputStream(client.getOutputStream(), 65536);
            while (!closed) {
                Request r = read(in);
                if (r == null) break;
                boolean keep = !"close".equalsIgnoreCase(r.headers.get("connection"));
                if (!handle(r, out)) break;             // event stream / unknown length: connection used up
                out.flush();
                if (!keep) break;
            }
        } catch (SocketTimeoutException | SocketException ignored) {
            // idle keep-alive connection
        } catch (IOException e) {
            if (!closed) Log.w(TAG, "http: " + e);
        } finally {
            try {
                client.close();
            } catch (IOException ignored) {
            }
        }
    }

    private static String line(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        int c;
        while ((c = in.read()) != -1) {
            if (c == '\n') break;
            if (c != '\r') b.write(c);
            if (b.size() > 16384) throw new IOException("header too long");
        }
        if (c == -1 && b.size() == 0) return null;
        return b.toString("ISO-8859-1");
    }

    private static Request read(InputStream in) throws IOException {
        String first = line(in);
        if (first == null || first.isEmpty()) return null;
        String[] parts = first.split(" ");
        if (parts.length < 2) throw new IOException("bad request line");
        Request r = new Request();
        r.method = parts[0];
        r.path = parts[1];
        String h;
        while ((h = line(in)) != null && !h.isEmpty()) {
            int colon = h.indexOf(':');
            if (colon > 0) r.headers.put(h.substring(0, colon).trim().toLowerCase(Locale.ROOT), h.substring(colon + 1).trim());
        }
        int length = 0;
        try {
            String cl = r.headers.get("content-length");
            if (cl != null) length = Integer.parseInt(cl);
        } catch (NumberFormatException ignored) {
        }
        if (length < 0 || length > 256 * 1024 * 1024) throw new IOException("body too large");
        if (length > 0) {
            r.body = new byte[length];
            int off = 0;
            while (off < length) {
                int n = in.read(r.body, off, length - off);
                if (n < 0) throw new EOFException();
                off += n;
            }
        }
        return r;
    }

    private static void head(OutputStream out, int status, String reason, String mime, long length,
                             Map<String, String> extra) throws IOException {
        StringBuilder h = new StringBuilder("HTTP/1.1 ").append(status).append(' ').append(reason).append("\r\n");
        h.append("Content-Type: ").append(mime).append("\r\n");
        if (length >= 0) h.append("Content-Length: ").append(length).append("\r\n");
        h.append("Cache-Control: no-cache\r\nX-Content-Type-Options: nosniff\r\n");
        if (extra != null) for (Map.Entry<String, String> e : extra.entrySet()) {
            h.append(e.getKey()).append(": ").append(e.getValue()).append("\r\n");
        }
        h.append("\r\n");
        out.write(h.toString().getBytes("ISO-8859-1"));
    }

    private static void bytes(OutputStream out, Request r, int status, String reason, String mime, byte[] data)
            throws IOException {
        head(out, status, reason, mime, data.length, null);
        if (!"HEAD".equals(r.method)) out.write(data);
    }

    private boolean handle(Request r, OutputStream out) throws IOException {
        String prefix = "/" + token + "/";
        String raw = r.path;
        int q = raw.indexOf('?');
        if (q >= 0) raw = raw.substring(0, q);
        if (!raw.startsWith(prefix)) {
            bytes(out, r, 403, "Forbidden", "text/plain", new byte[0]);
            return true;
        }
        String p = MainActivity.norm(URLDecoder.decode(raw.substring(prefix.length()).replace("+", "%2B"), "UTF-8"));
        if (p == null) {
            bytes(out, r, 403, "Forbidden", "text/plain", new byte[0]);
            return true;
        }
        if (p.isEmpty()) p = "index.html";
        if (p.equals("__ut__/events")) return events(out);
        if (p.equals("__ut__/rpc")) {
            bytes(out, r, 200, "OK", "application/json; charset=utf-8", rpc(r.body).getBytes(MainActivity.UTF8));
            return true;
        }
        return file(r, out, p);
    }

    /** {"m": "readText", "a": ["save/file1.rmmzsave"]} -> {"r": ...}, same methods as the WebView bridge. */
    private String rpc(byte[] body) {
        try {
            JSONObject call = new JSONObject(new String(body, MainActivity.UTF8));
            String m = call.getString("m");
            JSONArray a = call.optJSONArray("a");
            if (a == null) a = new JSONArray();
            Object result;
            switch (m) {
                case "exists": result = bridge.exists(a.optString(0, null)); break;
                case "isDir": result = bridge.isDir(a.optString(0, null)); break;
                case "readText": result = bridge.readText(a.optString(0, null)); break;
                case "readBase64": result = bridge.readBase64(a.optString(0, null)); break;
                case "writeText": result = bridge.writeText(a.optString(0, null), a.isNull(1) ? "" : a.optString(1, "")); break;
                case "writeBase64": result = bridge.writeBase64(a.optString(0, null), a.optString(1, "")); break;
                case "mkdir": result = bridge.mkdir(a.optString(0, null), a.optBoolean(1)); break;
                case "unlink": result = bridge.unlink(a.optString(0, null)); break;
                case "rmdir": result = bridge.rmdir(a.optString(0, null)); break;
                case "rename": result = bridge.rename(a.optString(0, null), a.optString(1, null)); break;
                case "readdir": result = bridge.readdir(a.optString(0, null)); break;
                case "stat": result = bridge.stat(a.optString(0, null)); break;
                case "missing": result = bridge.missing(); break;
                case "dataDir": result = bridge.dataDir(); break;
                case "openUrl": bridge.openUrl(a.optString(0, "")); result = null; break;
                case "exit": bridge.exit(); result = null; break;
                default: return new JSONObject().put("error", "unknown method " + m).toString();
            }
            return new JSONObject().put("r", result == null ? JSONObject.NULL : result).toString();
        } catch (Exception e) {
            Log.w(TAG, "rpc", e);
            return "{\"error\":" + JSONObject.quote(String.valueOf(e)) + "}";
        }
    }

    private boolean file(Request r, OutputStream out, String p) throws IOException {
        String real = activity.assetFile(p);
        if (real == null) {
            Log.w(TAG, "404 " + p);
            synchronized (activity.missing) {
                if (activity.missing.size() < 50) activity.missing.add(p);
            }
            bytes(out, r, 404, "Not Found", "text/plain", new byte[0]);
            return true;
        }
        String mime = MainActivity.mimeOf(real);
        if (mime.startsWith("text/") || mime.endsWith("javascript") || mime.endsWith("json")) mime += "; charset=utf-8";
        String asset = MainActivity.WWW + real;
        Long known = activity.files.get(real);
        long total = known == null ? -1 : known;
        long start = 0, length = total;
        int status = 200;
        Map<String, String> extra = new HashMap<>();
        extra.put("Accept-Ranges", "bytes");
        String range = r.headers.get("range");
        if (range != null && range.startsWith("bytes=") && total > 0) {
            try {
                String[] se = range.substring(6).split(",")[0].trim().split("-", -1);
                long end;
                if (se[0].isEmpty()) {
                    start = Math.max(0, total - Long.parseLong(se[1]));
                    end = total - 1;
                } else {
                    start = Long.parseLong(se[0]);
                    end = se.length > 1 && !se[1].isEmpty() ? Math.min(Long.parseLong(se[1]), total - 1) : total - 1;
                }
                if (start >= total || start > end) {
                    extra.put("Content-Range", "bytes */" + total);
                    head(out, 416, "Range Not Satisfiable", mime, 0, extra);
                    return true;
                }
                length = end - start + 1;
                status = 206;
                extra.put("Content-Range", "bytes " + start + "-" + end + "/" + total);
            } catch (NumberFormatException e) {
                start = 0;
                length = total;
            }
        }
        InputStream in = null;
        try {
            if (start > 0) {
                try {                                         // stored assets: seek directly
                    AssetFileDescriptor fd = am.openFd(asset);
                    InputStream s = fd.createInputStream();
                    long skip = start;
                    while (skip > 0) {
                        long k = s.skip(skip);
                        if (k <= 0) break;
                        skip -= k;
                    }
                    in = s;
                } catch (IOException compressed) {
                    in = null;
                }
            }
            if (in == null) {
                in = am.open(asset, AssetManager.ACCESS_STREAMING);
                long skip = start;
                while (skip > 0) {
                    long k = in.skip(skip);
                    if (k <= 0) {
                        if (in.read() < 0) throw new EOFException();
                        k = 1;
                    }
                    skip -= k;
                }
            }
            head(out, status, status == 206 ? "Partial Content" : "OK", mime, length, extra);
            if ("HEAD".equals(r.method)) return true;
            byte[] buf = new byte[65536];
            long left = length < 0 ? Long.MAX_VALUE : length;
            while (left > 0) {
                int n = in.read(buf, 0, (int) Math.min(buf.length, left));
                if (n < 0) break;
                out.write(buf, 0, n);
                left -= n;
            }
        } finally {
            if (in != null) in.close();
        }
        return length >= 0;
    }

    private boolean events(OutputStream out) throws IOException {
        BlockingQueue<String> queue = new LinkedBlockingQueue<>();
        listeners.add(queue);
        try {
            Map<String, String> extra = new HashMap<>();
            extra.put("Connection", "keep-alive");
            head(out, 200, "OK", "text/event-stream; charset=utf-8", -1, extra);
            out.write("retry: 500\n\n".getBytes(MainActivity.UTF8));
            out.flush();
            while (!closed) {
                String message = queue.poll(15, TimeUnit.SECONDS);
                if (closed) break;
                out.write((message == null ? ": ping\n\n" : message).getBytes(MainActivity.UTF8));
                out.flush();
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            listeners.remove(queue);
        }
        return false;
    }
}
