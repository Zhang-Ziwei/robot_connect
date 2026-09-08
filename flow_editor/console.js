(function () {
  var out = document.getElementById("console-out");
  var statusEl = document.getElementById("status-text");
  var zoomLabel = document.getElementById("zoom-level");
  var chkFollow = document.getElementById("chk-follow");
  var chkWrap = document.getElementById("chk-wrap");
  var btnPause = document.getElementById("btn-pause");

  var zoom = parseFloat(localStorage.getItem("consoleZoom") || "1") || 1;
  var offset = 0;
  var paused = false;
  var first = true;
  var MAX_CHARS = 1200000;

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "status" + (cls ? " " + cls : "");
  }

  function applyZoom() {
    zoom = Math.max(0.5, Math.min(3, zoom));
    document.documentElement.style.setProperty("--console-zoom", String(zoom));
    zoomLabel.textContent = Math.round(zoom * 100) + "%";
    localStorage.setItem("consoleZoom", String(zoom));
  }

  function stickToBottom() {
    if (chkFollow.checked) out.scrollTop = out.scrollHeight;
  }

  function appendText(text) {
    if (!text) return;
    out.appendChild(document.createTextNode(text));
    if (out.textContent.length > MAX_CHARS) {
      out.textContent = out.textContent.slice(-Math.floor(MAX_CHARS * 0.7));
    }
    stickToBottom();
  }

  function poll() {
    if (paused) return;
    var q = first ? "?tail=262144" : ("?after=" + offset);
    fetch("/api/console" + q, { cache: "no-store" }).then(function (resp) {
      return resp.json();
    }).then(function (res) {
      if (!res.success) {
        setStatus(res.message || "读取失败", "error");
        return;
      }
      if (!res.exists) {
        if (first) {
          out.textContent = "";
          var hint = document.createElement("span");
          hint.className = "empty-hint";
          hint.textContent = res.hint || "还没有主程序控制台日志。请启动 main.py。";
          out.appendChild(hint);
        }
        setStatus("等待 main.py…", "error");
        first = false;
        offset = 0;
        return;
      }
      if (typeof res.offset === "number") offset = res.offset;
      if (res.text) {
        if (first && out.querySelector(".empty-hint")) out.textContent = "";
        appendText(res.text);
      }
      first = false;
      setStatus("实时 · " + (res.path || "logs/main_console.log"), "ok");
    }).catch(function (e) {
      setStatus("连接失败: " + e.message, "error");
    });
  }

  document.getElementById("btn-zoom-out").addEventListener("click", function () {
    zoom -= 0.1; applyZoom();
  });
  document.getElementById("btn-zoom-in").addEventListener("click", function () {
    zoom += 0.1; applyZoom();
  });
  document.getElementById("btn-zoom-fit").addEventListener("click", function () {
    zoom = 1; applyZoom();
  });
  window.addEventListener("wheel", function (e) {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    zoom += (e.deltaY < 0 ? 0.1 : -0.1);
    applyZoom();
  }, { passive: false });

  chkWrap.addEventListener("change", function () {
    out.classList.toggle("wrap", chkWrap.checked);
  });
  chkFollow.addEventListener("change", function () {
    if (chkFollow.checked) stickToBottom();
  });
  btnPause.addEventListener("click", function () {
    paused = !paused;
    btnPause.textContent = paused ? "继续" : "暂停";
    if (!paused) poll();
  });
  document.getElementById("btn-clear").addEventListener("click", function () {
    out.textContent = "";
  });

  applyZoom();
  poll();
  setInterval(poll, 600);
})();
