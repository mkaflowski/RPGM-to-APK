package pl.unitytranslator.rpgm;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.res.AssetFileDescriptor;
import android.content.res.AssetManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.TextUtils;
import android.util.Base64;
import android.util.Log;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.Charset;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Locale;
import java.util.Map;
import java.util.TreeSet;

/**
 * RPG Maker MV/MZ in a WebView.
 *
 * The game lives in assets/www/ and is served from https://appassets.androidplatform.net/
 * (a real https origin, so XHR/fetch/wasm work - file:// would block them).
 * assets/ut_files.txt lists every packed file ("path\tsize") - used for fast
 * existence checks and for case-insensitive lookups (Windows games often reference
 * "Img/Foo.png" while the file is "img/foo.png").
 *
 * window.UTAndroid (see ut_android.js plugin) gives the game a small synchronous
 * file system: writes go to getExternalFilesDir()/ (saves in save/), reads fall
 * back to the read-only game assets.
 */
public class MainActivity extends Activity {
    static final String TAG = "RPGM";
    static final String HOST = "appassets.androidplatform.net";
    static final String BASE = "https://" + HOST + "/";
    static final String WWW = "www/";
    static final Charset UTF8 = Charset.forName("UTF-8");

    WebView web;
    AssetManager am;
    File root;
    final HashMap<String, Long> files = new HashMap<>();
    final HashMap<String, String> lower = new HashMap<>();
    final HashMap<String, TreeSet<String>> children = new HashMap<>();
    final ArrayList<String> missing = new ArrayList<>();       // 404s, shown by the boot watchdog

    static final HashMap<String, String> MIME = new HashMap<>();

    static {
        String[] m = {
                "html", "text/html", "htm", "text/html", "js", "application/javascript",
                "mjs", "application/javascript", "json", "application/json", "css", "text/css",
                "txt", "text/plain", "csv", "text/csv", "xml", "application/xml", "svg", "image/svg+xml",
                "png", "image/png", "jpg", "image/jpeg", "jpeg", "image/jpeg", "gif", "image/gif",
                "webp", "image/webp", "bmp", "image/bmp", "ico", "image/x-icon",
                "ogg", "audio/ogg", "oga", "audio/ogg", "m4a", "audio/mp4", "mp3", "audio/mpeg",
                "wav", "audio/wav", "mid", "audio/midi", "webm", "video/webm", "mp4", "video/mp4",
                "ogv", "video/ogg", "wasm", "application/wasm", "ttf", "font/ttf", "otf", "font/otf",
                "woff", "font/woff", "woff2", "font/woff2", "eot", "application/vnd.ms-fontobject",
        };
        for (int i = 0; i < m.length; i += 2) MIME.put(m[i], m[i + 1]);
    }

    // ------------------------------------------------------------------ lifecycle

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        am = getAssets();
        root = getExternalFilesDir(null);
        if (root == null) root = getFilesDir();
        loadIndex();

        Window w = getWindow();
        w.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        w.setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(Color.BLACK));
        if (Build.VERSION.SDK_INT >= 28) {
            WindowManager.LayoutParams lp = w.getAttributes();
            lp.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            w.setAttributes(lp);
        }

        WebView.setWebContentsDebuggingEnabled(true);   // chrome://inspect - handy when a game misbehaves
        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        web.setVerticalScrollBarEnabled(false);
        web.setHorizontalScrollBarEnabled(false);
        // Android 8+ rysuje ramkę "fokusu" wokół widoku, który dostał fokus (klawiatura, pad,
        // czasem sam start) - na starym motywie jest żółto-pomarańczowa i obejmuje cały ekran.
        if (Build.VERSION.SDK_INT >= 26) web.setDefaultFocusHighlightEnabled(false);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setTextZoom(100);
        // honour <meta viewport> (width=device-width, viewport-fit=cover) added to index.html
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        web.addJavascriptInterface(new Bridge(), "UTAndroid");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage c) {
                Log.d(TAG, c.messageLevel() + " " + c.sourceId() + ":" + c.lineNumber() + " " + c.message());
                return true;
            }
        });
        setContentView(web);
        hideSystemBars();
        web.loadUrl(BASE + "index.html");
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemBars();
    }

    @SuppressWarnings("deprecation")
    void hideSystemBars() {
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.systemBars());
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
        }
    }

    @Override
    protected void onPause() {
        if (web != null) {
            web.evaluateJavascript("window.__utPause && window.__utPause()", null);
            web.onPause();
            web.pauseTimers();
        }
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) {
            web.resumeTimers();
            web.onResume();
            web.evaluateJavascript("window.__utResume && window.__utResume()", null);
        }
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }

    /** Back = Escape (cancel / open menu), like on PC. The game itself never gets closed by Back. */
    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (web != null) web.evaluateJavascript("window.__utBack && window.__utBack()", null);
    }

    // ------------------------------------------------------------------ asset index

    void loadIndex() {
        try (BufferedReader r = new BufferedReader(new InputStreamReader(am.open("ut_files.txt"), UTF8))) {
            String line;
            while ((line = r.readLine()) != null) {
                if (line.isEmpty()) continue;
                int tab = line.lastIndexOf('\t');
                String p = tab < 0 ? line : line.substring(0, tab);
                long size = -1;
                if (tab >= 0) {
                    try {
                        size = Long.parseLong(line.substring(tab + 1));
                    } catch (NumberFormatException ignored) {
                    }
                }
                files.put(p, size);
                String lp = p.toLowerCase(Locale.ROOT);
                if (!lower.containsKey(lp)) lower.put(lp, p);
                // register every parent directory (exact and lower-case) with its child
                String child = p;
                int slash = child.lastIndexOf('/');
                while (true) {
                    String dir = slash < 0 ? "" : child.substring(0, slash);
                    String name = slash < 0 ? child : child.substring(slash + 1);
                    TreeSet<String> set = children.get(dir);
                    boolean known = set != null;
                    if (set == null) {
                        set = new TreeSet<>();
                        children.put(dir, set);
                        String ld = dir.toLowerCase(Locale.ROOT);
                        if (!lower.containsKey(ld)) lower.put(ld, dir);
                    }
                    set.add(name);
                    if (known || slash < 0) break;
                    child = dir;
                    slash = child.lastIndexOf('/');
                }
            }
        } catch (IOException e) {
            Log.e(TAG, "ut_files.txt missing", e);
        }
    }

    /** Exact name first, then case-insensitive. Returns the real asset path (relative to www/) or null. */
    String assetFile(String p) {
        if (p == null) return null;
        if (files.containsKey(p)) return p;
        String r = lower.get(p.toLowerCase(Locale.ROOT));
        return r != null && files.containsKey(r) ? r : null;
    }

    String assetDir(String p) {
        if (p == null) return null;
        if (children.containsKey(p)) return p;
        String r = lower.get(p.toLowerCase(Locale.ROOT));
        return r != null && children.containsKey(r) ? r : null;
    }

    static String norm(String p) {
        if (p == null) return null;
        p = p.replace('\\', '/');
        if (p.startsWith(BASE)) p = p.substring(BASE.length());
        int q = p.indexOf('?');
        if (q >= 0 && p.startsWith("http")) p = p.substring(0, q);
        ArrayList<String> out = new ArrayList<>();
        for (String seg : p.split("/")) {
            if (seg.isEmpty() || seg.equals(".")) continue;
            if (seg.equals("..")) {
                if (out.isEmpty()) return null;          // never escape the sandbox
                out.remove(out.size() - 1);
                continue;
            }
            out.add(seg);
        }
        return TextUtils.join("/", out);
    }

    static String mimeOf(String path) {
        int dot = path.lastIndexOf('.');
        String ext = dot < 0 ? "" : path.substring(dot + 1).toLowerCase(Locale.ROOT);
        String m = MIME.get(ext);
        return m != null ? m : "application/octet-stream";
    }

    static byte[] readAll(InputStream in) throws IOException {
        try {
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
            return bo.toByteArray();
        } finally {
            in.close();
        }
    }

    /** Stream limited to n bytes (for HTTP Range responses). */
    static class Limited extends FilterInputStream {
        long left;

        Limited(InputStream in, long n) {
            super(in);
            left = n;
        }

        @Override
        public int read() throws IOException {
            if (left <= 0) return -1;
            int b = super.read();
            if (b >= 0) left--;
            return b;
        }

        @Override
        public int read(byte[] b, int off, int len) throws IOException {
            if (left <= 0) return -1;
            int n = super.read(b, off, (int) Math.min(len, left));
            if (n > 0) left -= n;
            return n;
        }
    }

    // ------------------------------------------------------------------ http

    class Client extends WebViewClient {
        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
            Uri u = req.getUrl();
            if (!HOST.equals(u.getHost())) return null;
            String p = norm(u.getPath());
            if (p == null || p.isEmpty()) p = "index.html";
            String real = assetFile(p);
            HashMap<String, String> h = new HashMap<>();
            h.put("Access-Control-Allow-Origin", "*");
            h.put("Cache-Control", "no-cache");
            if (real == null) {
                Log.w(TAG, "404 " + p);
                synchronized (missing) {
                    if (missing.size() < 50) missing.add(p);
                }
                return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", h,
                        new java.io.ByteArrayInputStream(new byte[0]));
            }
            String mime = mimeOf(real);
            String enc = mime.startsWith("text/") || mime.endsWith("javascript") || mime.endsWith("json") ? "utf-8" : null;
            String asset = WWW + real;
            try {
                String range = null;
                Map<String, String> rh = req.getRequestHeaders();
                if (rh != null) {
                    for (Map.Entry<String, String> e : rh.entrySet()) {
                        if ("range".equalsIgnoreCase(e.getKey())) range = e.getValue();
                    }
                }
                if (range != null && range.startsWith("bytes=")) {
                    long total = -1;
                    try (AssetFileDescriptor fd = am.openFd(asset)) {   // only for stored (uncompressed) assets
                        total = fd.getLength();
                    } catch (IOException ignored) {
                    }
                    if (total > 0) {
                        String[] se = range.substring(6).split(",")[0].trim().split("-", -1);
                        long start, end;
                        if (se[0].isEmpty()) {                        // "bytes=-500" = last 500 bytes
                            start = Math.max(0, total - Long.parseLong(se[1]));
                            end = total - 1;
                        } else {
                            start = Long.parseLong(se[0]);
                            end = se.length > 1 && !se[1].isEmpty() ? Math.min(Long.parseLong(se[1]), total - 1) : total - 1;
                        }
                        if (start >= total || start > end) {
                            h.put("Content-Range", "bytes */" + total);
                            return new WebResourceResponse(mime, enc, 416, "Range Not Satisfiable", h,
                                    new java.io.ByteArrayInputStream(new byte[0]));
                        }
                        InputStream in = am.open(asset, AssetManager.ACCESS_STREAMING);
                        long skip = start;
                        while (skip > 0) {
                            long k = in.skip(skip);
                            if (k <= 0) break;
                            skip -= k;
                        }
                        h.put("Accept-Ranges", "bytes");
                        h.put("Content-Range", "bytes " + start + "-" + end + "/" + total);
                        h.put("Content-Length", String.valueOf(end - start + 1));
                        return new WebResourceResponse(mime, enc, 206, "Partial Content", h, new Limited(in, end - start + 1));
                    }
                }
                Long size = files.get(real);
                if (size != null && size >= 0) h.put("Content-Length", String.valueOf(size));
                return new WebResourceResponse(mime, enc, 200, "OK", h, am.open(asset, AssetManager.ACCESS_STREAMING));
            } catch (Exception e) {
                Log.e(TAG, "serve " + p, e);
                return new WebResourceResponse("text/plain", "utf-8", 500, "Error", h,
                        new java.io.ByteArrayInputStream(new byte[0]));
            }
        }

        /** Renderer killed (usually out of memory): without this Android 8+ kills the whole app,
         *  older WebViews leave a dead black view. Start over with a fresh WebView. */
        @Override
        public boolean onRenderProcessGone(WebView view, android.webkit.RenderProcessGoneDetail detail) {
            Log.e(TAG, "WebView renderer gone (crash=" + detail.didCrash() + ") - restarting");
            if (view == web) {
                web = null;
                if (view.getParent() instanceof android.view.ViewGroup) {
                    ((android.view.ViewGroup) view.getParent()).removeView(view);
                }
                view.destroy();
                recreate();
            }
            return true;
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
            Uri u = req.getUrl();
            if (HOST.equals(u.getHost())) return false;
            openExternal(u.toString());                       // links from the game -> real browser
            return true;
        }
    }

    void openExternal(String url) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (ActivityNotFoundException | SecurityException e) {
            Log.w(TAG, "cannot open " + url);
        }
    }

    // ------------------------------------------------------------------ JS bridge

    class Bridge {
        File file(String p) {
            return p == null ? null : (p.isEmpty() ? root : new File(root, p));
        }

        byte[] readBytes(String raw) {
            String p = norm(raw);
            if (p == null) return null;
            try {
                File f = file(p);
                if (f.isFile()) return readAll(new FileInputStream(f));
                String a = assetFile(p);
                if (a != null) return readAll(am.open(WWW + a));
            } catch (IOException e) {
                Log.w(TAG, "read " + p, e);
            }
            return null;
        }

        boolean writeBytes(String raw, byte[] data) {
            String p = norm(raw);
            if (p == null || p.isEmpty()) return false;
            File f = file(p);
            File dir = f.getParentFile();
            if (dir != null && !dir.isDirectory() && !dir.mkdirs()) return false;
            File tmp = new File(dir, f.getName() + ".uttmp");
            try (FileOutputStream o = new FileOutputStream(tmp)) {
                o.write(data);
                o.getFD().sync();
            } catch (IOException e) {
                Log.e(TAG, "write " + p, e);
                tmp.delete();
                return false;
            }
            if (tmp.renameTo(f)) return true;
            f.delete();
            return tmp.renameTo(f);
        }

        @JavascriptInterface
        public boolean exists(String raw) {
            String p = norm(raw);
            if (p == null) return false;
            return p.isEmpty() || file(p).exists() || assetFile(p) != null || assetDir(p) != null;
        }

        @JavascriptInterface
        public boolean isDir(String raw) {
            String p = norm(raw);
            if (p == null) return false;
            return p.isEmpty() || file(p).isDirectory() || assetDir(p) != null;
        }

        @JavascriptInterface
        public String readText(String raw) {
            byte[] b = readBytes(raw);
            return b == null ? null : new String(b, UTF8);
        }

        @JavascriptInterface
        public String readBase64(String raw) {
            byte[] b = readBytes(raw);
            return b == null ? null : Base64.encodeToString(b, Base64.NO_WRAP);
        }

        @JavascriptInterface
        public boolean writeText(String raw, String data) {
            return writeBytes(raw, (data == null ? "" : data).getBytes(UTF8));
        }

        @JavascriptInterface
        public boolean writeBase64(String raw, String b64) {
            try {
                return writeBytes(raw, Base64.decode(b64 == null ? "" : b64, Base64.DEFAULT));
            } catch (IllegalArgumentException e) {
                return false;
            }
        }

        @JavascriptInterface
        public boolean mkdir(String raw, boolean recursive) {
            String p = norm(raw);
            if (p == null) return false;
            File f = file(p);
            if (f.isDirectory()) return true;
            return recursive ? f.mkdirs() : f.mkdir();
        }

        @JavascriptInterface
        public boolean unlink(String raw) {
            String p = norm(raw);
            if (p == null || p.isEmpty()) return false;
            File f = file(p);
            return f.isFile() && f.delete();
        }

        @JavascriptInterface
        public boolean rmdir(String raw) {
            String p = norm(raw);
            if (p == null || p.isEmpty()) return false;
            File f = file(p);
            return f.isDirectory() && f.delete();
        }

        @JavascriptInterface
        public boolean rename(String rawFrom, String rawTo) {
            String a = norm(rawFrom), b = norm(rawTo);
            if (a == null || b == null || a.isEmpty() || b.isEmpty()) return false;
            File fa = file(a), fb = file(b);
            if (!fa.exists()) {                               // read-only asset -> copy into the writable dir
                byte[] data = readBytes(a);
                return data != null && writeBytes(b, data);
            }
            File dir = fb.getParentFile();
            if (dir != null && !dir.isDirectory()) dir.mkdirs();
            return fa.renameTo(fb);
        }

        @JavascriptInterface
        public String readdir(String raw) {
            String p = norm(raw);
            if (p == null) return null;
            TreeSet<String> names = new TreeSet<>();
            boolean found = false;
            File f = file(p);
            String[] list = f.isDirectory() ? f.list() : null;
            if (list != null) {
                found = true;
                for (String n : list) if (!n.endsWith(".uttmp")) names.add(n);
            }
            String d = assetDir(p);
            if (d != null) {
                found = true;
                names.addAll(children.get(d));
            }
            if (!found) return null;
            return new JSONArray(names).toString();
        }

        @JavascriptInterface
        public String stat(String raw) {
            String p = norm(raw);
            if (p == null) return null;
            try {
                JSONObject o = new JSONObject();
                File f = file(p);
                if (f.exists()) {
                    o.put("dir", f.isDirectory());
                    o.put("size", f.isFile() ? f.length() : 0);
                    o.put("mtime", f.lastModified());
                    return o.toString();
                }
                String a = assetFile(p);
                if (a != null) {
                    Long size = files.get(a);
                    o.put("dir", false);
                    o.put("size", size == null ? 0 : size);
                    o.put("mtime", 0);
                    return o.toString();
                }
                if (p.isEmpty() || assetDir(p) != null) {
                    o.put("dir", true);
                    o.put("size", 0);
                    o.put("mtime", 0);
                    return o.toString();
                }
            } catch (Exception ignored) {
            }
            return null;
        }

        @JavascriptInterface
        public String missing() {
            synchronized (missing) {
                return new JSONArray(missing).toString();
            }
        }

        @JavascriptInterface
        public String dataDir() {
            return root.getAbsolutePath();
        }

        @JavascriptInterface
        public void openUrl(final String url) {
            runOnUiThread(new Runnable() {
                public void run() {
                    openExternal(url);
                }
            });
        }

        @JavascriptInterface
        public void exit() {
            runOnUiThread(new Runnable() {
                public void run() {
                    finishAndRemoveTask();
                }
            });
        }
    }
}
