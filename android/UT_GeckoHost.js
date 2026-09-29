/* UT_GeckoHost.js - added by rpgm-to-apk to GeckoView builds (not part of the original game).
 *
 * GeckoView has no addJavascriptInterface, so window.UTAndroid (the small synchronous file
 * system used by UT_AndroidBridge.js for saves) is provided here: every call is a
 * synchronous POST to the app's loopback server (__ut__/rpc), which runs exactly the same
 * Java methods as the WebView bridge. Pause / resume / Back arrive as Server-Sent Events.
 * Loaded from index.html before the engine; does nothing if a native bridge already exists.
 */
(function() {
    "use strict";
    if (window.UTAndroid) return;

    function call(method, args) {
        var x = new XMLHttpRequest();
        x.open("POST", "__ut__/rpc", false);
        x.setRequestHeader("Content-Type", "application/json");
        x.send(JSON.stringify({m: method, a: args}));
        if (x.status !== 200) throw new Error("UTAndroid." + method + ": HTTP " + x.status);
        var res = JSON.parse(x.responseText);
        if (res.error) throw new Error("UTAndroid." + method + ": " + res.error);
        return res.r;
    }

    var bridge = {};
    ["exists", "isDir", "readText", "readBase64", "writeText", "writeBase64", "mkdir", "unlink", "rmdir",
     "rename", "readdir", "stat", "missing", "dataDir", "openUrl", "exit"].forEach(function(name) {
        bridge[name] = function() { return call(name, Array.prototype.slice.call(arguments)); };
    });
    window.UTAndroid = bridge;
    window.UT_GECKO = true;

    // MZ's main.js sends one test XHR and shows "Your browser does not allow to read local
    // files" if it has not finished by window "load". Over HTTP/1.1 Gecko uses at most six
    // connections per server, so on a slow device the test request can be queued behind the
    // engine scripts. The file server is known to work here, so mark the test as passed.
    // This listener is registered before main.js, so it runs before Main.onWindowLoad.
    window.addEventListener("load", function() {
        try {
            /* global main */
            if (typeof main !== "undefined" && main && "xhrSucceeded" in main) main.xhrSucceeded = true;
        } catch (e) { /* not MZ */ }
    });

    // Pause / resume / Back from the app. Opened after start-up, so the long-lived event
    // stream does not take one of the six connections while the engine loads.
    var handlers = {pause: "__utPause", resume: "__utResume", back: "__utBack"};
    window.addEventListener("load", function() {
        setTimeout(function() {
            try {
                var events = new EventSource("__ut__/events");
                events.addEventListener("cmd", function(e) {
                    var fn = window[handlers[e.data]];
                    if (typeof fn === "function") fn();
                });
            } catch (e) {
                console.error("[UT] no event stream: " + e);
            }
        }, 0);
    });
})();
