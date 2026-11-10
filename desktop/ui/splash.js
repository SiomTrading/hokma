// Start screen of the desktop window. It runs from the app itself (not from the engine), so it works when Python
// does not. The Rust side sends the state on the "hokma://status" event; this page only shows it.
(function () {
  "use strict";
  var tauri = window.__TAURI__;
  var invoke = tauri.core.invoke;
  var texts = {};
  var el = function (id) { return document.getElementById(id); };

  function t(key, values) {
    var text = texts[key] || key;
    Object.keys(values || {}).forEach(function (name) { text = text.split("{" + name + "}").join(values[name]); });
    return text;
  }

  function show(status) {
    var state = status.state;
    var waiting = state === "starting" || state === "slow" || state === "closing" || state === "installing";
    el("spinner").hidden = !waiting;
    el("message").textContent = status.message || "";
    el("message").className = "message" + (waiting || state === "ready" ? "" : " error");
    el("line").textContent = status.line || "";
    var retry = state === "failed" || state === "needs_tools" || state === "stopped" || state === "incomplete";
    el("retry").hidden = !retry || state === "incomplete";
    el("retry").textContent = state === "stopped" ? t("open_again") : t("retry");
    el("kill").hidden = state !== "port_busy";
    el("detail-toggle").hidden = state !== "failed";
    el("actions").hidden = el("retry").hidden && el("kill").hidden && el("detail-toggle").hidden;
    if (state !== "failed") { el("detail").hidden = true; }
  }

  function toggleDetail() {
    var box = el("detail");
    if (!box.hidden) {
      box.hidden = true;
      el("detail-toggle").textContent = t("show_detail");
      return;
    }
    invoke("engine_log").then(function (text) {
      el("detail-text").textContent = text;
      box.hidden = false;
      el("detail-toggle").textContent = t("hide_detail");
    });
  }

  function copyDetail() {
    var text = el("detail-text").textContent;
    var done = function () {
      el("copy").textContent = t("copied");
      setTimeout(function () { el("copy").textContent = t("copy"); }, 1500);
    };
    var fallback = function () {
      var area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      done();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  invoke("messages").then(function (loaded) {
    texts = loaded.texts;
    document.documentElement.lang = loaded.lang;
    el("kill").textContent = t("kill_and_retry");
    el("detail-toggle").textContent = t("show_detail");
    el("copy").textContent = t("copy");
    el("retry").addEventListener("click", function () { invoke("retry"); });
    el("kill").addEventListener("click", function () { invoke("kill_and_retry"); });
    el("detail-toggle").addEventListener("click", toggleDetail);
    el("copy").addEventListener("click", copyDetail);
    tauri.event.listen("hokma://status", function (event) { show(event.payload); });
    return invoke("current_status");
  }).then(show);
})();
