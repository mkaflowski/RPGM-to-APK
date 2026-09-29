//=============================================================================
// UT_AndroidBridge.js - added by rpgm_to_apk.py (not part of the original game)
//=============================================================================
/*:
 * @target MZ
 * @plugindesc Android WebView bridge: file saves, require() stubs, Back button.
 * @author UnityTranslator rpgm_to_apk
 *
 * @help
 * Loaded as the FIRST plugin, so every later plugin already sees it.
 *
 * - Saves go to real files (Android/data/<package>/files/save/*.rmmzsave or
 *   *.rpgsave) in exactly the same format as NW.js on PC, so save files can be
 *   copied between PC and the phone in both directions.
 * - require('fs' | 'path' | 'os') returns small working stand-ins, so PC-only
 *   plugin code (e.g. require('os').userInfo()) does not crash the game.
 *   Any other module throws "Cannot find module" like Node.js does, so
 *   try { require('./greenworks') } catch {} keeps working as "no Steam".
 *   `process` and `nw` are NOT defined: Utils.isNwjs() stays false.
 * - Back button = Escape (cancel / menu); SceneManager.terminate closes the app.
 *
 * Does nothing outside the APK (no window.UTAndroid).
 */

(function() {
    "use strict";
    var B = window.UTAndroid;
    if (!B) return;
    window.UT_ANDROID = true;

    // ------------------------------------------------------------ helpers
    function errno(code, op, p) {
        var msgs = {ENOENT: "no such file or directory", EACCES: "permission denied",
                    EEXIST: "file already exists", ENOTDIR: "not a directory", EISDIR: "is a directory"};
        var e = new Error(code + ": " + (msgs[code] || "error") + ", " + op + " '" + p + "'");
        e.code = code;
        e.errno = -1;
        e.syscall = op;
        e.path = p;
        return e;
    }

    function str(p) {
        if (p && typeof p === "object" && typeof p.href === "string") p = p.href;   // URL object
        return String(p);
    }

    function b64ToBytes(b64) {
        var bin = atob(b64), out = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
        return out;
    }

    function bytesToB64(bytes) {
        var chunks = [], CH = 0x8000;
        for (var i = 0; i < bytes.length; i += CH) {
            chunks.push(String.fromCharCode.apply(null, bytes.subarray(i, i + CH)));
        }
        return btoa(chunks.join(""));
    }

    function latin1ToBytes(s) {
        var out = new Uint8Array(s.length);
        for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i) & 0xff;
        return out;
    }

    function bytesToLatin1(bytes) {
        var chunks = [], CH = 0x8000;
        for (var i = 0; i < bytes.length; i += CH) {
            chunks.push(String.fromCharCode.apply(null, bytes.subarray(i, i + CH)));
        }
        return chunks.join("");
    }

    function toBytes(data) {
        if (data instanceof Uint8Array) return data;
        if (data instanceof ArrayBuffer) return new Uint8Array(data);
        if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
        return null;
    }

    function encodingOf(opt) {
        if (typeof opt === "string") return opt.toLowerCase();
        if (opt && typeof opt.encoding === "string") return opt.encoding.toLowerCase();
        return null;
    }

    function decode(bytes, enc) {
        if (!enc || enc === "buffer") return makeBuffer(bytes);
        if (enc === "base64") return bytesToB64(bytes);
        if (enc === "latin1" || enc === "binary") return bytesToLatin1(bytes);
        if (enc === "hex") {
            var h = "";
            for (var i = 0; i < bytes.length; i++) h += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16);
            return h;
        }
        return new TextDecoder("utf-8").decode(bytes);
    }

    // Minimal Buffer look-alike: a Uint8Array whose toString() decodes (utf8 by default).
    function makeBuffer(bytes) {
        var buf = new Uint8Array(bytes);
        buf.toString = function(enc) { return decode(this, (enc || "utf8").toLowerCase()); };
        return buf;
    }

    // ------------------------------------------------------------ fs
    function statObj(p, info) {
        var mtime = new Date(info.mtime || 0);
        return {
            size: info.size || 0, mtime: mtime, mtimeMs: mtime.getTime(), ctime: mtime, atime: mtime,
            birthtime: mtime, mode: info.dir ? 0o40755 : 0o100644,
            isFile: function() { return !info.dir; },
            isDirectory: function() { return !!info.dir; },
            isSymbolicLink: function() { return false; }
        };
    }

    var fs = {
        constants: {F_OK: 0, R_OK: 4, W_OK: 2, X_OK: 1},

        existsSync: function(p) {
            try { return B.exists(str(p)); } catch (e) { return false; }
        },
        accessSync: function(p) {
            if (!B.exists(str(p))) throw errno("ENOENT", "access", str(p));
        },
        statSync: function(p, opt) {
            var s = B.stat(str(p));
            if (s == null) {
                if (opt && opt.throwIfNoEntry === false) return undefined;
                throw errno("ENOENT", "stat", str(p));
            }
            return statObj(str(p), JSON.parse(s));
        },
        readFileSync: function(p, opt) {
            p = str(p);
            var enc = encodingOf(opt);
            if (B.isDir(p)) throw errno("EISDIR", "read", p);
            if (enc === "utf8" || enc === "utf-8") {
                var s = B.readText(p);
                if (s == null) throw errno("ENOENT", "open", p);
                return s;
            }
            var b = B.readBase64(p);
            if (b == null) throw errno("ENOENT", "open", p);
            return decode(b64ToBytes(b), enc);
        },
        writeFileSync: function(p, data, opt) {
            p = str(p);
            var enc = encodingOf(opt), ok, bytes = toBytes(data);
            if (bytes) {
                ok = B.writeBase64(p, bytesToB64(bytes));
            } else {
                data = String(data);
                if (enc === "base64") ok = B.writeBase64(p, data);
                else if (enc === "latin1" || enc === "binary") ok = B.writeBase64(p, bytesToB64(latin1ToBytes(data)));
                else ok = B.writeText(p, data);
            }
            if (!ok) throw errno("EACCES", "open", p);
        },
        appendFileSync: function(p, data, opt) {
            var old = fs.existsSync(p) ? fs.readFileSync(p) : new Uint8Array(0);
            var add = toBytes(data) || new TextEncoder().encode(String(data));
            var all = new Uint8Array(old.length + add.length);
            all.set(old, 0);
            all.set(add, old.length);
            fs.writeFileSync(p, all, opt);
        },
        copyFileSync: function(a, b) {
            fs.writeFileSync(b, fs.readFileSync(a));
        },
        mkdirSync: function(p, opt) {
            p = str(p);
            var rec = !!(opt && typeof opt === "object" && opt.recursive);
            if (!rec && B.exists(p)) throw errno("EEXIST", "mkdir", p);
            if (!B.mkdir(p, rec)) throw errno("ENOENT", "mkdir", p);
            return undefined;
        },
        unlinkSync: function(p) {
            p = str(p);
            if (!B.unlink(p)) throw errno("ENOENT", "unlink", p);
        },
        rmdirSync: function(p) {
            p = str(p);
            if (!B.rmdir(p)) throw errno("ENOENT", "rmdir", p);
        },
        rmSync: function(p, opt) {
            p = str(p);
            if (B.unlink(p) || B.rmdir(p)) return;
            if (!(opt && opt.force)) throw errno("ENOENT", "rm", p);
        },
        renameSync: function(a, b) {
            if (!B.rename(str(a), str(b))) throw errno("ENOENT", "rename", str(a));
        },
        readdirSync: function(p, opt) {
            p = str(p);
            var s = B.readdir(p);
            if (s == null) throw errno("ENOENT", "scandir", p);
            var names = JSON.parse(s);
            if (opt && opt.withFileTypes) {
                return names.map(function(n) {
                    var st = fs.statSync(p.replace(/\/*$/, "/") + n);
                    return {name: n, isFile: st.isFile, isDirectory: st.isDirectory,
                            isSymbolicLink: function() { return false; }};
                });
            }
            return names;
        }
    };
    fs.lstatSync = fs.statSync;

    // async (callback) and promise variants on top of the synchronous ones
    function lastFn(args) { return typeof args[args.length - 1] === "function" ? args.pop() : null; }
    ["stat", "lstat", "readFile", "writeFile", "appendFile", "copyFile", "mkdir", "unlink",
     "rmdir", "rm", "rename", "readdir", "access"].forEach(function(name) {
        var sync = fs[name + "Sync"];
        fs[name] = function() {
            var args = Array.prototype.slice.call(arguments), cb = lastFn(args);
            setTimeout(function() {
                var res, err = null;
                try { res = sync.apply(fs, args); } catch (e) { err = e; }
                if (cb) cb(err, res);
            }, 0);
        };
    });
    fs.exists = function(p, cb) {
        var r = fs.existsSync(p);
        setTimeout(function() { if (cb) cb(r); }, 0);
    };
    fs.promises = {};
    ["stat", "lstat", "readFile", "writeFile", "appendFile", "copyFile", "mkdir", "unlink",
     "rmdir", "rm", "rename", "readdir", "access"].forEach(function(name) {
        var sync = fs[name + "Sync"];
        fs.promises[name] = function() {
            var args = arguments;
            return new Promise(function(resolve, reject) {
                try { resolve(sync.apply(fs, args)); } catch (e) { reject(e); }
            });
        };
    });

    // ------------------------------------------------------------ path (POSIX)
    function normalizeParts(p, keepRoot) {
        var abs = p.charAt(0) === "/", out = [];
        p.split("/").forEach(function(seg) {
            if (!seg || seg === ".") return;
            if (seg === "..") {
                if (out.length && out[out.length - 1] !== "..") out.pop();
                else if (!abs) out.push("..");
                return;
            }
            out.push(seg);
        });
        return (abs && keepRoot ? "/" : "") + out.join("/");
    }

    var path = {
        sep: "/",
        delimiter: ":",
        normalize: function(p) {
            p = String(p).replace(/\\/g, "/");
            var trail = /\/$/.test(p), r = normalizeParts(p, true);
            if (!r) r = p.charAt(0) === "/" ? "/" : ".";
            return trail && r !== "/" ? r + "/" : r;
        },
        join: function() {
            var parts = Array.prototype.filter.call(arguments, function(a) { return a !== ""; });
            return parts.length ? path.normalize(parts.join("/")) : ".";
        },
        resolve: function() {
            var r = "";
            for (var i = arguments.length - 1; i >= 0; i--) {
                var a = String(arguments[i]).replace(/\\/g, "/");
                if (!a) continue;
                r = r ? a + "/" + r : a;
                if (a.charAt(0) === "/") break;
            }
            return "/" + normalizeParts(r, false);
        },
        isAbsolute: function(p) { return String(p).charAt(0) === "/"; },
        dirname: function(p) {
            p = String(p).replace(/\\/g, "/").replace(/\/+$/, "");
            var i = p.lastIndexOf("/");
            if (i < 0) return ".";
            return i === 0 ? "/" : p.slice(0, i);
        },
        basename: function(p, ext) {
            p = String(p).replace(/\\/g, "/").replace(/\/+$/, "");
            var b = p.slice(p.lastIndexOf("/") + 1);
            if (ext && b.slice(-ext.length) === ext && b !== ext) b = b.slice(0, -ext.length);
            return b;
        },
        extname: function(p) {
            var b = path.basename(p), i = b.lastIndexOf(".");
            return i <= 0 ? "" : b.slice(i);
        },
        relative: function(from, to) {
            var f = path.resolve(from).split("/").filter(Boolean), t = path.resolve(to).split("/").filter(Boolean);
            while (f.length && t.length && f[0] === t[0]) { f.shift(); t.shift(); }
            return f.map(function() { return ".."; }).concat(t).join("/");
        },
        parse: function(p) {
            var base = path.basename(p), ext = path.extname(p);
            return {root: path.isAbsolute(p) ? "/" : "", dir: path.dirname(p), base: base, ext: ext,
                    name: ext ? base.slice(0, -ext.length) : base};
        },
        format: function(o) {
            var base = o.base || ((o.name || "") + (o.ext || ""));
            return o.dir ? o.dir.replace(/\/$/, "") + "/" + base : (o.root || "") + base;
        }
    };
    path.posix = path;

    // ------------------------------------------------------------ os
    var os = {
        EOL: "\n",
        platform: function() { return "android"; },
        type: function() { return "Linux"; },
        arch: function() { return "arm64"; },
        release: function() { return ""; },
        hostname: function() { return "android"; },
        homedir: function() { return "/"; },
        tmpdir: function() { return "/tmp"; },
        userInfo: function() { return {username: "android", uid: -1, gid: -1, shell: null, homedir: "/"}; },
        cpus: function() { return []; },
        totalmem: function() { return 0; },
        freemem: function() { return 0; },
        uptime: function() { return performance.now() / 1000; },
        networkInterfaces: function() { return {}; }
    };

    // ------------------------------------------------------------ require
    var modules = {fs: fs, path: path, os: os};
    if (typeof window.require !== "function") {
        window.require = function(name) {
            var n = String(name).replace(/^node:/, "");
            if (Object.prototype.hasOwnProperty.call(modules, n)) return modules[n];
            var e = new Error("Cannot find module '" + name + "'");
            e.code = "MODULE_NOT_FOUND";
            throw e;
        };
        window.require.cache = {};
        window.require.resolve = function(name) { return String(name); };
    }

    // ------------------------------------------------------------ saves -> files
    if (typeof StorageManager !== "undefined") {
        StorageManager.isLocalMode = function() { return true; };
        // MZ: Scene_Boot waits for StorageManager.forageKeysUpdated(), i.e. localforage.keys()
        // (IndexedDB) without any error handling. In a WebView IndexedDB sometimes hangs or
        // rejects on start -> endless black screen. Saves here are files, so no IndexedDB wait.
        if (StorageManager.updateForageKeys) {
            StorageManager.updateForageKeys = function() {
                this._forageKeys = [];
                this._forageKeysUpdated = true;
                return Promise.resolve(0);
            };
            StorageManager._forageKeysUpdated = true;
        }
        if (StorageManager.fileDirectoryPath) {           // MZ
            StorageManager.fileDirectoryPath = function() { return "save/"; };
        }
        if (StorageManager.localFileDirectoryPath) {      // MV
            StorageManager.localFileDirectoryPath = function() { return "save/"; };
        }
    }

    // ------------------------------------------------------------ screen size
    // MZ on a phone uses only 90 % of the height unless the page is file:// (room for the
    // browser's address bar - there is none in this app). Use the full height; the engine
    // still scales with min(width, height) ratio, so the aspect ratio is kept (black bars).
    if (typeof Graphics !== "undefined" && typeof Graphics._stretchHeight === "function") {
        Graphics._stretchHeight = function() {
            return Utils.isMobileDevice() ? document.documentElement.clientHeight : window.innerHeight;
        };
    }

    // ------------------------------------------------------------ app lifecycle
    if (typeof SceneManager !== "undefined") {
        SceneManager.terminate = function() { B.exit(); };
    }

    function fakeKey(code) {
        return {keyCode: code, key: "Escape", ctrlKey: false, altKey: false, shiftKey: false,
                preventDefault: function() {}, stopPropagation: function() {}};
    }

    window.__utBack = function() {
        if (typeof Input === "undefined" || !Input._onKeyDown) return;
        Input._onKeyDown(fakeKey(27));
        setTimeout(function() { Input._onKeyUp(fakeKey(27)); }, 100);
    };

    function audioContexts() {
        var list = [];
        if (typeof WebAudio !== "undefined" && WebAudio._context) list.push(WebAudio._context);
        return list;
    }

    window.__utPause = function() {
        audioContexts().forEach(function(c) { try { c.suspend(); } catch (e) { /* ignore */ } });
    };

    window.__utResume = function() {
        audioContexts().forEach(function(c) { try { c.resume(); } catch (e) { /* ignore */ } });
    };

    // no focus rings / tap highlight drawn by Chromium around the canvas
    var style = document.createElement("style");
    style.textContent = "*:focus,*:focus-visible{outline:none!important}" +
        "html,body,canvas{-webkit-tap-highlight-color:transparent;outline:none}";
    (document.head || document.documentElement).appendChild(style);

    // boot watchdog: if the game is still in Scene_Boot after 20 s, say what it waits for
    function bootReport() {
        var s = typeof SceneManager !== "undefined" ? SceneManager._scene : null;
        var name = s ? s.constructor.name : "(no scene)";
        if (s && name !== "Scene_Boot") return null;
        var r = [name];
        function add(label, fn) { try { r.push(label + "=" + fn()); } catch (e) { /* not in MV/MZ */ } }
        add("database", function() { return DataManager.isDatabaseLoaded(); });
        add("forageKeys", function() { return StorageManager.forageKeysUpdated(); });
        add("config", function() { return ConfigManager.isLoaded(); });
        add("globalInfo", function() { return DataManager.isGlobalInfoLoaded(); });
        add("images", function() { return ImageManager.isReady(); });
        add("fonts", function() { return JSON.stringify(FontManager._states); });
        add("errorPrinter", function() { var e = document.getElementById("errorPrinter"); return e ? e.innerText : "-"; });
        add("missing", function() { return B.missing ? B.missing() : "?"; });
        return r.join(" | ");
    }

    setTimeout(function() {
        var rep = bootReport();
        if (!rep) return;
        console.error("[UT] boot stuck: " + rep);
        var d = document.createElement("div");
        d.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:99999;padding:8px;" +
            "background:rgba(0,0,0,.85);color:#fff;font:12px monospace;white-space:pre-wrap;word-break:break-all";
        d.textContent = "Gra nie wystartowała po 20 s (dotknij, aby uruchomić ponownie):\n" + rep;
        d.addEventListener("click", function() { location.reload(); });
        document.body.appendChild(d);
    }, 20000);

    // links opened by plugins (window.open) -> the phone's browser
    var origOpen = window.open;
    window.open = function(url) {
        if (url && /^(https?|mailto):/i.test(String(url)) && String(url).indexOf("appassets.androidplatform.net") < 0 &&
                String(url).indexOf(location.origin + "/") !== 0) {
            B.openUrl(String(url));
            return null;
        }
        return origOpen ? origOpen.apply(window, arguments) : null;
    };
})();
