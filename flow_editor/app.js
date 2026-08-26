/*
 * 流程编辑器前端逻辑（纯原生 JavaScript，无构建步骤、无框架依赖）。
 *
 * 为什么不用 React/Vue 之类的框架：
 *   - 生产环境是打包进 Docker 的嵌入式设备，不保证有 Node.js/npm 环境来做构建，
 *     用纯浏览器原生能跑的代码，`flow_editor/` 目录复制到哪里都能直接打开。
 *   - 编辑器逻辑本身并不复杂（增删节点/边、拖拽、表单渲染），没有必要为此引入
 *     一整套框架 + 构建链路。
 *
 * 注意：目标运行环境的 JS 引擎可能不支持较新的 ES2020+ 语法（可选链 `?.`、
 * 空值合并 `??`），本文件统一使用更保守的写法（显式 if 判断 / 三元表达式）。
 *
 * 整体结构：
 *   1. 全局状态 state
 *   2. api() 通用请求封装
 *   3. 项目/流程加载
 *   4. 画布渲染（节点 + 边）与交互（拖拽移动、连线、删除）
 *   5. 属性检查器（根据节点类型 schema 动态生成表单）
 *   6. 工具栏动作：新建 / 保存 / 校验 / 演练
 *   7. 演练轨迹回放动画
 */

(function () {
  "use strict";

  // ── 1. 全局状态 ──────────────────────────────────────────────────────────

  var state = {
    project: null,
    flowId: null,
    nodeTypes: {},            // { type: {label, category, fields, outputs} }
    graph: { nodes: [], edges: [] },
    selectedNodeId: null,
    nextIdCounter: 1,
  };

  var dragState = null;       // 拖动节点时的临时状态
  var connectState = null;    // 拉线时的临时状态
  var panState = null;        // 拖动画布空白处平移视图

  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  // ── 2. API 封装 ──────────────────────────────────────────────────────────

  function api(path, method, body) {
    return fetch(path, {
      method: method || "GET",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("服务器返回了非 JSON 响应 (HTTP " + resp.status + ")");
      });
    });
  }

  function projectBase() {
    return "/api/projects/" + encodeURIComponent(state.project);
  }

  function log(text, cls) {
    var line = document.createElement("div");
    line.className = "log-line" + (cls ? " " + cls : "");
    var ts = new Date().toLocaleTimeString();
    line.textContent = "[" + ts + "] " + text;
    var body = $("#log-body");
    body.appendChild(line);
    body.scrollTop = body.scrollHeight;
  }

  function setStatus(text, cls) {
    var el = $("#status-text");
    el.textContent = text;
    el.className = "status" + (cls ? " " + cls : "");
  }

  // ── 3. 初始化 / 项目与流程加载 ───────────────────────────────────────────

  function init() {
    bindToolbar();
    api("/api/projects").then(function (res) {
      if (!res.success || !res.projects.length) {
        setStatus("没有已注册的可视化项目", "error");
        return;
      }
      var sel = $("#sel-project");
      sel.innerHTML = "";
      res.projects.forEach(function (p) {
        var opt = document.createElement("option");
        opt.value = p; opt.textContent = p;
        sel.appendChild(opt);
      });
      selectProject(res.projects[0]);
    }).catch(function (e) {
      setStatus("加载项目列表失败: " + e.message, "error");
    });
  }

  function bindToolbar() {
    $("#sel-project").addEventListener("change", function (e) { selectProject(e.target.value); });
    $("#sel-flow").addEventListener("change", function (e) { loadFlow(e.target.value); });
    $("#btn-new-flow").addEventListener("click", createNewFlow);
    $("#btn-save").addEventListener("click", saveFlow);
    $("#btn-validate").addEventListener("click", validateFlow);
    $("#btn-dryrun").addEventListener("click", doDryRun);
    $("#btn-clear-log").addEventListener("click", function () { $("#log-body").innerHTML = ""; });
  }

  function selectProject(project) {
    state.project = project;
    Promise.all([
      api(projectBase() + "/node-types"),
      api(projectBase() + "/flows"),
    ]).then(function (results) {
      var nodeTypesRes = results[0], flowsRes = results[1];
      state.nodeTypes = nodeTypesRes.node_types || {};
      renderPalette();

      var sel = $("#sel-flow");
      sel.innerHTML = "";
      (flowsRes.flows || []).forEach(function (fid) {
        var opt = document.createElement("option");
        opt.value = fid; opt.textContent = fid;
        sel.appendChild(opt);
      });
      if (flowsRes.flows && flowsRes.flows.length) {
        loadFlow(flowsRes.flows[0]);
      } else {
        newEmptyGraph();
      }
    }).catch(function (e) {
      setStatus("加载项目 '" + project + "' 失败: " + e.message, "error");
    });
  }

  function loadFlow(flowId) {
    if (!flowId) return;
    api(projectBase() + "/flows/" + encodeURIComponent(flowId)).then(function (res) {
      if (!res.success) {
        log("加载流程失败: " + res.message, "failure");
        return;
      }
      state.flowId = flowId;
      state.graph = normalizeGraph(res.graph);
      state.selectedNodeId = null;
      $("#sel-flow").value = flowId;
      renderCanvas();
      renderInspector();
      log("已加载流程 '" + flowId + "'（" + state.graph.nodes.length + " 个节点）", "info");
    });
  }

  function normalizeGraph(graph) {
    return {
      start: graph.start || null,
      nodes: (graph.nodes || []).map(function (n, i) {
        // 没有坐标的节点（比如手写/后端生成的流程 JSON 本来不含布局信息）按网格顺序
        // 摆开，避免全部叠在同一个像素点上，看起来像"一个节点都没有/拖不动"。
        var hasX = (typeof n.x === "number");
        var hasY = (typeof n.y === "number");
        return {
          id: n.id, type: n.type, label: n.label || n.id,
          params: n.params || {},
          x: hasX ? n.x : (60 + (i % 5) * 220),
          y: hasY ? n.y : (60 + Math.floor(i / 5) * 140),
        };
      }),
      // core/flow_engine.py 的流程 JSON 规范里，边的分支字段名是 "when"
      // （见 FLOW_ENGINE_GUIDE.md），"default" 是普通边（无分支）；编辑器内部用
      // 更短的 "branch" 命名（null = 普通边），这里做双向映射，避免加载/保存时
      // 分支信息丢失导致流程图"看起来不对"。
      edges: (graph.edges || []).map(function (e, i) {
        var when = e.when;
        var branch = (when === undefined || when === null || when === "default") ? (e.branch || null) : when;
        return {
          id: e.id || ("e" + i), source: e.source, target: e.target,
          branch: branch,
        };
      }),
    };
  }

  function newEmptyGraph() {
    state.flowId = null;
    state.graph = { start: null, nodes: [], edges: [] };
    state.selectedNodeId = null;
    renderCanvas();
    renderInspector();
  }

  function createNewFlow() {
    var name = window.prompt("新流程 id（英文/数字/下划线）:", "new_flow");
    if (!name) return;
    var opt = document.createElement("option");
    opt.value = name; opt.textContent = name + "（未保存）";
    $("#sel-flow").appendChild(opt);
    $("#sel-flow").value = name;
    state.flowId = name;
    state.graph = { start: null, nodes: [], edges: [] };
    state.selectedNodeId = null;
    renderCanvas();
    renderInspector();
    log("已创建新流程 '" + name + "'，记得点保存", "info");
  }

  // ── 4. 节点面板 ──────────────────────────────────────────────────────────

  function renderPalette() {
    var list = $("#palette-list");
    list.innerHTML = "";
    var byCategory = {};
    Object.keys(state.nodeTypes).forEach(function (type) {
      var schema = state.nodeTypes[type];
      var cat = schema.category || "其它";
      if (!byCategory[cat]) byCategory[cat] = [];
      byCategory[cat].push(type);
    });
    Object.keys(byCategory).sort().forEach(function (cat) {
      var catEl = document.createElement("div");
      catEl.className = "palette-category";
      catEl.textContent = cat;
      list.appendChild(catEl);

      byCategory[cat].forEach(function (type) {
        var schema = state.nodeTypes[type];
        var item = document.createElement("div");
        item.className = "palette-item";
        item.textContent = schema.label || type;
        item.draggable = true;
        item.addEventListener("dragstart", function (e) {
          e.dataTransfer.setData("text/plain", type);
        });
        item.addEventListener("click", function () {
          addNode(type, 120 + Math.random() * 200, 120 + Math.random() * 200);
        });
        list.appendChild(item);
      });
    });

    var wrapper = $("#canvas-wrapper");
    wrapper.addEventListener("dragover", function (e) { e.preventDefault(); });
    wrapper.addEventListener("drop", function (e) {
      e.preventDefault();
      var type = e.dataTransfer.getData("text/plain");
      if (!type) return;
      var rect = wrapper.getBoundingClientRect();
      var x = e.clientX - rect.left + wrapper.scrollLeft;
      var y = e.clientY - rect.top + wrapper.scrollTop;
      addNode(type, x, y);
    });
  }

  function addNode(type, x, y) {
    var id = type + "_" + (state.nextIdCounter++);
    while (findNode(id)) { id = type + "_" + (state.nextIdCounter++); }
    var schema = state.nodeTypes[type] || { fields: [] };
    var params = {};
    (schema.fields || []).forEach(function (f) {
      if (f.default !== undefined) params[f.name] = f.default;
    });
    state.graph.nodes.push({ id: id, type: type, label: schema.label || type, params: params, x: x, y: y });
    if (!state.graph.start) state.graph.start = id;
    renderCanvas();
    selectNode(id);
  }

  function findNode(id) {
    for (var i = 0; i < state.graph.nodes.length; i++) {
      if (state.graph.nodes[i].id === id) return state.graph.nodes[i];
    }
    return null;
  }

  function deleteNode(id) {
    state.graph.nodes = state.graph.nodes.filter(function (n) { return n.id !== id; });
    state.graph.edges = state.graph.edges.filter(function (e) { return e.source !== id && e.target !== id; });
    if (state.graph.start === id) state.graph.start = state.graph.nodes.length ? state.graph.nodes[0].id : null;
    if (state.selectedNodeId === id) state.selectedNodeId = null;
    renderCanvas();
    renderInspector();
  }

  // ── 5. 画布渲染 ──────────────────────────────────────────────────────────

  function renderCanvas() {
    var nodeLayer = $("#node-layer");
    nodeLayer.innerHTML = "";

    state.graph.nodes.forEach(function (node) {
      nodeLayer.appendChild(renderNodeEl(node));
    });

    renderEdges();
  }

  function nodeSchema(node) {
    return state.nodeTypes[node.type] || { label: node.type, outputs: ["out"], fields: [] };
  }

  function renderNodeEl(node) {
    var schema = nodeSchema(node);
    var el = document.createElement("div");
    el.className = "flow-node" + (node.id === state.selectedNodeId ? " selected" : "");
    if (node._runStatus) el.className += " status-" + node._runStatus;
    el.style.left = node.x + "px";
    el.style.top = node.y + "px";
    el.dataset.nodeId = node.id;

    var typeEl = document.createElement("div");
    typeEl.className = "node-type";
    typeEl.textContent = node.type + (state.graph.start === node.id ? "  ★ 起点" : "");
    el.appendChild(typeEl);

    var labelEl = document.createElement("div");
    labelEl.className = "node-label";
    labelEl.textContent = node.label || node.id;
    el.appendChild(labelEl);

    var inPort = document.createElement("div");
    inPort.className = "node-port port-in";
    inPort.addEventListener("mouseup", function (e) {
      e.stopPropagation();
      if (connectState) finishConnect(node.id);
    });
    el.appendChild(inPort);

    var outputs = schema.outputs && schema.outputs.length ? schema.outputs : ["out"];
    outputs.forEach(function (branch, idx) {
      var outPort = document.createElement("div");
      var branchClass = (branch === "true") ? "branch-true" : (branch === "false") ? "branch-false" : "single";
      outPort.className = "node-port port-out " + branchClass;
      if (outputs.length > 1 && branchClass === "single") {
        outPort.style.top = (30 + idx * 40) + "%";
      }
      outPort.title = branch;
      outPort.addEventListener("mousedown", function (e) {
        e.stopPropagation();
        startConnect(node.id, branch === "out" ? null : branch);
      });
      el.appendChild(outPort);
    });

    el.addEventListener("mousedown", function (e) {
      if (e.target.classList.contains("node-port")) return;
      selectNode(node.id);
      dragState = {
        id: node.id,
        startX: e.clientX, startY: e.clientY,
        origX: node.x, origY: node.y,
      };
      e.preventDefault();
    });

    return el;
  }

  function renderEdges() {
    var svg = $("#edge-layer");
    svg.innerHTML = "";
    state.graph.edges.forEach(function (edge) {
      var path = edgePathEl(edge);
      if (path) svg.appendChild(path);
    });
  }

  function edgePathEl(edge) {
    var src = findNode(edge.source), tgt = findNode(edge.target);
    if (!src || !tgt) return null;

    var schema = nodeSchema(src);
    var outputs = schema.outputs && schema.outputs.length ? schema.outputs : ["out"];
    var idx = edge.branch ? outputs.indexOf(edge.branch) : 0;
    if (idx < 0) idx = 0;
    var portYRatio = outputs.length > 1 ? (0.3 + idx * 0.4) : 0.5;

    var x1 = src.x + 170;
    var y1 = src.y + 40 * portYRatio + 6;
    var x2 = tgt.x - 6, y2 = tgt.y + 30;

    var midX = (x1 + x2) / 2;
    var d = "M " + x1 + " " + y1 + " C " + midX + " " + y1 + ", " + midX + " " + y2 + ", " + x2 + " " + y2;

    var ns = "http://www.w3.org/2000/svg";
    var path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    path.setAttribute("data-edge-id", edge.id);
    var cls = "";
    if (edge.branch === "true") cls = "branch-true";
    if (edge.branch === "false") cls = "branch-false";
    path.setAttribute("class", cls);
    path.addEventListener("click", function () {
      if (window.confirm("删除这条连线？")) {
        state.graph.edges = state.graph.edges.filter(function (e) { return e.id !== edge.id; });
        renderEdges();
      }
    });
    return path;
  }

  function startConnect(sourceId, branch) {
    connectState = { sourceId: sourceId, branch: branch };
    setStatus("拖到目标节点上松开鼠标以连线（Esc 取消）");
  }

  function finishConnect(targetId) {
    if (!connectState) return;
    if (connectState.sourceId === targetId) { connectState = null; return; }
    var id = "e_" + Date.now();
    state.graph.edges = state.graph.edges.filter(function (e) {
      return !(e.source === connectState.sourceId && e.branch === connectState.branch);
    });
    state.graph.edges.push({ id: id, source: connectState.sourceId, target: targetId, branch: connectState.branch });
    connectState = null;
    setStatus("");
    renderEdges();
  }

  document.addEventListener("mousemove", function (e) {
    if (dragState) {
      var node = findNode(dragState.id);
      if (!node) return;
      var dx = e.clientX - dragState.startX, dy = e.clientY - dragState.startY;
      node.x = dragState.origX + dx;
      node.y = dragState.origY + dy;
      renderCanvas();
    }
  });
  document.addEventListener("mouseup", function () {
    dragState = null;
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && connectState) {
      connectState = null;
      setStatus("");
    }
    if (e.key === "Delete" && state.selectedNodeId) {
      deleteNode(state.selectedNodeId);
    }
  });

  function selectNode(id) {
    state.selectedNodeId = id;
    renderCanvas();
    renderInspector();
  }

  // ── 6. 属性检查器 ────────────────────────────────────────────────────────

  function renderInspector() {
    var body = $("#inspector-body");
    body.innerHTML = "";

    var node = state.selectedNodeId ? findNode(state.selectedNodeId) : null;
    if (!node) {
      body.innerHTML = '<p class="hint">点击画布上的节点以编辑参数</p>';
      return;
    }

    var schema = nodeSchema(node);

    body.appendChild(makeField("string", "id", node.id, true, function () {}));
    body.appendChild(makeField("string", "label（展示名）", node.label, false, function (v) {
      node.label = v; renderCanvas();
    }));

    var startRow = document.createElement("div");
    startRow.className = "field-row";
    var startBtn = document.createElement("button");
    startBtn.textContent = (state.graph.start === node.id) ? "★ 已是起点" : "设为起点";
    startBtn.disabled = state.graph.start === node.id;
    startBtn.addEventListener("click", function () {
      state.graph.start = node.id; renderCanvas(); renderInspector();
    });
    startRow.appendChild(startBtn);
    body.appendChild(startRow);

    (schema.fields || []).forEach(function (field) {
      var value = node.params[field.name];
      if (value === undefined) value = field.default !== undefined ? field.default : "";
      var displayValue = (field.type === "json" && typeof value !== "string") ? JSON.stringify(value) : value;
      body.appendChild(makeField(field.type, field.label || field.name, displayValue, false, function (v) {
        node.params[field.name] = coerceFieldValue(field, v);
      }, field.options));
    });

    var delBtn = document.createElement("button");
    delBtn.className = "btn-delete";
    delBtn.textContent = "🗑 删除节点";
    delBtn.addEventListener("click", function () { deleteNode(node.id); });
    body.appendChild(delBtn);
  }

  function coerceFieldValue(field, raw) {
    if (field.type === "number") {
      if (raw === "" || raw === null || raw === undefined) return null;
      var n = Number(raw);
      return isNaN(n) ? null : n;
    }
    if (field.type === "json") {
      try { return JSON.parse(raw); } catch (e) { return raw; }
    }
    return raw;
  }

  function makeField(type, labelText, value, readOnly, onChange, options) {
    var row = document.createElement("div");
    row.className = "field-row";
    var label = document.createElement("label");
    label.textContent = labelText;
    row.appendChild(label);

    var input;
    if (type === "select") {
      input = document.createElement("select");
      (options || []).forEach(function (opt) {
        var o = document.createElement("option");
        o.value = opt; o.textContent = opt;
        input.appendChild(o);
      });
      input.value = value;
    } else if (type === "json") {
      input = document.createElement("textarea");
      input.value = value;
    } else {
      input = document.createElement("input");
      input.type = (type === "number") ? "number" : "text";
      input.value = (value === null || value === undefined) ? "" : value;
    }
    if (readOnly) input.disabled = true;
    input.addEventListener("change", function () { onChange(input.value); });
    row.appendChild(input);
    return row;
  }

  // ── 7. 工具栏动作：保存 / 校验 / 演练 ───────────────────────────────────

  function currentGraphPayload() {
    return {
      start: state.graph.start,
      nodes: state.graph.nodes.map(function (n) {
        return { id: n.id, type: n.type, label: n.label, params: n.params, x: n.x, y: n.y };
      }),
      edges: state.graph.edges.map(function (e) {
        return { id: e.id, source: e.source, target: e.target, when: e.branch || "default" };
      }),
    };
  }

  function saveFlow() {
    if (!state.flowId) {
      log("请先新建或选择一个流程", "failure");
      return;
    }
    api(projectBase() + "/flows/" + encodeURIComponent(state.flowId), "POST", {
      graph: currentGraphPayload(),
    }).then(function (res) {
      if (res.success) {
        log("已保存流程 '" + state.flowId + "'", "success");
        setStatus("已保存", "ok");
      } else {
        log("保存失败: " + res.message, "failure");
        setStatus("保存失败", "error");
      }
    });
  }

  function validateFlow() {
    api(projectBase() + "/flows/" + encodeURIComponent(state.flowId || "tmp") + "/validate", "POST", {
      graph: currentGraphPayload(),
    }).then(function (res) {
      if (res.success) {
        log("校验通过，流程图结构正确", "success");
        setStatus("校验通过", "ok");
      } else {
        (res.errors || []).forEach(function (e) { log("校验错误: " + e, "failure"); });
        setStatus("校验未通过（" + (res.errors || []).length + " 个问题）", "error");
      }
    });
  }

  function doDryRun() {
    setStatus("演练中…（使用 mock 机器人，不影响生产）");
    log("开始演练…", "info");
    state.graph.nodes.forEach(function (n) { n._runStatus = null; });
    renderCanvas();

    api(projectBase() + "/flows/" + encodeURIComponent(state.flowId || "tmp") + "/dryrun", "POST", {
      graph: currentGraphPayload(), timeout: 30,
    }).then(function (res) {
      if (!res.success) {
        setStatus("演练请求失败", "error");
        log("演练请求失败: " + res.message, "failure");
        (res.errors || []).forEach(function (e) { log("  - " + e, "failure"); });
        return;
      }
      animateTrace(res.dryrun.trace).then(function () {
        setStatus("演练结束: " + res.dryrun.status, res.dryrun.success ? "ok" : "error");
        log(
          "演练结束: status=" + res.dryrun.status + " message=" + res.dryrun.message,
          res.dryrun.success ? "success" : "failure"
        );
      });
    }).catch(function (e) {
      setStatus("演练请求异常", "error");
      log("演练请求异常: " + e.message, "failure");
    });
  }

  // ── 8. 演练轨迹回放动画 ──────────────────────────────────────────────────

  function animateTrace(trace) {
    return new Promise(function (resolve) {
      var i = 0;
      function step() {
        if (i >= trace.length) { resolve(); return; }
        var rec = trace[i];
        var node = findNode(rec.node_id);
        if (node) {
          node._runStatus = rec.status === "running" ? "running" : (rec.status === "success" ? "success" : "failed");
          renderCanvas();
        }
        log("[" + rec.type + "] " + rec.label + " -> " + rec.status + (rec.message ? " (" + rec.message + ")" : ""),
            rec.status === "failed" ? "failure" : (rec.status === "success" ? "success" : "info"));
        i++;
        setTimeout(step, 120);
      }
      step();
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
