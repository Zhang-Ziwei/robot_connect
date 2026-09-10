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
 *   7. 演练：流式接收节点事件并高亮
 *   8. 交互式使用说明（线框高亮带路）
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
    selectedEdgeId: null,     // 选中的连线（与 selectedNodeId 互斥，同一时刻只选一个）
    nextIdCounter: 1,
    dirty: false,             // 画布有未保存修改（切换流程/关页面前据此拦截）
    exclusiveEnabled: true,   // 本项目是否只允许一份流程激活（WRC/CONST）
    enabledById: {},          // { flowId: true/false } 下拉框「激活」标记
    roleById: {},             // { flowId: 'entry' | 'subflow' } 子流程没有独立入口，不能激活
    zoom: 1,                  // 画布缩放比，1 = 100%
    dryrunActive: false,
    dryrunAbort: null,
  };

  // ── 功能区块配色 ────────────────────────────────────────────────────────
  //
  // 按节点 schema 的 category 给节点上色，让"控制流 / 机器人动作 / ..."在画布上
  // 一眼能分辨。已知分类显式指定颜色（保证同一分类在面板和画布上颜色一致）；
  // 项目自定义的新分类落到 FALLBACK_PALETTE，按分类名排序稳定取色，
  // 避免每次刷新颜色乱跳。
  var CATEGORY_COLORS = {
    "控制流": "#4c8dff",
    "机器人动作": "#35c471",
    "流程辅助": "#c07cf0",
    "状态记录": "#f2c14e",
  };
  var FALLBACK_PALETTE = ["#e0794a", "#4ec5c1", "#d4649a", "#8a94f0", "#9fbf3f"];

  function categoryColor(cat) {
    if (!cat) return "#7a7f92";
    if (CATEGORY_COLORS[cat]) return CATEGORY_COLORS[cat];
    var unknown = {};
    Object.keys(state.nodeTypes).forEach(function (t) {
      var c = state.nodeTypes[t].category;
      if (c && !CATEGORY_COLORS[c]) unknown[c] = true;
    });
    var idx = Object.keys(unknown).sort().indexOf(cat);
    return FALLBACK_PALETTE[(idx < 0 ? 0 : idx) % FALLBACK_PALETTE.length];
  }

  function nodeColor(node) {
    var schema = state.nodeTypes[node.type];
    return categoryColor(schema ? schema.category : null);
  }

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
        if (resp.status === 404) {
          throw new Error("读取位姿接口不存在 (HTTP 404)。请重启 Flow API（python3 -m network.flow_api_server）后再试");
        }
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

  // ── 未保存修改的标记与拦截 ──────────────────────────────────────────────
  //
  // 画布上的任何改动（增删节点/连线、改参数、拖动位置）都只存在于浏览器内存里，
  // 不点"保存"就不会写回服务器。这里用一个 dirty 标记统一管理：
  //   - 流程下拉框里追加"（未保存）"，让人一眼看出当前状态
  //   - 切换流程/项目、关闭页面前弹窗拦截，避免辛苦连的线白连

  function markDirty() {
    if (!state.dirty) {
      state.dirty = true;
      updateUnsavedUi();
    }
  }

  function markClean() {
    state.dirty = false;
    updateUnsavedUi();
  }

  function updateUnsavedUi() {
    var sel = $("#sel-flow");
    if (sel) {
      for (var i = 0; i < sel.options.length; i++) {
        var opt = sel.options[i];
        var tags = [];
        if (state.roleById[opt.value] === "subflow") tags.push("子流程");
        else if (state.enabledById[opt.value]) tags.push("激活");
        if (state.dirty && opt.value === state.flowId) tags.push("未保存");
        opt.textContent = opt.value + (tags.length ? "（" + tags.join("·") + "）" : "");
      }
    }
    var btn = $("#btn-save");
    if (btn) {
      btn.classList.remove("save-dirty", "save-clean");
      btn.classList.add(state.dirty ? "save-dirty" : "save-clean");
      btn.title = state.dirty
        ? "有未保存修改，点击写入服务器（图形改动只有保存后才生效）"
        : "当前流程已保存";
    }
    var enBtn = $("#btn-flow-enabled");
    if (enBtn) {
      var isSub = state.roleById[state.flowId] === "subflow";
      var on = !isSub && !!(state.flowId && (state.graph.enabled || state.enabledById[state.flowId]));
      enBtn.disabled = !state.flowId || isSub;
      enBtn.classList.toggle("flow-on", on);
      enBtn.classList.toggle("flow-off", !on);
      enBtn.textContent = isSub ? "— 子流程" : (on ? "● 已激活" : "○ 激活");
      enBtn.title = isSub
        ? "子流程由别的流程用「调用子流程」节点调用，没有自己的启动入口，不需要激活。"
        : on
        ? (state.exclusiveEnabled
            ? "这条命令只会跑这份流程。再点一次关闭激活。"
            : "这份流程会响应对应的外部命令。再点一次关闭激活。")
        : "打开后，PROCESS_BEGINS / 对应业务命令才会跑这份图。新建的备选/测试图默认不激活。";
    }
  }

  /**
   * 有未保存修改时先弹窗询问；没有就直接执行 onProceed。
   * 三个选项分别对应"先存再走 / 丢弃改动 / 留在原地"。
   */
  function guardUnsaved(onProceed, onCancel) {
    if (!state.dirty) { onProceed(); return; }
    showModal({
      title: "有未保存的修改",
      message: "流程 '" + (state.flowId || "（未命名）") + "' 有未保存的修改，继续操作会丢失这些改动。",
      buttons: [
        {
          text: "保存并继续", primary: true,
          action: function () {
            saveFlow(function (ok) {
              if (ok) onProceed();
              else if (onCancel) onCancel();
            });
          },
        },
        { text: "放弃修改", danger: true, action: onProceed },
        { text: "取消", action: onCancel },
      ],
    });
  }

  /**
   * 通用模态框。
   * opts: { title, message?, content?(DOM), wide?, buttons: [{text, title, primary, danger, keepOpen, action}] }
   * 按钮的 action 收到一个 close 函数；配了 keepOpen 就得自己决定什么时候关。
   */
  function showModal(opts) {
    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.style.zIndex = String(100 + document.querySelectorAll(".modal-mask").length * 10);

    var box = document.createElement("div");
    box.className = "modal-box" + (opts.wide ? " wide" : "") + (opts.className ? " " + opts.className : "");

    var h = document.createElement("h4");
    h.textContent = opts.title;
    box.appendChild(h);

    if (opts.message) {
      var p = document.createElement("p");
      p.textContent = opts.message;
      box.appendChild(p);
    }
    if (opts.content) box.appendChild(opts.content);

    function close() {
      if (mask.parentNode) document.body.removeChild(mask);
    }

    var btnRow = document.createElement("div");
    btnRow.className = "modal-buttons";
    (opts.buttons || []).forEach(function (b) {
      var btn = document.createElement("button");
      btn.textContent = b.text;
      if (b.title) btn.title = b.title;
      if (b.primary) btn.className = "primary";
      if (b.danger) btn.className = "danger";
      btn.addEventListener("click", function () {
        if (!b.keepOpen) close();
        if (b.action) b.action(close);
      });
      btnRow.appendChild(btn);
    });
    box.appendChild(btnRow);

    mask.appendChild(box);
    document.body.appendChild(mask);
    return close;
  }

  // ── 3. 初始化 / 项目与流程加载 ───────────────────────────────────────────

  function init() {
    bindToolbar();
    bindViewEvents();
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
    $("#sel-project").addEventListener("change", function (e) {
      var target = e.target.value, prev = state.project;
      guardUnsaved(function () { selectProject(target); },
                   function () { $("#sel-project").value = prev; });
    });
    $("#sel-flow").addEventListener("change", function (e) {
      var target = e.target.value, prev = state.flowId;
      guardUnsaved(function () { loadFlow(target); },
                   function () { if (prev) $("#sel-flow").value = prev; });
    });
    $("#btn-new-flow").addEventListener("click", createNewFlow);
    $("#btn-flow-enabled").addEventListener("click", toggleFlowEnabled);
    // 包一层：直接传 saveFlow 的话，click 事件对象会被当成 done 回调传进去
    $("#btn-save").addEventListener("click", function () { saveFlow(); });
    updateUnsavedUi();
    $("#btn-export").addEventListener("click", openFlowExport);
    $("#btn-validate").addEventListener("click", validateFlow);
    $("#btn-dryrun").addEventListener("click", doDryRun);
    $("#btn-poses").addEventListener("click", openPoseEditor);
    $("#btn-run-panel").addEventListener("click", openRunControl);
    $("#btn-console").addEventListener("click", function () {
      window.open("console.html", "main-console");
    });
    $("#btn-guide").addEventListener("click", function () { startGuide(false); });
    $("#btn-clear-log").addEventListener("click", function () { $("#log-body").innerHTML = ""; });
  }

  /**
   * 重新拉取节点 schema 并刷新界面。
   * navigate 的"目标点位"等下拉框的选项由后端按当前 robot_config.json 实时生成，
   * 所以改完点位要重新拉一次，否则新点位在下拉框里看不到。
   */
  function refreshNodeTypes() {
    return api(projectBase() + "/node-types").then(function (res) {
      if (!res.node_types) return;
      state.nodeTypes = res.node_types;
      renderPalette();
      renderInspector();   // 属性面板里已展开的下拉框要立刻用上新选项
    }).catch(function (e) {
      log("刷新节点选项失败: " + e.message, "failure");
    });
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
      state.exclusiveEnabled = !!flowsRes.exclusive_enabled;
      state.enabledById = {};
      state.roleById = {};
      var entries = (flowsRes.flows || []).map(function (item) {
        return (typeof item === "string") ? { id: item, enabled: false } : item;
      });
      var startId = "";
      entries.forEach(function (entry) {
        state.enabledById[entry.id] = !!entry.enabled;
        state.roleById[entry.id] = entry.role || "entry";
        var opt = document.createElement("option");
        opt.value = entry.id;
        sel.appendChild(opt);
        if (entry.enabled && !startId) startId = entry.id;
      });
      if (!startId && entries.length) startId = entries[0].id;
      if (startId) loadFlow(startId);
      else newEmptyGraph();
      updateUnsavedUi();
      maybeAutoStartTour();
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
      state.enabledById[flowId] = !!state.graph.enabled;
      state.selectedNodeId = null;
      state.selectedEdgeId = null;
      $("#sel-flow").value = flowId;
      markClean();
      renderCanvas();
      renderInspector();
      log("已加载流程 '" + flowId + "'（" + state.graph.nodes.length + " 个节点）", "info");
    });
  }

  function normalizeGraph(graph) {
    // 先原样带上编辑器不直接编辑的顶层字段（id / name / description / role 等）。
    // 不带的话，凡在编辑器里保存过一次的流程图都会丢掉这些字段——子流程的
    // role 丢了会被后端当成可独立运行的入口，进而报"同时激活了多份流程"。
    var normalized = {};
    Object.keys(graph || {}).forEach(function (key) {
      if (key !== "nodes" && key !== "edges") normalized[key] = graph[key];
    });
    normalized.start = graph.start || null;
    normalized.enabled = !!graph.enabled;
    normalized.context_vars = graph.context_vars;
    normalized.nodes = (graph.nodes || []).map(function (n, i) {
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
    });
    // core/flow_engine.py 的流程 JSON 规范里，边的分支字段名是 "when"
    // （见 FLOW_ENGINE_GUIDE.md），"default" 是普通边（无分支）；编辑器内部用
    // 更短的 "branch" 命名（null = 普通边），这里做双向映射，避免加载/保存时
    // 分支信息丢失导致流程图"看起来不对"。
    normalized.edges = (graph.edges || []).map(function (e, i) {
      var when = e.when;
      var branch = (when === undefined || when === null || when === "default") ? (e.branch || null) : when;
      return {
        id: e.id || ("e" + i), source: e.source, target: e.target,
        branch: branch,
      };
    });
    materializeParallelOnGraph(normalized);
    return normalized;
  }

  function materializeParallelOnGraph(graph) {
    if (!graph || !graph.nodes) return;
    var nodeById = {};
    graph.nodes.forEach(function (n) { nodeById[n.id] = n; });
    graph.nodes.forEach(function (node) {
      if (node.type !== "parallel") return;
      node.params = node.params || {};
      var listed = Array.isArray(node.params.branches) ? node.params.branches : [];
      listed.forEach(function (tid) {
        if (!nodeById[tid]) return;
        var has = (graph.edges || []).some(function (e) {
          return e.source === node.id && e.target === tid;
        });
        if (!has) {
          graph.edges.push({
            id: "e_par_" + node.id + "_" + tid,
            source: node.id, target: tid, branch: "branch",
          });
        }
      });
      graph.edges = (graph.edges || []).filter(function (e) {
        if (e.source !== node.id) return true;
        return !!nodeById[e.target];
      });
      var ids = [];
      graph.edges.forEach(function (e) {
        if (e.source !== node.id) return;
        e.branch = "branch";
        if (ids.indexOf(e.target) < 0) ids.push(e.target);
      });
      node.params.branches = ids;
      delete node.params.join;
    });
  }

  function syncParallelBranches() {
    materializeParallelOnGraph(state.graph);
  }

  function newEmptyGraph() {
    state.flowId = null;
    state.graph = { start: null, enabled: false, nodes: [], edges: [] };
    state.selectedNodeId = null;
    renderCanvas();
    renderInspector();
  }

  function createNewFlow() {
    guardUnsaved(function () {
      var name = window.prompt("新流程 id（可用中文/英文/数字/下划线）:", "new_flow");
      if (!name) return;
      var opt = document.createElement("option");
      opt.value = name; opt.textContent = name;
      $("#sel-flow").appendChild(opt);
      $("#sel-flow").value = name;
      state.flowId = name;
      state.graph = { start: null, enabled: !state.exclusiveEnabled, nodes: [], edges: [] };
      state.selectedNodeId = null;
      state.selectedEdgeId = null;
      state.enabledById[name] = !state.exclusiveEnabled;
      renderCanvas();
      renderInspector();
      markDirty();   // 新流程还没落盘，标成未保存
      log("已创建新流程 '" + name + "'，记得点保存。" +
          (state.exclusiveEnabled
            ? "展会项目新建图默认不激活，避免盖掉正在用的那份；要跑它请打开「激活」。"
            : "默认已激活。PROCESS_BEGINS 是否必须先发，看配置 flow_control.require_process_begins。"),
          "info");
    });
  }

  function toggleFlowEnabled() {
    if (!state.flowId) {
      log("请先选择或新建并保存一个流程", "failure");
      return;
    }
    var next = !(state.graph.enabled || state.enabledById[state.flowId]);
    api(projectBase() + "/flows/" + encodeURIComponent(state.flowId) + "/enabled", "POST", {
      enabled: next,
    }).then(function (res) {
      if (!res || !res.success) {
        log("切换激活失败: " + ((res && res.message) || "未知错误"), "failure");
        return;
      }
      state.graph.enabled = next;
      if (next && state.exclusiveEnabled) {
        Object.keys(state.enabledById).forEach(function (id) {
          state.enabledById[id] = false;
        });
        (res.disabled || []).forEach(function (id) { state.enabledById[id] = false; });
      }
      state.enabledById[state.flowId] = next;
      updateUnsavedUi();
      log(next
        ? ("已激活流程 '" + state.flowId + "'" + (state.exclusiveEnabled ? "（其它流程已关闭激活）" : ""))
        : ("已关闭 '" + state.flowId + "' 的激活，对应命令不会再跑这份图"),
        "success");
      setStatus(next ? ("已激活 " + state.flowId) : (state.flowId + " 未激活"), "ok");
    }).catch(function (e) {
      log("切换激活异常: " + e.message, "failure");
    });
  }

  // ── 4. 节点面板 ──────────────────────────────────────────────────────────

  function renderPalette() {
    var list = $("#palette-list");
    list.innerHTML = "";

    // 先按 scope 分成"通用 / 本项目专有"两大栏，栏内再按 category 分组。
    // 分栏是为了避免同名不同参的坑：不同项目对同一概念的参数可能完全不同
    // （KAIAO 的导航要选走廊中间点策略，WRC 的导航没有这些），混在一起容易用错。
    var projectName = "";
    var groups = { generic: {}, project: {} };
    Object.keys(state.nodeTypes).forEach(function (type) {
      var schema = state.nodeTypes[type];
      var bucket = schema.scope === "project" ? "project" : "generic";
      if (schema.scope === "project" && schema.project) projectName = schema.project;
      var cat = schema.category || "其它";
      if (!groups[bucket][cat]) groups[bucket][cat] = [];
      groups[bucket][cat].push(type);
    });

    [
      { key: "project", title: projectName ? projectName + " 专有" : "本项目专有",
        note: "参数按本项目定制，换项目不通用" },
      { key: "generic", title: "通用节点",
        note: "所有项目一致，可放心复用" }
    ].forEach(function (section) {
      var cats = Object.keys(groups[section.key]);
      if (!cats.length) return;

      var head = document.createElement("div");
      head.className = "palette-scope palette-scope-" + section.key;
      head.innerHTML = "<span class='palette-scope-title'></span>" +
                       "<span class='palette-scope-note'></span>";
      head.querySelector(".palette-scope-title").textContent = section.title;
      head.querySelector(".palette-scope-note").textContent = section.note;
      list.appendChild(head);

      cats.sort().forEach(function (cat) {
        var color = categoryColor(cat);
        var catEl = document.createElement("div");
        catEl.className = "palette-category";
        catEl.textContent = cat;
        catEl.style.setProperty("--cat-color", color);
        list.appendChild(catEl);

        groups[section.key][cat].forEach(function (type) {
          var schema = state.nodeTypes[type];
          var item = document.createElement("div");
          item.className = "palette-item";
          item.textContent = schema.label || type;
          item.title = (schema.label || type) + "（" + type + "）" +
                       (schema.scope === "project"
                         ? "\n仅 " + (schema.project || "本项目") + " 可用"
                         : "\n所有项目通用");
          item.style.setProperty("--cat-color", color);
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
    });

    var wrapper = $("#canvas-wrapper");
    // 点画布空白处取消选中（点节点/连线时它们各自 stopPropagation，不会走到这里）
    wrapper.addEventListener("mousedown", function (e) {
      if (e.target === wrapper || e.target.id === "node-layer" || e.target.id === "edge-layer") {
        clearSelection();
      }
    });
    wrapper.addEventListener("dragover", function (e) { e.preventDefault(); });
    wrapper.addEventListener("drop", function (e) {
      e.preventDefault();
      var type = e.dataTransfer.getData("text/plain");
      if (!type) return;
      // 画布可能被缩放过，屏幕像素要换算回图坐标（节点 x/y 存的是图坐标）
      var rect = wrapper.getBoundingClientRect();
      var x = (e.clientX - rect.left + wrapper.scrollLeft) / state.zoom;
      var y = (e.clientY - rect.top + wrapper.scrollTop) / state.zoom;
      addNode(type, x, y);
    });
  }

  // ── 画布缩放与面板折叠（不同分辨率适配）────────────────────────────────
  //
  // 外壳 UI（工具栏/两侧栏/日志）已由 CSS 按视口尺寸缩放，见 style.css 顶部。
  // 画布内容不跟着屏幕变——节点 x/y 是存进流程 JSON 的图坐标，若随屏幕缩放，
  // 同一张图在不同机器上排布观感会不一致——改由这里的 zoom 手动控制。

  var ZOOM_MIN = 0.3, ZOOM_MAX = 2.5;

  function applyZoom(z, anchorClient) {
    var wrapper = $("#canvas-wrapper");
    var old = state.zoom;
    z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
    if (Math.abs(z - old) < 1e-4) return;

    // 以鼠标位置（或视口中心）为锚点缩放，否则放大时内容会往左上角跑
    var rect = wrapper.getBoundingClientRect();
    var ax = anchorClient ? anchorClient.x - rect.left : wrapper.clientWidth / 2;
    var ay = anchorClient ? anchorClient.y - rect.top : wrapper.clientHeight / 2;
    var gx = (wrapper.scrollLeft + ax) / old;
    var gy = (wrapper.scrollTop + ay) / old;

    state.zoom = z;
    wrapper.style.setProperty("--zoom", z);
    var lvl = $("#zoom-level");
    if (lvl) lvl.textContent = Math.round(z * 100) + "%";

    wrapper.scrollLeft = gx * z - ax;
    wrapper.scrollTop = gy * z - ay;
  }

  /** 缩放到刚好装下整张图（留一点边距），空图则回到 100% */
  function zoomToFit() {
    var wrapper = $("#canvas-wrapper");
    var nodes = state.graph.nodes || [];
    if (!nodes.length) { applyZoom(1); return; }

    var maxX = 0, maxY = 0;
    nodes.forEach(function (n) {
      var box = nodeBox(n.id) || { w: 170, h: 62 };
      maxX = Math.max(maxX, (n.x || 0) + box.w);
      maxY = Math.max(maxY, (n.y || 0) + box.h);
    });
    var pad = 40;
    var z = Math.min(
      (wrapper.clientWidth - pad) / Math.max(maxX, 1),
      (wrapper.clientHeight - pad) / Math.max(maxY, 1)
    );
    applyZoom(Math.min(z, 1));      // 只缩不放，图小的时候没必要放大
    wrapper.scrollLeft = 0; wrapper.scrollTop = 0;
  }

  function togglePanel(panelSel, restoreId, restoreLabel, onRestoreClick) {
    var panel = $(panelSel);
    var collapsed = panel.classList.toggle("collapsed");
    var existing = document.getElementById(restoreId);
    if (existing) existing.remove();
    if (collapsed) {
      var btn = document.createElement("button");
      btn.id = restoreId;
      btn.className = "panel-restore";
      btn.textContent = restoreLabel;
      btn.title = "展开面板";
      btn.addEventListener("click", onRestoreClick);
      $("#main").appendChild(btn);
    }
  }

  function bindViewEvents() {
    var wrapper = $("#canvas-wrapper");
    wrapper.style.setProperty("--zoom", state.zoom);

    $("#btn-zoom-in").addEventListener("click", function () { applyZoom(state.zoom * 1.2); });
    $("#btn-zoom-out").addEventListener("click", function () { applyZoom(state.zoom / 1.2); });
    $("#btn-zoom-fit").addEventListener("click", zoomToFit);

    // Ctrl/⌘ + 滚轮缩放；不按修饰键时保持正常滚动
    wrapper.addEventListener("wheel", function (e) {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      applyZoom(state.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12),
                { x: e.clientX, y: e.clientY });
    }, { passive: false });

    // 折叠/展开是同一个 toggle，展开按钮点完自己就消失了
    var togglePalette = function () {
      togglePanel("#palette", "restore-palette", "»", togglePalette);
    };
    var toggleInspector = function () {
      togglePanel("#inspector", "restore-inspector", "«", toggleInspector);
    };
    $("#btn-collapse-palette").addEventListener("click", togglePalette);
    $("#btn-collapse-inspector").addEventListener("click", toggleInspector);
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
    markDirty();
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
    syncParallelBranches();
    markDirty();
    renderCanvas();
    renderInspector();
  }

  // ── 5. 画布渲染 ──────────────────────────────────────────────────────────

  // 画布默认 3000×2000。节点拖出这个范围（或手写 JSON 把 x 摆到 3000 以外）时，
  // 节点层 overflow 可见所以块还在，但 SVG 连线会被裁掉——看起来像「线消失了」。
  // 按节点包围盒把画布撑开，两边留空方便继续往外拖。
  var CANVAS_MIN_W = 3000, CANVAS_MIN_H = 2000, CANVAS_PAD = 480;

  function syncCanvasSize() {
    var maxX = CANVAS_MIN_W, maxY = CANVAS_MIN_H;
    (state.graph.nodes || []).forEach(function (n) {
      maxX = Math.max(maxX, (n.x || 0) + 240 + CANVAS_PAD);
      maxY = Math.max(maxY, (n.y || 0) + 140 + CANVAS_PAD);
    });
    var wrapper = $("#canvas-wrapper");
    if (!wrapper) return;
    wrapper.style.setProperty("--canvas-w", maxX + "px");
    wrapper.style.setProperty("--canvas-h", maxY + "px");
    var svg = $("#edge-layer");
    if (svg) {
      svg.setAttribute("width", String(maxX));
      svg.setAttribute("height", String(maxY));
    }
  }

  function renderCanvas() {
    var nodeLayer = $("#node-layer");
    nodeLayer.innerHTML = "";

    state.graph.nodes.forEach(function (node) {
      nodeLayer.appendChild(renderNodeEl(node));
    });

    syncCanvasSize();
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
    // 分类色以 CSS 变量下发，具体画成左侧色条还是标题文字颜色由 style.css 决定
    el.style.setProperty("--cat-color", nodeColor(node));
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
    if (node.type === "parallel") outputs = ["default"];
    outputs.forEach(function (branch, idx) {
      var outPort = document.createElement("div");
      var branchClass = "single";
      if (node.type === "parallel") branchClass = "branch-parallel";
      else if (branch === "true" || branch === "success") branchClass = "branch-true";
      else if (branch === "false" || branch === "failure") branchClass = "branch-false";
      outPort.className = "node-port port-out " + branchClass;
      if (outputs.length > 1) {
        outPort.style.top = (((idx + 0.5) / outputs.length) * 100) + "%";
        var lab = document.createElement("span");
        lab.className = "port-label";
        lab.textContent = branch;
        outPort.appendChild(lab);
      }
      outPort.title = node.type === "parallel" ? "拉出并行支路（可拉多条）" : branch;
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
        moved: false,
      };
      e.preventDefault();
    });

    return el;
  }

  var SVG_NS = "http://www.w3.org/2000/svg";

  // 箭头是 <marker> 定义的，marker 不会继承引用它的连线颜色，所以按分支各定义一个。
  var ARROW_MARKERS = [
    { id: "arrow-default", cls: "" },
    { id: "arrow-true", cls: "branch-true" },
    { id: "arrow-false", cls: "branch-false" },
    { id: "arrow-parallel", cls: "branch-parallel" },
    { id: "arrow-selected", cls: "edge-selected" },
  ];

  function edgeArrowDefs() {
    var defs = document.createElementNS(SVG_NS, "defs");
    ARROW_MARKERS.forEach(function (m) {
      var marker = document.createElementNS(SVG_NS, "marker");
      marker.setAttribute("id", m.id);
      marker.setAttribute("viewBox", "0 0 10 10");
      // refX=9 让箭头尖端（而不是尾部）落在连线终点上，正好抵住目标节点的入端口
      marker.setAttribute("refX", "9");
      marker.setAttribute("refY", "5");
      marker.setAttribute("markerWidth", "6");
      marker.setAttribute("markerHeight", "6");
      // 跟随连线末端切线方向旋转，曲线从哪个角度进来箭头就朝哪个方向
      marker.setAttribute("orient", "auto-start-reverse");
      var tip = document.createElementNS(SVG_NS, "path");
      tip.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
      if (m.cls) tip.setAttribute("class", m.cls);
      marker.appendChild(tip);
      defs.appendChild(marker);
    });
    return defs;
  }

  function renderEdges() {
    var svg = $("#edge-layer");
    svg.innerHTML = "";
    svg.appendChild(edgeArrowDefs());
    state.graph.edges.forEach(function (edge) {
      var path = edgePathEl(edge);
      if (path) svg.appendChild(path);
    });
    parallelBranchLinks().forEach(function (link) {
      var path = parallelPathEl(link);
      if (path) svg.appendChild(path);
    });
  }

  // parallel 节点的分支不是用边表达的，而是写在 params.branches（节点 id 数组）里
  // ——引擎按这串 id 各开一条并发子链路。画布上如果什么都不画，就会出现
  // "并行起点孤零零、看不出它带起了哪几条分支"的情况，所以这里按 branches
  // 生成一批"虚拟连线"补画出来（虚线 + 独立配色，与真实边区分）。
  function parallelBranchLinks() {
    var links = [];
    var covered = {};
    (state.graph.edges || []).forEach(function (e) {
      if (e.branch === "branch") covered[e.source + "->" + e.target] = true;
    });
    state.graph.nodes.forEach(function (node) {
      if (node.type !== "parallel") return;
      var branches = node.params ? node.params.branches : null;
      if (typeof branches === "string") {
        try { branches = JSON.parse(branches); } catch (e) { return; }
      }
      if (!branches || !branches.length) return;
      var valid = branches.filter(function (b) { return !!findNode(b); });
      valid.forEach(function (targetId, i) {
        if (covered[node.id + "->" + targetId]) return;
        links.push({
          id: "parallel:" + node.id + ":" + targetId,
          source: node.id, target: targetId,
          index: i, total: valid.length, virtual: true,
        });
      });
    });
    return links;
  }

  // 节点宽高由内容（标签长短）决定，不是固定值，所以连线端点必须按渲染后的真实
  // 尺寸算。否则长标签节点的出线会从节点内部冒出来、甚至整条线被节点盖住
  // （node-layer 叠在 edge-layer 之上）。renderCanvas 先画节点再画边，这里能读到尺寸。
  function nodeBox(id) {
    var node = findNode(id);
    if (!node) return null;
    var el = document.querySelector('.flow-node[data-node-id="' + id + '"]');
    return {
      x: node.x, y: node.y,
      w: el ? el.offsetWidth : 170,
      h: el ? el.offsetHeight : 62,
    };
  }

  function curveBetween(x1, y1, x2, y2) {
    var midX = (x1 + x2) / 2;
    return "M " + x1 + " " + y1 + " C " + midX + " " + y1 + ", " + midX + " " + y2 + ", " + x2 + " " + y2;
  }

  function makeEdgePath(d, edgeId, cls, arrow, onClick) {
    var path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("data-edge-id", edgeId);
    var selected = (state.selectedEdgeId === edgeId);
    path.setAttribute("class", cls + (selected ? " edge-selected" : ""));
    path.setAttribute("marker-end", "url(#" + (selected ? "arrow-selected" : arrow) + ")");
    path.addEventListener("click", function (e) {
      e.stopPropagation();
      onClick();
    });
    return path;
  }

  function edgePathEl(edge) {
    var src = nodeBox(edge.source), tgt = nodeBox(edge.target);
    if (!src || !tgt) return null;

    var schema = nodeSchema(findNode(edge.source));
    var outputs = schema.outputs && schema.outputs.length ? schema.outputs : ["out"];
    var idx = edge.branch ? outputs.indexOf(edge.branch) : 0;
    if (idx < 0) idx = 0;
    // 出端口的纵向位置要和 renderNodeEl 里给端口设的 top 百分比保持一致
    var portYRatio = outputs.length > 1 ? (0.3 + idx * 0.4) : 0.5;

    var x1 = src.x + src.w;
    var y1 = src.y + src.h * portYRatio;
    var x2 = tgt.x - 6, y2 = tgt.y + tgt.h / 2;

    var cls = "";
    var arrow = "arrow-default";
    var srcNode = findNode(edge.source);
    if (srcNode && srcNode.type === "parallel") {
      cls = "branch-parallel";
      arrow = "arrow-parallel";
    } else {
      if (edge.branch === "true") { cls = "branch-true"; arrow = "arrow-true"; }
      if (edge.branch === "false") { cls = "branch-false"; arrow = "arrow-false"; }
      if (edge.branch === "branch") { cls = "branch-parallel"; arrow = "arrow-parallel"; }
    }

    return makeEdgePath(curveBetween(x1, y1, x2, y2), edge.id, cls, arrow, function () {
      selectEdge(edge.id);
    });
  }

  function parallelPathEl(link) {
    var src = nodeBox(link.source), tgt = nodeBox(link.target);
    if (!src || !tgt) return null;
    // 起点取 parallel 节点的右下角：既和 success/failure 出端口（在右侧 30%/70%
    // 高度）错开、能一眼看出是并行分支，又保证一出发就在节点边界之外——从底边
    // 中部出发的话，去往右上方的分支得绕回来穿过节点，会被节点盖住（节点层在连线层之上）。
    // 多条分支共用同一个起点，视觉上就是"一点发散"，正好对应并行语义。
    var x1 = src.x + src.w, y1 = src.y + src.h;
    var x2 = tgt.x - 6, y2 = tgt.y + tgt.h / 2;
    // 控制点至少向右探出一段，避免目标很近时曲线向左回绕又钻回节点底下
    var c2x = Math.max(x2 - 70, x1 + 40);
    var d = "M " + x1 + " " + y1 +
            " C " + (x1 + 40) + " " + y1 + ", " + c2x + " " + y2 + ", " + x2 + " " + y2;
    return makeEdgePath(d, link.id, "branch-parallel", "arrow-parallel", function () {
      selectEdge(link.id);
    });
  }

  function startConnect(sourceId, branch) {
    connectState = { sourceId: sourceId, branch: branch };
    setStatus("拖到目标节点上松开鼠标以连线（Esc 取消）");
  }

  function finishConnect(targetId) {
    if (!connectState) return;
    if (connectState.sourceId === targetId) { connectState = null; return; }
    var srcNode = findNode(connectState.sourceId);
    var isParallel = srcNode && srcNode.type === "parallel";
    if (isParallel) {
      var dup = state.graph.edges.some(function (e) {
        return e.source === connectState.sourceId && e.target === targetId;
      });
      if (dup) { connectState = null; setStatus(""); return; }
    } else {
      state.graph.edges = state.graph.edges.filter(function (e) {
        return !(e.source === connectState.sourceId && e.branch === connectState.branch);
      });
    }
    var id = "e_" + Date.now();
    state.graph.edges.push({
      id: id,
      source: connectState.sourceId,
      target: targetId,
      branch: isParallel ? "branch" : connectState.branch,
    });
    if (isParallel) syncParallelBranches();
    connectState = null;
    markDirty();
    setStatus("");
    renderCanvas();
    renderInspector();
  }

  document.addEventListener("mousemove", function (e) {
    if (!dragState) return;
    // 点击选中时鼠标几乎总会抖几像素；不到阈值就当点击，不改坐标、不标未保存。
    if (!dragState.moved) {
      if (Math.abs(e.clientX - dragState.startX) < 4 &&
          Math.abs(e.clientY - dragState.startY) < 4) {
        return;
      }
      dragState.moved = true;
    }
    var node = findNode(dragState.id);
    if (!node) return;
    var dx = (e.clientX - dragState.startX) / state.zoom,
        dy = (e.clientY - dragState.startY) / state.zoom;
    node.x = dragState.origX + dx;
    node.y = dragState.origY + dy;
    renderCanvas();
  });
  document.addEventListener("mouseup", function () {
    if (dragState && dragState.moved) markDirty();
    dragState = null;
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && connectState) {
      connectState = null;
      setStatus("");
    }
    if (e.key === "Delete") {
      if (state.selectedEdgeId) deleteEdge(state.selectedEdgeId);
      else if (state.selectedNodeId) deleteNode(state.selectedNodeId);
    }
  });

  function selectNode(id) {
    state.selectedNodeId = id;
    state.selectedEdgeId = null;   // 节点和连线同一时刻只选中一个，属性面板不会二义
    renderCanvas();
    renderInspector();
  }

  function selectEdge(edgeId) {
    state.selectedEdgeId = edgeId;
    state.selectedNodeId = null;
    renderCanvas();
    renderInspector();
  }

  function clearSelection() {
    state.selectedNodeId = null;
    state.selectedEdgeId = null;
    renderCanvas();
    renderInspector();
  }

  function findEdge(edgeId) {
    for (var i = 0; i < state.graph.edges.length; i++) {
      if (state.graph.edges[i].id === edgeId) return state.graph.edges[i];
    }
    var links = parallelBranchLinks();
    for (var j = 0; j < links.length; j++) {
      if (links[j].id === edgeId) return links[j];
    }
    return null;
  }

  function deleteEdge(edgeId) {
    var edge = findEdge(edgeId);
    if (!edge) return;
    if (edge.virtual) {
      state.graph.edges = state.graph.edges.filter(function (e) {
        return !(e.source === edge.source && e.target === edge.target);
      });
    } else {
      state.graph.edges = state.graph.edges.filter(function (e) { return e.id !== edgeId; });
    }
    syncParallelBranches();
    if (state.selectedEdgeId === edgeId) state.selectedEdgeId = null;
    markDirty();
    renderCanvas();
    renderInspector();
  }

  // ── 6. 属性检查器 ────────────────────────────────────────────────────────

  function renderInspector() {
    var body = $("#inspector-body");
    body.innerHTML = "";

    if (state.selectedEdgeId) {
      renderEdgeInspector(body, state.selectedEdgeId);
      return;
    }

    var node = state.selectedNodeId ? findNode(state.selectedNodeId) : null;
    if (!node) {
      body.innerHTML = '<p class="hint">点击画布上的节点以编辑参数，点击连线可查看/删除该连线</p>';
      return;
    }

    var schema = nodeSchema(node);

    body.appendChild(makeField("string", "id", node.id, true, function () {}));
    body.appendChild(makeField("string", "label（展示名）", node.label, false, function (v) {
      if ((node.label || "") === v) return;
      node.label = v; markDirty(); renderCanvas();
    }));

    var startRow = document.createElement("div");
    startRow.className = "field-row";
    var startBtn = document.createElement("button");
    startBtn.textContent = (state.graph.start === node.id) ? "★ 已是起点" : "设为起点";
    startBtn.disabled = state.graph.start === node.id;
    startBtn.addEventListener("click", function () {
      state.graph.start = node.id; markDirty(); renderCanvas(); renderInspector();
    });
    startRow.appendChild(startBtn);
    body.appendChild(startRow);

    (schema.fields || []).forEach(function (field) {
      var value = node.params[field.name];
      if (value === undefined) value = field.default !== undefined ? field.default : "";
      var displayValue = (field.type === "json" && typeof value !== "string")
        ? JSON.stringify(value, null, 2) : value;
      // 标了 allow_free_text 的下拉框，在本项目没提供候选值时降级成文本输入，
      // 否则会出现一个永远只有占位项的空下拉框，根本没法填（比如 WRC 的步骤名
      // 本就是自由文本，没有枚举可选）。
      var fieldType = field.type;
      if (fieldType === "select" && field.allow_free_text &&
          !(field.options && field.options.length)) {
        fieldType = "text";
      }
      var row = makeField(fieldType, field.label || field.name, displayValue, false, function (v) {
        var coerced = coerceFieldValue(field, v);
        if (sameParamValue(node.params[field.name], coerced)) return;
        node.params[field.name] = coerced;
        if (node.type === "wait_for_command" && field.name === "event_name") {
          var defs = field.option_defaults || {};
          var tpl = Object.prototype.hasOwnProperty.call(defs, coerced) ? defs[coerced] : {};
          node.params.dryrun_params = JSON.parse(JSON.stringify(tpl));
          markDirty();
          renderCanvas();
          renderInspector();
          return;
        }
        markDirty();
        renderCanvas();   // params.branches 改了要重画并行分支线
      }, field.options);
      if (field.hint) {
        var hint = document.createElement("div");
        hint.className = "field-hint";
        hint.textContent = field.hint;
        row.appendChild(hint);
      }
      body.appendChild(row);
    });

    // 子流程节点：给一个直接跳进去编辑的入口。复杂功能块（比如 ConST 的装表）
    // 就藏在子流程里，主图上只看得到一个块，没有这个按钮就得回顶部下拉里翻。
    if (node.type === "sub_flow" && node.params.flow) {
      var openRow = document.createElement("div");
      openRow.className = "field-row";
      var openBtn = document.createElement("button");
      openBtn.className = "btn-open-subflow";
      openBtn.textContent = "✎ 打开子流程「" + node.params.flow + "」";
      openBtn.title = "切换到这张子流程图去编辑它的内容和顺序";
      openBtn.addEventListener("click", function () {
        var target = node.params.flow;
        guardUnsaved(function () {
          var sel = $("#flow-select");
          if (sel) sel.value = target;
          loadFlow(target);
        });
      });
      openRow.appendChild(openBtn);
      body.appendChild(openRow);
    }

    renderNodeEdgeList(body, node);

    var delBtn = document.createElement("button");
    delBtn.className = "btn-delete";
    delBtn.textContent = "🗑 删除节点";
    delBtn.addEventListener("click", function () { deleteNode(node.id); });
    body.appendChild(delBtn);
  }

  function nodeTitleOf(id) {
    var n = findNode(id);
    return n ? (n.label || n.id) : id + "（不存在）";
  }

  // 选中节点时，把与它相连的所有连线列出来，可以逐条查看（点击定位）或直接删除。
  // 画布上连线密集时，靠点线本身去选很容易点偏，这个列表是更可靠的入口。
  function renderNodeEdgeList(body, node) {
    var related = [];
    state.graph.edges.forEach(function (e) {
      if (e.source === node.id) related.push({ edge: e, dir: "out" });
      else if (e.target === node.id) related.push({ edge: e, dir: "in" });
    });
    parallelBranchLinks().forEach(function (l) {
      if (l.source === node.id) related.push({ edge: l, dir: "out" });
      else if (l.target === node.id) related.push({ edge: l, dir: "in" });
    });

    var title = document.createElement("div");
    title.className = "palette-category";
    title.textContent = "相关连线（" + related.length + "）";
    body.appendChild(title);

    if (!related.length) {
      var hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = "该节点还没有任何连线";
      body.appendChild(hint);
      return;
    }

    related.forEach(function (item) {
      var e = item.edge;
      var row = document.createElement("div");
      row.className = "edge-row";

      var desc = document.createElement("span");
      desc.className = "edge-desc";
      var other = (item.dir === "out") ? e.target : e.source;
      desc.textContent = (item.dir === "out" ? "→ " : "← ") + nodeTitleOf(other);
      desc.title = e.source + " → " + e.target;
      row.appendChild(desc);

      var when = document.createElement("span");
      when.className = "edge-when";
      var srcN = findNode(e.source);
      when.textContent = (e.virtual || (srcN && srcN.type === "parallel")) ? "并行支路" : (e.branch || "default");
      row.appendChild(when);

      var viewBtn = document.createElement("button");
      viewBtn.textContent = "查看";
      viewBtn.addEventListener("click", function () { selectEdge(e.id); });
      row.appendChild(viewBtn);

      var delBtn = document.createElement("button");
      delBtn.textContent = "删除";
      delBtn.addEventListener("click", function () { deleteEdge(e.id); });
      row.appendChild(delBtn);

      body.appendChild(row);
    });
  }

  function renderEdgeInspector(body, edgeId) {
    var edge = findEdge(edgeId);
    if (!edge) {
      body.innerHTML = '<p class="hint">该连线已不存在</p>';
      return;
    }

    var srcNode = findNode(edge.source);
    var isParallelLine = edge.virtual || (srcNode && srcNode.type === "parallel");

    var title = document.createElement("div");
    title.className = "palette-category";
    title.textContent = isParallelLine ? "并行支路" : "连线";
    body.appendChild(title);

    body.appendChild(makeField("string", "起点节点", nodeTitleOf(edge.source), true, function () {}));
    body.appendChild(makeField("string", "终点节点", nodeTitleOf(edge.target), true, function () {}));

    if (isParallelLine) {
      var note = document.createElement("p");
      note.className = "hint";
      note.textContent = "从并行节点拉出的线就是一条独立线程。删除这条线即撤销该支路。";
      body.appendChild(note);
    } else {
      var outputs = nodeSchema(srcNode || { type: "" }).outputs;
      outputs = (outputs && outputs.length) ? outputs : ["default"];
      body.appendChild(makeField("select", "分支（when）", edge.branch || outputs[0], false, function (v) {
        var next = (v === "default" || v === "out") ? null : v;
        if (edge.branch === next) return;
        edge.branch = next;
        markDirty();
        renderCanvas();
      }, outputs));
    }

    var jumpRow = document.createElement("div");
    jumpRow.className = "field-row";
    var jumpBtn = document.createElement("button");
    jumpBtn.textContent = "选中起点节点";
    jumpBtn.addEventListener("click", function () { selectNode(edge.source); });
    jumpRow.appendChild(jumpBtn);
    body.appendChild(jumpRow);

    var delBtn = document.createElement("button");
    delBtn.className = "btn-delete";
    delBtn.textContent = "🗑 删除这条连线";
    delBtn.addEventListener("click", function () { deleteEdge(edge.id); });
    body.appendChild(delBtn);
  }

  function sameParamValue(a, b) {
    if (a === b) return true;
    if ((a === undefined || a === null || a === "") &&
        (b === undefined || b === null || b === "")) return true;
    if (typeof a === "object" && typeof b === "object") {
      try { return JSON.stringify(a) === JSON.stringify(b); } catch (e) { return false; }
    }
    return false;
  }

  function coerceFieldValue(field, raw) {
    if (field.type === "checkbox") return !!raw;
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
    if (type === "checkbox") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = (value === true || value === "true" || value === 1 || value === "1");
    } else if (type === "select") {
      input = document.createElement("select");
      var opts = options || [];
      // 还没选过值时给个占位项。不然 select 匹配不到任何 option 会显示成一片空白，
      // 看起来像"没有选项可选"，实际上展开就有。
      if (value === undefined || value === null || value === "") {
        var placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "— 请选择（共 " + opts.length + " 项）—";
        placeholder.className = "opt-placeholder";
        input.appendChild(placeholder);
      }
      // 当前值不在候选列表里：{{变量}} 是流程图里的合法引用，不是配错；
      // 其它才标成「当前配置中不存在」（例如点位已从 robot_config 删掉）。
      if (value !== undefined && value !== null && value !== "" && opts.indexOf(value) < 0) {
        var missing = document.createElement("option");
        missing.value = value;
        var isTpl = typeof value === "string" && /^\{\{[\s\S]+\}\}$/.test(value.trim());
        missing.textContent = isTpl ? (value + "（流程变量）") : (value + "（当前配置中不存在）");
        missing.className = isTpl ? "opt-template" : "opt-missing";
        input.appendChild(missing);
      }
      opts.forEach(function (opt) {
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
    // change 要等失焦才触发。点画布/别的节点会先拆掉输入框，改动就丢了。
    // input 在每次按键/勾选时立刻写回内存；select 再听 change 兜底。
    if (type === "checkbox") {
      input.addEventListener("change", function () { onChange(input.checked); });
    } else {
      input.addEventListener("input", function () { onChange(input.value); });
      input.addEventListener("change", function () { onChange(input.value); });
    }
    row.appendChild(input);
    return row;
  }

  // ── 7. 工具栏动作：保存 / 校验 / 演练 ───────────────────────────────────

  function currentGraphPayload() {
    syncParallelBranches();
    // 同 normalizeGraph：先透传编辑器不直接编辑的顶层字段（id / name / description / role），
    // 只重建自己管的那几项。否则保存一次就把这些字段抹掉了。
    var payload = {};
    Object.keys(state.graph).forEach(function (key) {
      if (key !== "nodes" && key !== "edges") payload[key] = state.graph[key];
    });
    payload.start = state.graph.start;
    payload.enabled = !!state.graph.enabled;
    payload.nodes = state.graph.nodes.map(function (n) {
      return { id: n.id, type: n.type, label: n.label, params: n.params, x: n.x, y: n.y };
    });
    payload.edges = state.graph.edges.map(function (e) {
      return { id: e.id, source: e.source, target: e.target, when: e.branch || "default" };
    });
    if (!state.graph.context_vars) delete payload.context_vars;
    return payload;
  }

  function saveFlow(done) {
    if (!state.flowId) {
      log("请先新建或选择一个流程", "failure");
      if (done) done(false);
      return;
    }
    api(projectBase() + "/flows/" + encodeURIComponent(state.flowId), "POST", {
      graph: currentGraphPayload(),
    }).then(function (res) {
      if (res.success) {
        markClean();   // 清掉"（未保存）"标记
        if (state.graph.enabled && state.exclusiveEnabled) {
          Object.keys(state.enabledById).forEach(function (id) {
            state.enabledById[id] = false;
          });
        }
        state.enabledById[state.flowId] = !!state.graph.enabled;
        updateUnsavedUi();
        log("已保存流程 '" + state.flowId + "'", "success");
        setStatus("已保存", "ok");
      } else {
        log("保存失败: " + res.message, "failure");
        setStatus("保存失败", "error");
      }
      if (done) done(!!res.success);
    }).catch(function (e) {
      log("保存请求异常: " + e.message, "failure");
      setStatus("保存失败", "error");
      if (done) done(false);
    });
  }

  function fileStamp() {
    var d = new Date();
    function z(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + z(d.getMonth() + 1) + z(d.getDate()) + "_" +
           z(d.getHours()) + z(d.getMinutes()) + z(d.getSeconds());
  }

  function downloadJsonFile(filename, obj) {
    var blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(url);
      if (a.parentNode) a.parentNode.removeChild(a);
    }, 800);
  }

  function openFlowExport() {
    var box = document.createElement("div");
    box.className = "export-panel";

    var p = document.createElement("p");
    p.textContent = "软件更新会替换镜像里的流程图。更新前把流程输出到本机，更新后再导入即可迁回。" +
                    "若现场已把 /config/flows 挂到宿主机，日常保存本身就在机外，仍建议更新前再导出一份。";
    box.appendChild(p);

    var picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ".json,application/json";
    picker.style.display = "none";
    picker.addEventListener("change", function () {
      var file = picker.files && picker.files[0];
      picker.value = "";
      if (file) importFlowFile(file);
    });

    var actions = document.createElement("div");
    actions.className = "export-actions";

    function addAction(label, hint, primary, onClick) {
      var btn = document.createElement("button");
      if (primary) btn.className = "primary";
      var t = document.createElement("span");
      t.textContent = label;
      var s = document.createElement("small");
      s.textContent = hint;
      btn.appendChild(t);
      btn.appendChild(s);
      btn.addEventListener("click", onClick);
      actions.appendChild(btn);
    }

    addAction("输出当前流程", "一份可直接放回 flows/ 目录的 JSON", true, function () {
      exportCurrentFlow();
    });
    addAction("输出本项目全部流程", "迁移包，更新后一次性导回当前项目下的所有图", false, function () {
      exportAllFlows();
    });
    addAction("导入流程文件…", "识别单份 JSON 或全部流程迁移包", false, function () {
      picker.click();
    });
    box.appendChild(actions);
    box.appendChild(picker);

    showModal({
      title: "流程输出 / 迁移",
      content: box,
      buttons: [{ text: "关闭", action: null }],
    });
  }

  function exportCurrentFlow() {
    if (!state.flowId) {
      log("请先新建或选择一个流程再输出", "failure");
      return;
    }
    var name = state.project + "_" + state.flowId + "_" + fileStamp() + ".json";
    downloadJsonFile(name, currentGraphPayload());
    log("已输出当前流程 '" + state.flowId + "' -> " + name, "success");
    setStatus("已输出当前流程", "ok");
  }

  function exportAllFlows() {
    if (!state.project) {
      log("请先选择项目", "failure");
      return;
    }
    setStatus("正在汇总本项目流程…");
    api(projectBase() + "/flows").then(function (listRes) {
      if (!listRes.success) {
        log("导出失败: " + (listRes.message || "无法列出流程"), "failure");
        setStatus("导出失败", "error");
        return;
      }
      var ids = (listRes.flows || []).map(function (item) {
        return (typeof item === "string") ? item : item.id;
      }).filter(Boolean);
      var fetches = ids.map(function (id) {
        return api(projectBase() + "/flows/" + encodeURIComponent(id)).then(function (one) {
          return { id: id, graph: (one && one.success) ? one.graph : null };
        });
      });
      return Promise.all(fetches).then(function (rows) {
        var flows = {};
        var failed = [];
        rows.forEach(function (row) {
          if (row.graph) flows[row.id] = row.graph;
          else failed.push(row.id);
        });
        if (state.flowId) flows[state.flowId] = currentGraphPayload();
        var outIds = Object.keys(flows);
        if (!outIds.length) {
          log("当前项目没有可输出的流程", "failure");
          setStatus("导出失败", "error");
          return;
        }
        var pack = {
          format: "robot_connect.flow_pack",
          version: 1,
          exported_at: fileStamp(),
          project: state.project,
          flows: flows,
        };
        var name = state.project + "_flows_" + fileStamp() + ".json";
        downloadJsonFile(name, pack);
        log("已输出本项目 " + outIds.length + " 份流程 -> " + name, "success");
        if (failed.length) {
          log("以下流程读取失败，未写入包: " + failed.join("、"), "failure");
        }
        setStatus("已输出全部流程", "ok");
      });
    }).catch(function (e) {
      log("导出请求异常: " + e.message, "failure");
      setStatus("导出失败", "error");
    });
  }

  function guessFlowIdFromFilename(name) {
    var base = String(name || "").replace(/^.*[\\/]/, "");
    base = base.replace(/\.json$/i, "");
    base = base.replace(/_\d{8}_\d{6}$/, "");
    if (state.project && base.indexOf(state.project + "_") === 0) {
      base = base.slice(state.project.length + 1);
    }
    if (base === "flows" || !base) return "imported_flow";
    return base;
  }

  function parseImportedJson(obj, filename) {
    if (!obj || typeof obj !== "object") return null;
    if (obj.format === "robot_connect.flow_pack" ||
        (obj.flows && typeof obj.flows === "object" && !Array.isArray(obj.flows) && !obj.nodes)) {
      return { pack: true, project: obj.project || "", flows: obj.flows || {} };
    }
    if (obj.graph && obj.graph.nodes) {
      var id1 = obj.flow_id || obj.id || guessFlowIdFromFilename(filename);
      var flows1 = {};
      flows1[id1] = obj.graph;
      return { pack: false, project: obj.project || "", flows: flows1 };
    }
    if (Array.isArray(obj.nodes)) {
      var id2 = guessFlowIdFromFilename(filename);
      var flows2 = {};
      flows2[id2] = obj;
      return { pack: false, project: "", flows: flows2 };
    }
    return null;
  }

  function importFlowFile(file) {
    var reader = new FileReader();
    reader.onerror = function () {
      log("读取文件失败", "failure");
    };
    reader.onload = function () {
      var obj;
      try {
        obj = JSON.parse(String(reader.result || ""));
      } catch (e) {
        log("导入失败: JSON 无法解析 — " + e.message, "failure");
        return;
      }
      var parsed = parseImportedJson(obj, file.name);
      if (!parsed) {
        log("导入失败: 不是流程 JSON，也不是迁移包", "failure");
        return;
      }
      var ids = Object.keys(parsed.flows);
      if (!ids.length) {
        log("导入失败: 文件里没有流程图", "failure");
        return;
      }
      if (parsed.pack && parsed.project && parsed.project !== state.project) {
        if (!window.confirm(
              "这份迁移包来自项目 " + parsed.project +
              "，当前打开的是 " + state.project + "。\n\n仍要导入到当前项目吗？")) {
          return;
        }
      }
      var msg = parsed.pack
        ? ("将导入 " + ids.length + " 份流程到 " + state.project +
           "：\n" + ids.join("、") + "\n\n同名流程会被覆盖（服务器会先留一份备份）。")
        : ("将导入流程 '" + ids[0] + "'。若已存在会被覆盖（服务器会先留一份备份）。");
      if (!window.confirm(msg)) return;
      applyImportedFlows(parsed.flows, parsed.project);
    };
    reader.readAsText(file, "utf-8");
  }

  function applyImportedFlows(flows, sourceProject) {
    api(projectBase() + "/import", "POST", {
      format: "robot_connect.flow_pack",
      project: sourceProject || state.project,
      flows: flows,
    }).then(function (res) {
      if (res.success) {
        finishImport(res);
        return;
      }
      if (isMissingApi(res)) {
        return importFlowsViaSave(flows);
      }
      log("导入失败: " + (res.message || "未知错误"), "failure");
      setStatus("导入失败", "error");
      (res.warnings || []).forEach(function (w) { log(w, "failure"); });
    }).catch(function (e) {
      log("导入请求异常: " + e.message, "failure");
    });
  }

  function isMissingApi(res) {
    var msg = (res && res.message) ? String(res.message) : "";
    return msg.indexOf("Not Found:") === 0;
  }

  function finishImport(res) {
    var written = res.written || [];
    log((res.message || "导入完成") + (written.length
          ? "：" + written.map(function (w) { return w.id; }).join("、")
          : ""), "success");
    (res.warnings || []).forEach(function (w) { log(w, "info"); });
    setStatus("导入完成", "ok");
    var prefer = state.flowId;
    if (written.length && written.every(function (w) { return w.id !== prefer; })) {
      prefer = written[0].id;
    }
    markClean();
    reloadFlowList(prefer);
  }

  function importFlowsViaSave(flows) {
    var ids = Object.keys(flows);
    var written = [];
    var warnings = [];
    var chain = Promise.resolve();
    ids.forEach(function (id) {
      chain = chain.then(function () {
        return api(projectBase() + "/flows/" + encodeURIComponent(id), "POST", {
          graph: flows[id],
        }).then(function (res) {
          if (res && res.success) {
            written.push({ id: id, path: res.path || "" });
          } else {
            warnings.push("'" + id + "' 写入失败: " + ((res && res.message) || "未知错误"));
            ((res && res.errors) || []).forEach(function (e) {
              warnings.push("'" + id + "' " + e);
            });
          }
        });
      });
    });
    return chain.then(function () {
      if (!written.length) {
        log("导入失败: 没有写入任何流程", "failure");
        warnings.forEach(function (w) { log(w, "failure"); });
        setStatus("导入失败", "error");
        return;
      }
      finishImport({
        message: "已导入 " + written.length + " 份流程",
        written: written,
        warnings: warnings,
      });
    });
  }

  function reloadFlowList(preferId) {
    return api(projectBase() + "/flows").then(function (flowsRes) {
      state.exclusiveEnabled = !!flowsRes.exclusive_enabled;
      state.enabledById = {};
      state.roleById = {};
      var sel = $("#sel-flow");
      sel.innerHTML = "";
      var entries = (flowsRes.flows || []).map(function (item) {
        return (typeof item === "string") ? { id: item, enabled: false } : item;
      });
      var startId = "";
      var preferOk = false;
      entries.forEach(function (entry) {
        state.enabledById[entry.id] = !!entry.enabled;
        state.roleById[entry.id] = entry.role || "entry";
        var opt = document.createElement("option");
        opt.value = entry.id;
        sel.appendChild(opt);
        if (entry.id === preferId) preferOk = true;
        if (entry.enabled && !startId) startId = entry.id;
      });
      if (preferOk) startId = preferId;
      else if (!startId && entries.length) startId = entries[0].id;
      updateUnsavedUi();
      if (startId) loadFlow(startId);
      else newEmptyGraph();
    }).catch(function (e) {
      log("刷新流程列表失败: " + e.message, "failure");
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

  function applyTraceRecord(rec) {
    if (!rec) return;
    var node = findNode(rec.node_id);
    if (node) {
      node._runStatus = rec.status === "running" ? "running" : (rec.status === "success" ? "success" : "failed");
      renderCanvas();
    }
    log("[" + rec.type + "] " + rec.label + " -> " + rec.status + (rec.message ? " (" + rec.message + ")" : ""),
        rec.status === "failed" ? "failure" : (rec.status === "success" ? "success" : "info"));
  }

  function setDryRunButton(running) {
    var btn = $("#btn-dryrun");
    if (!btn) return;
    btn.textContent = running ? "■ 取消" : "▶ 演练";
    btn.title = running
      ? "停止当前演练"
      : "用 mock 机器人跑一遍，不会影响生产机器人";
    if (running) btn.classList.add("danger");
    else btn.classList.remove("danger");
  }

  function cancelDryRun() {
    if (!state.dryrunActive) return;
    log("正在取消演练…", "info");
    setStatus("正在取消演练…");
    if (state.dryrunAbort && typeof state.dryrunAbort.abort === "function") {
      try { state.dryrunAbort.abort(); } catch (e) {}
    }
    fetch("/api/dryrun/cancel", { method: "POST", cache: "no-store" }).catch(function () {});
  }

  function finishDryRun(dryrun) {
    if (!state.dryrunActive) return;
    state.dryrunActive = false;
    state.dryrunAbort = null;
    setDryRunButton(false);
    dryrun = dryrun || {};
    if (!dryrun.success) {
      state.graph.nodes.forEach(function (n) {
        if (n._runStatus === "running") n._runStatus = "failed";
      });
      renderCanvas();
    }
    var stopped = dryrun.status === "stopped" || dryrun.status === "cancelled";
    setStatus("演练结束: " + (dryrun.status || "unknown"), dryrun.success ? "ok" : "error");
    log(
      "演练结束: status=" + dryrun.status + " message=" + dryrun.message,
      stopped ? "info" : (dryrun.success ? "success" : "failure")
    );
  }

  function readDryRunStream(resp) {
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buf = "";
    var finished = false;

    function handleLine(line) {
      line = (line || "").trim();
      if (!line) return;
      var ev;
      try { ev = JSON.parse(line); } catch (e) {
        log("演练流解析失败: " + line, "failure");
        return;
      }
      if (ev.type === "node") {
        applyTraceRecord(ev.record);
      } else if (ev.type === "done") {
        finished = true;
        finishDryRun(ev.dryrun);
      }
    }

    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) {
          if (buf) handleLine(buf);
          if (!finished) {
            finishDryRun({ success: false, status: "stopped", message: "演练已结束（连接关闭）" });
          }
          return;
        }
        buf += decoder.decode(chunk.value, { stream: true });
        var parts = buf.split("\n");
        buf = parts.pop();
        parts.forEach(handleLine);
        return pump();
      });
    }
    return pump();
  }

  function normalizeDryrunPayload(parsed) {
    if (Array.isArray(parsed)) return { jobs: parsed };
    if (parsed && typeof parsed === "object" && parsed.cmd_type &&
        ("params" in parsed || "extra" in parsed)) {
      var data = {};
      var p = parsed.params;
      var extra = (parsed.extra && typeof parsed.extra === "object") ? parsed.extra : {};
      if (Array.isArray(p)) data.jobs = p;
      else if (p && typeof p === "object") {
        Object.keys(p).forEach(function (k) { data[k] = p[k]; });
      }
      Object.keys(extra).forEach(function (k) {
        if (data[k] === undefined) data[k] = extra[k];
      });
      return data;
    }
    return (parsed && typeof parsed === "object") ? parsed : {};
  }

  function waitCommandNodes() {
    return (state.graph.nodes || []).filter(function (n) { return n.type === "wait_for_command"; });
  }

  function startDryRun(signals, timeoutSec) {
    if (state.dryrunActive) {
      cancelDryRun();
      return;
    }
    state.dryrunActive = true;
    setDryRunButton(true);
    setStatus("演练中…（再点「取消」可提前结束）");
    log("开始演练…", "info");
    (signals || []).forEach(function (s) {
      log("演练注入 " + s.event + " " + JSON.stringify(s.data), "info");
    });
    state.graph.nodes.forEach(function (n) { n._runStatus = null; });
    renderCanvas();

    var abortCtl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    state.dryrunAbort = abortCtl;
    var url = projectBase() + "/flows/" + encodeURIComponent(state.flowId || "tmp") + "/dryrun";
    var opts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        graph: currentGraphPayload(),
        timeout: timeoutSec || 60,
        signals: signals || [],
      }),
    };
    if (abortCtl) opts.signal = abortCtl.signal;
    fetch(url, opts).then(function (resp) {
      var ctype = (resp.headers.get("Content-Type") || "");
      if (ctype.indexOf("ndjson") !== -1) {
        return readDryRunStream(resp);
      }
      return resp.json().then(function (res) {
        if (!res.success) {
          setStatus("演练请求失败", "error");
          log("演练请求失败: " + res.message, "failure");
          (res.errors || []).forEach(function (e) { log("  - " + e, "failure"); });
          finishDryRun({ success: false, status: "error", message: res.message || "演练请求失败" });
          return;
        }
        (res.dryrun.trace || []).forEach(applyTraceRecord);
        finishDryRun(res.dryrun);
      });
    }).catch(function (e) {
      var aborted = e && (e.name === "AbortError" || (String(e.message || "").indexOf("abort") >= 0));
      if (aborted) {
        finishDryRun({ success: false, status: "stopped", message: "演练已取消" });
        return;
      }
      setStatus("演练请求异常", "error");
      log("演练请求异常: " + e.message, "failure");
      finishDryRun({ success: false, status: "error", message: e.message || "演练请求异常" });
    });
  }

  function doDryRun() {
    if (state.dryrunActive) {
      cancelDryRun();
      return;
    }
    var waits = waitCommandNodes();
    if (!waits.length) {
      startDryRun([], 60);
      return;
    }

    var content = document.createElement("div");
    content.className = "dryrun-inputs";
    var hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "改下面的 JSON 就能换演练场景（货架格、零件种类/数量等）。" +
      "也可以直接粘贴 test_commands 里的整包命令。开始后会写回对应等待节点，记得保存流程。";
    content.appendChild(hint);

    var editors = [];
    waits.forEach(function (node) {
      var eventName = (node.params && node.params.event_name) || "";
      var block = document.createElement("div");
      block.className = "dryrun-signal";
      var title = document.createElement("div");
      title.className = "palette-category";
      title.textContent = (node.label || node.id) + "  ←  " + (eventName || "（未填命令名）");
      block.appendChild(title);
      var ta = document.createElement("textarea");
      ta.className = "dryrun-payload";
      var seed = (node.params && node.params.dryrun_params != null) ? node.params.dryrun_params : {};
      ta.value = (typeof seed === "string") ? seed : JSON.stringify(seed, null, 2);
      block.appendChild(ta);
      content.appendChild(block);
      editors.push({ node: node, eventName: eventName, textarea: ta });
    });

    var timeoutRow = document.createElement("div");
    timeoutRow.className = "field-row";
    var timeoutLabel = document.createElement("label");
    timeoutLabel.textContent = "演练上限（秒）";
    timeoutRow.appendChild(timeoutLabel);
    var timeoutInput = document.createElement("input");
    timeoutInput.type = "number";
    timeoutInput.value = "60";
    timeoutInput.min = "5";
    timeoutRow.appendChild(timeoutInput);
    content.appendChild(timeoutRow);

    var errBox = document.createElement("div");
    errBox.className = "pose-errors";
    content.appendChild(errBox);

    showModal({
      title: "演练输入",
      wide: true,
      content: content,
      buttons: [
        { text: "取消" },
        {
          text: "开始演练",
          primary: true,
          keepOpen: true,
          action: function (close) {
            errBox.textContent = "";
            var signals = [];
            for (var i = 0; i < editors.length; i++) {
              var ed = editors[i];
              if (!ed.eventName) {
                errBox.textContent = "节点「" + (ed.node.label || ed.node.id) + "」未填写等待的命令";
                return;
              }
              var parsed;
              try {
                parsed = ed.textarea.value.trim() ? JSON.parse(ed.textarea.value) : {};
              } catch (e) {
                errBox.textContent = "「" + (ed.node.label || ed.eventName) + "」JSON 无效: " + e.message;
                return;
              }
              var data = normalizeDryrunPayload(parsed);
              ed.node.params = ed.node.params || {};
              ed.node.params.dryrun_params = data;
              signals.push({ event: ed.eventName, data: data });
            }
            markDirty();
            var timeoutSec = Number(timeoutInput.value);
            if (!timeoutSec || timeoutSec < 5) timeoutSec = 60;
            close();
            startDryRun(signals, timeoutSec);
          },
        },
      ],
    });
  }

  // ── 9. 导航点位编辑 / 当前导航位姿 ─────────────────────────────────────────
  //
  // 点位坐标存在 robot_config.json 的 navigation_poses.<项目> 段里，原本只能手改
  // 文件。这里做成表格，每行一个点位，坐标用 JSON 数组填写，支持新增/删除。
  // 格式与 infrastructure/pose_loader.py 的校验规则一致：
  //   [[x,y,z,qx,qy,qz,qw]]              单段（走一个点）
  //   [[...],[...]]                      多段（途经点，按顺序走）
  //
  // 当前导航位姿放在点位弹窗顶部单独一块（读取按钮 + 只读坐标），不混进每一行。

  function openPoseEditor() {
    api(projectBase() + "/poses").then(function (res) {
      if (!res.success) {
        log("读取点位失败: " + res.message, "failure");
        return;
      }
      buildPoseEditor(res.poses || {}, res.path);
    }).catch(function (e) {
      log("读取点位异常: " + e.message, "failure");
    });
  }

  function buildPoseEditor(poses, path) {
    var content = document.createElement("div");

    var live = document.createElement("div");
    live.className = "live-pose-panel";
    var liveTitle = document.createElement("div");
    liveTitle.className = "palette-category";
    liveTitle.textContent = "当前导航位姿";
    live.appendChild(liveTitle);
    var liveHint = document.createElement("p");
    liveHint.className = "hint";
    liveHint.textContent = "订阅 /zj_humanoid/navigation/odom_info，只显示、不写入下面的点位表。导航未开时读不到。";
    live.appendChild(liveHint);
    var liveRow = document.createElement("div");
    liveRow.className = "live-pose-toolbar";
    var liveBtn = document.createElement("button");
    liveBtn.className = "primary";
    liveBtn.textContent = "读取当前位置";
    liveRow.appendChild(liveBtn);
    var liveStatus = document.createElement("span");
    liveStatus.className = "hint";
    liveRow.appendChild(liveStatus);
    live.appendChild(liveRow);
    var liveDisplay = document.createElement("textarea");
    liveDisplay.className = "live-pose-coord";
    liveDisplay.rows = 2;
    liveDisplay.readOnly = true;
    liveDisplay.placeholder = "尚未读取";
    live.appendChild(liveDisplay);
    liveBtn.addEventListener("click", function () {
      liveBtn.disabled = true;
      liveStatus.textContent = "读取中…";
      api(projectBase() + "/current-pose").then(function (res) {
        liveBtn.disabled = false;
        if (!res.success) {
          liveStatus.textContent = res.message || "读取失败";
          log("读取当前位姿失败: " + (res.message || ""), "failure");
          return;
        }
        liveDisplay.value = JSON.stringify(res.pose);
        liveStatus.textContent = (res.robot_id || "") + (res.host ? "  " + res.host : "");
        log("当前导航位姿 " + liveDisplay.value, "success");
      }).catch(function (e) {
        liveBtn.disabled = false;
        liveStatus.textContent = e.message;
        log("读取当前位姿异常: " + e.message, "failure");
      });
    });
    content.appendChild(live);

    var tip = document.createElement("p");
    tip.className = "hint";
    tip.textContent = "配置文件: " + path +
      "　坐标格式 [[x,y,z,qx,qy,qz,qw]]，多段途经点就写多个数组。";
    content.appendChild(tip);

    var list = document.createElement("div");
    list.className = "pose-list";
    content.appendChild(list);

    function addRow(name, value) {
      var row = document.createElement("div");
      row.className = "pose-row";

      var nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.className = "pose-name";
      nameInput.value = name || "";
      nameInput.placeholder = "点位名";
      row.appendChild(nameInput);

      var coordInput = document.createElement("textarea");
      coordInput.className = "pose-coord";
      coordInput.rows = 1;
      coordInput.value = JSON.stringify(value === undefined ? [[0, 0, 0, 0, 0, 0, 1]] : value);
      row.appendChild(coordInput);

      var del = document.createElement("button");
      del.textContent = "删除";
      del.addEventListener("click", function () { list.removeChild(row); });
      row.appendChild(del);

      list.appendChild(row);
      return row;
    }

    Object.keys(poses).sort().forEach(function (name) { addRow(name, poses[name]); });

    var addBtn = document.createElement("button");
    addBtn.textContent = "＋ 新增点位";
    addBtn.addEventListener("click", function () {
      addRow("", undefined).querySelector(".pose-name").focus();
    });
    content.appendChild(addBtn);

    var errBox = document.createElement("div");
    errBox.className = "pose-errors";
    content.appendChild(errBox);

    function collect() {
      var out = {}, errs = [];
      $all(".pose-row").forEach(function (row) {
        var name = row.querySelector(".pose-name").value.trim();
        var raw = row.querySelector(".pose-coord").value.trim();
        if (!name && !raw) return;
        if (!name) { errs.push("有一行没填点位名"); return; }
        try {
          out[name] = JSON.parse(raw);
        } catch (e) {
          errs.push("点位 '" + name + "' 的坐标不是合法 JSON");
        }
      });
      return { poses: out, errors: errs };
    }

    showModal({
      title: "导航点位（" + state.project + "）",
      wide: true,
      content: content,
      buttons: [
        {
          text: "💾 保存点位", primary: true, keepOpen: true,
          action: function (close) {
            var got = collect();
            if (got.errors.length) {
              errBox.textContent = got.errors.join("；");
              return;
            }
            api(projectBase() + "/poses", "POST", { poses: got.poses }).then(function (res) {
              if (!res.success) {
                errBox.textContent = (res.errors || [res.message]).join("；");
                return;
              }
              close();
              log("点位已保存 -> " + res.path, "success");
              log(res.message, "info");
              setStatus("点位已保存", "ok");
              // 点位变了，navigate 等节点的"目标点位"下拉框要跟着更新
              refreshNodeTypes();
            }).catch(function (e) {
              errBox.textContent = "保存请求异常: " + e.message;
            });
          },
        },
        { text: "取消", action: null },
      ],
    });
  }

  // ── 10. 真实流程运行控制 ─────────────────────────────────────────────────
  //
  // 「启动」发 START_WORKING：连接机器人、下发地图，完成后随时可收业务命令。
  // 「重置」发 RESET_SYSTEM：清调度记忆并休眠，不是发给机器人的业务动作。
  // 业务命令走模拟 HTTP 发送或外部设备。顶部「▶ 演练」走 mock，不经过这里。

  var runControlCloser = null;

  function openRunControl() {
    lastProjectMismatch = "";
    var shell = document.createElement("div");
    shell.className = "run-shell";

    var main = document.createElement("div");
    main.className = "run-main";

    var head = document.createElement("div");
    head.className = "run-head";
    var title = document.createElement("h4");
    title.textContent = "真实流程运行控制";
    head.appendChild(title);
    var mockSwitch = buildMockSwitch();
    head.appendChild(mockSwitch.root);
    var btnConfig = document.createElement("button");
    btnConfig.className = "run-head-toggle";
    btnConfig.textContent = "配置文件";
    btnConfig.title = "编辑当前项目配置：KAIAO 走廊导航中间点（带字段注释）以及 robot_config.json。保存后需重置系统或重启 main.py 才生效。";
    btnConfig.addEventListener("click", openRobotConfigEditor);
    head.appendChild(btnConfig);
    var btnToggleConsole = document.createElement("button");
    btnToggleConsole.className = "run-head-toggle";
    btnToggleConsole.textContent = "主程序输出";
    btnToggleConsole.title = "展开或折叠右侧主程序终端输出。";
    head.appendChild(btnToggleConsole);
    main.appendChild(head);

    var sendPanel = document.createElement("section");
    sendPanel.className = "run-panel send-panel";
    var sendTitle = document.createElement("h5");
    sendTitle.className = "run-panel-title";
    sendTitle.textContent = "模拟 HTTP 发送";
    sendTitle.title = "等同 WCS / curl 打到命令端口";
    sendPanel.appendChild(sendTitle);
    var sendFrame = document.createElement("div");
    sendFrame.className = "run-panel-frame";
    var sendMain = document.createElement("div");
    sendMain.className = "run-panel-main";
    var sendSide = document.createElement("div");
    sendSide.className = "run-panel-side";
    sendFrame.appendChild(sendMain);
    sendFrame.appendChild(sendSide);
    sendPanel.appendChild(sendFrame);
    main.appendChild(sendPanel);

    var statePanel = document.createElement("section");
    statePanel.className = "run-panel state-panel";
    var stateTitle = document.createElement("h5");
    stateTitle.className = "run-panel-title";
    stateTitle.textContent = "机器人状态";
    stateTitle.title = "查询主程序当前任务/流程状态（GET_TASK_STATE）";
    statePanel.appendChild(stateTitle);
    var stateFrame = document.createElement("div");
    stateFrame.className = "run-panel-frame";
    var stateBox = document.createElement("pre");
    stateBox.className = "run-state";
    stateBox.textContent = "查询中…";
    var stateSide = document.createElement("div");
    stateSide.className = "run-panel-side";
    var btnRefresh = document.createElement("button");
    btnRefresh.textContent = "状态刷新";
    btnRefresh.title = "向主程序查询当前任务/流程状态（GET_TASK_STATE），不向机器人发业务命令。";
    btnRefresh.addEventListener("click", function () {
      refreshRuntimeState(stateBox);
    });
    stateSide.appendChild(btnRefresh);
    stateFrame.appendChild(stateBox);
    stateFrame.appendChild(stateSide);
    statePanel.appendChild(stateFrame);
    main.appendChild(statePanel);

    var echo = document.createElement("p");
    echo.className = "run-echo";
    echo.textContent = " ";
    main.appendChild(echo);

    var dock = document.createElement("div");
    dock.className = "run-dock";
    var dockPower = document.createElement("div");
    dockPower.className = "run-dock-power";
    var dockFlow = document.createElement("div");
    dockFlow.className = "run-dock-flow";
    var dockInterrupt = document.createElement("div");
    dockInterrupt.className = "run-dock-interrupt";
    var dockClose = document.createElement("div");
    dockClose.className = "run-dock-close";
    dock.appendChild(dockPower);
    dock.appendChild(dockFlow);
    dock.appendChild(dockInterrupt);
    dock.appendChild(dockClose);
    main.appendChild(dock);

    var consoleCol = document.createElement("aside");
    consoleCol.className = "run-console-col";
    var consoleHead = document.createElement("div");
    consoleHead.className = "run-console-head";
    var consoleTitle = document.createElement("strong");
    consoleTitle.textContent = "主程序输出";
    var consoleStatus = document.createElement("span");
    consoleStatus.textContent = "连接中…";
    consoleHead.appendChild(consoleTitle);
    consoleHead.appendChild(consoleStatus);
    var consoleOut = document.createElement("pre");
    consoleOut.className = "run-console-out";
    consoleCol.appendChild(consoleHead);
    consoleCol.appendChild(consoleOut);

    shell.appendChild(main);
    shell.appendChild(consoleCol);

    var consoleCtl = attachEmbeddedConsole(consoleOut, consoleStatus);
    var closeModal = showModal({
      title: "真实流程运行控制",
      wide: true,
      className: "run-modal",
      content: shell,
      buttons: [],
    });
    var origClose = closeModal;
    closeModal = function () {
      consoleCtl.stop();
      mockSwitch.stop();
      origClose();
      if (runControlCloser === closeModal) runControlCloser = null;
    };
    runControlCloser = closeModal;

    var btnClose = document.createElement("button");
    btnClose.textContent = "关闭";
    btnClose.title = "关闭运行控制面板。";
    btnClose.addEventListener("click", closeModal);
    dockClose.appendChild(btnClose);

    btnToggleConsole.addEventListener("click", function () {
      shell.classList.toggle("console-collapsed");
      btnToggleConsole.textContent = shell.classList.contains("console-collapsed")
        ? "显示输出" : "主程序输出";
    });

    api(projectBase() + "/run-control").then(function (res) {
      if (!res.success) {
        var fail = "加载运行控制失败: " + (res.message || "未知错误");
        setEcho(echo, "✗ " + fail, false);
        log(fail, "failure");
        return;
      }
      renderRunDock(dockPower, dockFlow, dockInterrupt, res.buttons || [], stateBox, echo);
      renderCommandSender(sendMain, sendSide, res.command_templates || [], stateBox, echo);
      if (res.hint) log(res.hint, "info");
      logProjectMismatch(res.config_active_project);
    }).catch(function (e) {
      var fail = "加载运行控制异常: " + e.message;
      setEcho(echo, "✗ " + fail, false);
      log(fail, "failure");
    });

    refreshRuntimeState(stateBox);
  }

  function buildMockSwitch() {
    var label = document.createElement("label");
    label.className = "run-mock-switch";
    label.title = "启动或关闭 mock_rosbridge/mock_rosbridge_server.py（本机 9090/9091/9092）。给运行控制连模拟机器人用。";

    var input = document.createElement("input");
    input.type = "checkbox";
    var slider = document.createElement("span");
    slider.className = "run-switch-ui";
    var text = document.createElement("span");
    text.className = "run-mock-label";
    text.textContent = "Mock";
    label.appendChild(input);
    label.appendChild(slider);
    label.appendChild(text);

    var stopped = false;
    var busy = false;

    function apply(res) {
      if (!res) return;
      input.checked = !!res.running;
      label.title = res.message || label.title;
      text.textContent = res.running ? "Mock 开" : "Mock";
    }

    function refresh() {
      if (stopped || busy) return;
      fetch("/api/mock-rosbridge", { cache: "no-store" }).then(function (r) {
        return r.json();
      }).then(apply).catch(function () {});
    }

    input.addEventListener("change", function () {
      var want = input.checked;
      if (busy) {
        input.checked = !want;
        return;
      }
      busy = true;
      input.disabled = true;
      fetch("/api/mock-rosbridge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: want }),
      }).then(function (r) { return r.json(); }).then(function (res) {
        apply(res);
        if (res && res.message) log(res.message, res.success === false ? "failure" : "info");
      }).catch(function (err) {
        input.checked = !want;
        log("Mock 开关失败: " + err.message, "failure");
      }).then(function () {
        busy = false;
        input.disabled = false;
      });
    });

    refresh();
    var timer = setInterval(refresh, 2500);
    return {
      root: label,
      stop: function () {
        stopped = true;
        clearInterval(timer);
      },
    };
  }

  function stringifyWaypointValue(field, value) {
    if (field.type === "string_list") {
      return (Array.isArray(value) ? value : []).join(", ");
    }
    if (field.type === "json") {
      try { return JSON.stringify(value); } catch (e) { return "[]"; }
    }
    if (value === undefined || value === null) return "";
    return String(value);
  }

  function collectWaypointForm(form) {
    var out = {};
    var errors = [];
    if (!form) return { values: null, errors: errors };
    (form._wpFields || []).forEach(function (field) {
      var el = form.querySelector('[data-wp-key="' + field.key + '"]');
      if (!el) return;
      var raw = el.value;
      try {
        if (field.type === "number") {
          var n = Number(String(raw).trim());
          if (!isFinite(n)) throw new Error("必须是数字");
          out[field.key] = n;
        } else if (field.type === "string_list") {
          var list = String(raw).split(/[,，\n]+/).map(function (s) {
            return s.trim();
          }).filter(Boolean);
          if (!list.length) throw new Error("至少填一个点位名");
          out[field.key] = list;
        } else if (field.type === "json") {
          var parsed = JSON.parse(String(raw).trim() || "[]");
          if (!Array.isArray(parsed)) throw new Error("必须是 JSON 数组");
          out[field.key] = parsed;
        } else {
          out[field.key] = raw;
        }
      } catch (e) {
        errors.push((field.label || field.key) + ": " + (e.message || e));
      }
    });
    return { values: out, errors: errors };
  }

  function buildWaypointForm(wp) {
    var wrap = document.createElement("div");
    wrap.className = "wp-config";
    wrap._wpFields = wp.schema || [];

    var title = document.createElement("div");
    title.className = "palette-category";
    title.textContent = "走廊导航中间点（kaiao_waypoint）";
    wrap.appendChild(title);

    var intro = document.createElement("p");
    intro.className = "hint";
    intro.textContent = (wp.intro || "") + (wp.path ? " 写入：" + wp.path : "");
    wrap.appendChild(intro);

    var lastGroup = "";
    (wp.schema || []).forEach(function (field) {
      if (field.group && field.group !== lastGroup) {
        lastGroup = field.group;
        var g = document.createElement("div");
        g.className = "wp-group-title";
        g.textContent = field.group;
        wrap.appendChild(g);
      }
      var row = document.createElement("div");
      row.className = "wp-field";
      var lab = document.createElement("label");
      lab.textContent = field.label || field.key;
      var input;
      if (field.type === "json") {
        input = document.createElement("textarea");
        input.rows = 2;
        input.className = "wp-json";
        input.spellcheck = false;
      } else {
        input = document.createElement("input");
        input.type = field.type === "number" ? "number" : "text";
        if (field.type === "number") input.step = "any";
      }
      input.setAttribute("data-wp-key", field.key);
      input.value = stringifyWaypointValue(field, (wp.values || {})[field.key]);
      lab.appendChild(input);
      row.appendChild(lab);
      if (field.comment) {
        var cmt = document.createElement("p");
        cmt.className = "wp-comment";
        cmt.textContent = field.comment;
        row.appendChild(cmt);
      }
      wrap.appendChild(row);
    });
    return wrap;
  }

  function openRobotConfigEditor() {
    var content = document.createElement("div");
    var pathLine = document.createElement("p");
    pathLine.className = "run-config-path";
    pathLine.textContent = "正在读取 robot_config.json…";
    content.appendChild(pathLine);

    var hint = document.createElement("p");
    hint.className = "run-hint-line";
    hint.textContent = "保存后正在运行的 main.py 不会立刻生效，需要「重置系统」或重启主程序。";
    content.appendChild(hint);

    var wpHost = document.createElement("div");
    content.appendChild(wpHost);

    var jsonTitle = document.createElement("div");
    jsonTitle.className = "palette-category";
    jsonTitle.textContent = "项目配置 JSON";
    content.appendChild(jsonTitle);

    var ta = document.createElement("textarea");
    ta.className = "run-config-editor";
    ta.spellcheck = false;
    ta.wrap = "off";
    content.appendChild(ta);

    var errBox = document.createElement("p");
    errBox.className = "pose-errors";
    content.appendChild(errBox);

    var wpForm = null;

    showModal({
      title: "配置文件",
      wide: true,
      className: "config-modal",
      content: content,
      buttons: [
        {
          text: "保存", primary: true, keepOpen: true,
          title: "写入磁盘上的 robot_config.json。运行中的主程序需重置或重启后才会用新配置。",
          action: function (close) {
            var cfg;
            try {
              cfg = JSON.parse(ta.value);
            } catch (e) {
              errBox.textContent = "JSON 格式错误: " + e.message;
              return;
            }
            if (!cfg || typeof cfg !== "object" || Array.isArray(cfg)) {
              errBox.textContent = "配置必须是 JSON 对象";
              return;
            }
            var payload = { config: cfg };
            if (wpForm) {
              var got = collectWaypointForm(wpForm);
              if (got.errors.length) {
                errBox.textContent = got.errors.join("；");
                return;
              }
              payload.waypoint = got.values;
            }
            errBox.textContent = "";
            api(projectBase() + "/robot-config", "POST", payload).then(function (res) {
              if (!res.success) {
                errBox.textContent = (res.message || "保存失败") +
                  (res.errors && res.errors.length ? " " + res.errors.join("；") : "");
                return;
              }
              close();
              log("配置已保存 -> " + res.path, "success");
              if (res.waypoint_path && res.waypoint_path !== res.path) {
                log("走廊导航参数 -> " + res.waypoint_path, "success");
              }
              log(res.message, "info");
              setStatus("配置已保存", "ok");
            }).catch(function (e) {
              errBox.textContent = "保存请求异常: " + e.message;
            });
          },
        },
        { text: "取消", action: null },
      ],
    });

    api(projectBase() + "/robot-config").then(function (res) {
      if (!res.success) {
        pathLine.textContent = "✗ " + (res.message || "读取失败");
        return;
      }
      var cfgObj = res.config || {};
      if (res.waypoint && res.waypoint.key) {
        cfgObj = Object.assign({}, cfgObj);
        delete cfgObj[res.waypoint.key];
        wpForm = buildWaypointForm(res.waypoint);
        wpHost.appendChild(wpForm);
        ta.classList.add("has-waypoint");
      }
      pathLine.textContent = (res.exists === false ? "文件尚不存在，保存时将创建：" : "当前文件：") + res.path;
      ta.value = formatConfigForEditor(cfgObj, res.text);
    }).catch(function (e) {
      pathLine.textContent = "✗ 读取异常: " + e.message;
    });
  }

  function isCompactNumericArray(value) {
    if (!Array.isArray(value) || !value.length) return false;
    return value.every(function (x) {
      return typeof x === "number" || (
        Array.isArray(x) && x.length && x.every(function (n) { return typeof n === "number"; })
      );
    });
  }

  function formatConfigForEditor(obj, fallbackText) {
    var frozen = {};
    var n = 0;
    function walk(value) {
      if (isCompactNumericArray(value)) {
        var token = "__NUMARR_" + (n++) + "__";
        frozen[token] = JSON.stringify(value);
        return token;
      }
      if (Array.isArray(value)) return value.map(walk);
      if (value && typeof value === "object") {
        var out = {};
        Object.keys(value).forEach(function (k) { out[k] = walk(value[k]); });
        return out;
      }
      return value;
    }
    try {
      var text = JSON.stringify(walk(obj), null, 2);
      Object.keys(frozen).forEach(function (token) {
        text = text.replace('"' + token + '"', frozen[token]);
      });
      return text;
    } catch (e) {
      return fallbackText || "{}";
    }
  }

  function groupOf(btn) {
    if (btn.group) return btn.group;
    if (btn.action === "begin" || btn.action === "reset_system") return "power";
    if (btn.action === "cancel_op" || btn.action === "cancel_nav") return "interrupt";
    return "flow";
  }

  function renderRunDock(powerBox, flowBox, interruptBox, buttons, stateBox, echo) {
    powerBox.innerHTML = "";
    flowBox.innerHTML = "";
    interruptBox.innerHTML = "";
    var hosts = { power: powerBox, flow: flowBox, interrupt: interruptBox };
    (buttons || []).forEach(function (a) {
      var host = hosts[groupOf(a)] || flowBox;
      host.appendChild(makeRunButton(a, stateBox, echo));
    });
  }

  function makeRunButton(a, stateBox, echo) {
    var btn = document.createElement("button");
    btn.textContent = a.text || a.cmd;
    if (a.danger) btn.className = "danger";
    if (a.hint) btn.title = a.hint;
    if (a.disabled) {
      btn.disabled = true;
      return btn;
    }
    btn.addEventListener("click", function () {
      if (a.confirm) {
        var msg = a.confirm_message || (
          "确定要发送 " + a.cmd + " 吗？\n\n这会作用于真实主程序。"
        );
        if (!window.confirm(msg)) return;
      }
      if (a.command) {
        sendRawCommand(a.command, a.cmd || a.command.cmd_type, stateBox, echo);
      } else {
        sendControl(a.action, a.cmd, stateBox, echo);
      }
    });
    return btn;
  }

  function renderCommandSender(mainHost, sideHost, templates, stateBox, echo) {
    mainHost.innerHTML = "";
    sideHost.innerHTML = "";
    if (!templates || !templates.length) {
      var empty = document.createElement("p");
      empty.className = "run-hint-line";
      empty.textContent = "本项目没有可模拟发送的命令模板。";
      mainHost.appendChild(empty);
      return;
    }

    var row = document.createElement("div");
    row.className = "run-send-row";
    var sel = document.createElement("select");
    templates.forEach(function (t, i) {
      var opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = t.label + (t.error ? "（加载失败）" : "");
      sel.appendChild(opt);
    });
    row.appendChild(sel);
    mainHost.appendChild(row);

    var ta = document.createElement("textarea");
    ta.spellcheck = false;
    mainHost.appendChild(ta);

    var sendBtn = document.createElement("button");
    sendBtn.className = "primary";
    sendBtn.textContent = "发送";
    sendBtn.title = "把左侧 JSON 转发到主程序命令端口（8090），等同 WCS / curl。作用于真实调度，不是顶部的演练。";
    sideHost.appendChild(sendBtn);

    function fillTemplate() {
      var t = templates[parseInt(sel.value, 10)];
      if (!t) return;
      if (t.error) {
        ta.value = "";
        setEcho(echo, "✗ 模板加载失败: " + t.error, false);
        return;
      }
      ta.value = JSON.stringify(t.command, null, 2);
    }
    sel.addEventListener("change", fillTemplate);
    fillTemplate();

    sendBtn.addEventListener("click", function () {
      var command;
      try {
        command = JSON.parse(ta.value);
      } catch (e) {
        setEcho(echo, "✗ JSON 格式错误: " + e.message, false);
        return;
      }
      if (!command || !command.cmd_type) {
        setEcho(echo, "✗ 命令缺少 cmd_type", false);
        return;
      }
      if (!window.confirm(
            "确定要发送 " + command.cmd_type + " 吗？\n\n这会作用于真实主程序。")) return;
      sendRawCommand(command, command.cmd_type, stateBox, echo);
    });
  }

  function setEcho(echo, text, ok) {
    if (!echo) return;
    echo.textContent = text;
    echo.className = "run-echo " + (ok ? "ok" : "err");
  }

  function showControlResult(cmdType, res, stateBox, echo) {
    if (!res.success) {
      setEcho(echo, "✗ " + res.message, false);
      log(cmdType + " 失败: " + res.message, "failure");
      return;
    }
    var inner = res.response || {};
    var ok = inner.success !== false;
    var extra = "";
    if (ok && cmdType === "START_WORKING") {
      extra = " 机器人正在连接并初始化，完成后随时可接收业务命令。";
    } else if (ok && cmdType === "RESET_SYSTEM") {
      extra = " 调度侧记忆已清除，系统回到休眠。需再次启动才能收命令。";
    }
    setEcho(echo, cmdType + (ok ? " 已下发。" : " 主程序返回失败。") + extra, ok);
    log(cmdType + " 已下发，主程序返回: " +
        JSON.stringify(inner.message || inner), ok ? "success" : "failure");
    refreshRuntimeState(stateBox);
  }

  function sendControl(action, cmdType, stateBox, echo) {
    setEcho(echo, "正在发送 " + cmdType + " …", true);
    api(projectBase() + "/control/" + action, "POST", {}).then(function (res) {
      showControlResult(cmdType, res, stateBox, echo);
    }).catch(function (e) {
      setEcho(echo, "✗ 请求异常: " + e.message, false);
    });
  }

  function sendRawCommand(command, cmdType, stateBox, echo) {
    setEcho(echo, "正在发送 " + cmdType + " …", true);
    api(projectBase() + "/control/send", "POST", { command: command }).then(function (res) {
      showControlResult(cmdType, res, stateBox, echo);
    }).catch(function (e) {
      setEcho(echo, "✗ 请求异常: " + e.message, false);
    });
  }

  var lastProjectMismatch = "";

  function logProjectMismatch(runtimeProject) {
    if (!runtimeProject || runtimeProject === state.project) {
      lastProjectMismatch = "";
      return;
    }
    var msg =
      "编辑器当前项目是 " + state.project + "，但主程序正在跑 " + runtimeProject +
      "。运行控制发到 8090，会按 " + runtimeProject + " 处理。" +
      "请把 infrastructure/robot_config.json 的 active_project 改成 " + state.project +
      "，然后「重置系统」再「启动」。";
    if (msg === lastProjectMismatch) return;
    lastProjectMismatch = msg;
    log(msg, "failure");
  }

  function refreshRuntimeState(stateBox) {
    stateBox.textContent = "查询中…";
    api(projectBase() + "/runtime-state").then(function (res) {
      stateBox.textContent = res.success
        ? JSON.stringify(res.response, null, 2)
        : "✗ " + res.message;
      var runtime = res.success && res.response && res.response.active_project;
      if (runtime) logProjectMismatch(runtime);
    }).catch(function (e) {
      stateBox.textContent = "✗ 查询异常: " + e.message;
    });
  }

  function attachEmbeddedConsole(outEl, statusEl) {
    var offset = 0;
    var first = true;
    var stopped = false;
    var MAX_CHARS = 400000;

    function setStatus(text) {
      if (statusEl) statusEl.textContent = text;
    }
    function appendText(text) {
      if (!text) return;
      if (outEl.querySelector(".empty-hint")) outEl.textContent = "";
      outEl.appendChild(document.createTextNode(text));
      if (outEl.textContent.length > MAX_CHARS) {
        outEl.textContent = outEl.textContent.slice(-Math.floor(MAX_CHARS * 0.7));
      }
      outEl.scrollTop = outEl.scrollHeight;
    }

    function poll() {
      if (stopped) return;
      var q = first ? "?tail=131072" : ("?after=" + offset);
      fetch("/api/console" + q, { cache: "no-store" }).then(function (resp) {
        return resp.json();
      }).then(function (res) {
        if (stopped) return;
        if (!res.success) {
          setStatus(res.message || "读取失败");
          return;
        }
        if (!res.exists) {
          if (first && !outEl.textContent.trim()) {
            var hint = document.createElement("span");
            hint.className = "empty-hint";
            hint.textContent = res.hint || "还没有主程序控制台日志。请启动 main.py。";
            outEl.appendChild(hint);
          }
          setStatus("等待 main.py…");
          first = false;
          offset = 0;
          return;
        }
        if (typeof res.offset === "number") offset = res.offset;
        if (res.text) appendText(res.text);
        first = false;
        setStatus("实时");
      }).catch(function (e) {
        if (!stopped) setStatus("连接失败: " + e.message);
      });
    }
    poll();
    var timer = setInterval(poll, 1000);
    return {
      stop: function () {
        stopped = true;
        clearInterval(timer);
      },
    };
  }

  // ── 11. 交互式使用说明 ───────────────────────────────────────────────────
  //
  // 线框高亮真实界面，按「从建图到演练再到运行控制」带路。不改用户正在编辑的流程。
  // 首次打开自动弹出一次；工具栏「使用说明」随时可再走一遍。

  var GUIDE_STORAGE_KEY = "flow_editor_guide_done";
  var tour = null;          // { root, step, onKey, onReflow }
  var tourAutoTried = false;

  function guideSteps() {
    return [
      {
        phase: 0,
        title: "欢迎使用流程编辑器",
        body: [
          "软件按「画流程图 → 先演练 → 再给主程序跑」来用。下面会用线框标出每个区域，按下一步跟着走即可。",
          "演练会自动接 mock 机器人，不会动真机。运行控制连的是主程序；新手请打开里面的 Mock 开关代替真机器人。"
        ],
        welcome: true
      },
      {
        phase: 0,
        title: "先选项目和流程",
        body: [
          "「项目」对应一套现场任务（例如 WRC_FLOW、KAIAO_FLOW、CONST_FLOW），节点种类和命令都不一样。",
          "「流程」是这个项目下的一张图。下拉框里带标记的是当前激活、命令真正会执行的那一份。"
        ],
        targets: ["#tour-project", "#tour-flow"]
      },
      {
        phase: 0,
        title: "新建一份流程",
        body: [
          "点「＋ 新建」会要一个流程 id，然后得到一张空画布。",
          "新图还没写到服务器，标题栏的保存按钮会变黄。展会类项目新建时默认不激活，以免盖掉正在跑的那份。"
        ],
        target: "#btn-new-flow"
      },
      {
        phase: 0,
        title: "激活后命令才会跑它",
        body: [
          "打开「激活」，对应命令才会执行这张图。有的项目同时只允许一份激活，打开这份会自动关掉其它。",
          "只想改图、还不想上场，可以先不激活，用后面的「演练」验证。"
        ],
        target: "#btn-flow-enabled"
      },
      {
        phase: 1,
        title: "从这里拖功能块",
        body: [
          "左侧是节点面板。上面是当前项目专有动作（导航、抓放等），下面是通用控制（延时、条件、并行）。",
          "拖到中间画布，或单击直接放下。颜色按分类区分，和画布上的块一致。"
        ],
        target: "#palette",
        before: function (done) {
          expandPanelForTour("#palette", "restore-palette");
          afterLayout(done);
        }
      },
      {
        phase: 1,
        title: "点一项就能放下",
        body: [
          "例如先放一个导航或延时。每个块左边是入口，右边是出口；条件块会有 true / false 两个出口。",
          "放错了：点中块后按 Delete 删除。起点块标题里会带 ★。"
        ],
        targetFn: firstPaletteItem,
        before: function (done) {
          expandPanelForTour("#palette", "restore-palette");
          afterLayout(done);
        }
      },
      {
        phase: 1,
        title: "在画布上连成一条流程",
        body: [
          "拖块改位置。从右侧小圆点按住，拉到下一块左侧小圆点，松开即连线。",
          "点连线可选中，再按 Delete 断开。空白处拖动画布，Ctrl（⌘）+ 滚轮缩放，或用工具栏的 − ＋ 适应。"
        ],
        target: "#canvas-wrapper"
      },
      {
        phase: 1,
        title: "在右侧改这个块的参数",
        body: [
          "点画布上的块，右侧会出现参数表（目标点位、延时秒数等）。改完立刻写进内存。",
          "要持久化必须再点保存。相关连线也可以在这里改分支或删掉。"
        ],
        target: "#inspector",
        before: function (done) {
          expandPanelForTour("#inspector", "restore-inspector");
          afterLayout(done);
        }
      },
      {
        phase: 1,
        title: "保存，并先做结构校验",
        body: [
          "「💾 保存」把图写到服务器。黄色表示有未保存修改，绿色表示已同步。",
          "「✔ 校验」只检查起点、连线、必填参数，不驱动机器人。通过后再演练。"
        ],
        targets: ["#btn-save", "#btn-validate"]
      },
      {
        phase: 1,
        title: "输出流程，方便软件更新后迁回",
        body: [
          "「⬇ 输出」把当前图或本项目全部流程下载到本机。换镜像 / 升级软件会盖掉容器里的流程图，更新前请先输出。",
          "更新完成后仍点这个按钮，选「导入」即可还原。单份 JSON 和全部流程迁移包都能认。"
        ],
        target: "#btn-export"
      },
      {
        phase: 2,
        title: "演练：用 mock 空跑一遍",
        body: [
          "「▶ 演练」用 mock 机器人按当前画布走一遍，不会碰生产机器人，也不走「启动 / START_WORKING」。",
          "后端会按需拉起 mock；再点一次按钮即可取消。画布上的块会按执行顺序变色。"
        ],
        target: "#btn-dryrun"
      },
      {
        phase: 2,
        title: "看每一步有没有跑通",
        body: [
          "底部是执行日志和演练轨迹：保存结果、校验错误、每个节点的开始/结束都会打在这里。",
          "演练失败时先看这一栏，再回头改块的参数或连线。"
        ],
        target: "#log-panel"
      },
      {
        phase: 2,
        title: "导航点位在这里改",
        body: [
          "「📍 点位」编辑当前项目 robot_config 里的导航坐标。导航类功能块的下拉选项来自这份列表。",
          "改完点位后记得保存；属性面板里的目标点位下拉会自动刷新。"
        ],
        target: "#btn-poses"
      },
      {
        phase: 3,
        title: "运行控制：交给主程序",
        body: [
          "「🤖 运行控制」作用的是正在跑的 main.py，不是顶部的演练。用来启动系统、模拟下发业务命令。",
          "下一步会打开面板，并标出新手该开的 Mock 开关——结合教程请用 mock 代替真机。"
        ],
        target: "#btn-run-panel"
      },
      {
        phase: 3,
        title: "新手请打开 Mock",
        body: [
          "这个开关会启动本机 mock_rosbridge（9090/9091/9092），让主程序连模拟机器人而不是现场真机。",
          "没开 Mock 时，「启动」会去连配置文件里的真实机器人。不熟悉现场前请保持 Mock 打开。"
        ],
        target: ".run-mock-switch",
        needRun: true,
        before: function (done) { ensureRunControlForTour(done); }
      },
      {
        phase: 3,
        title: "启动系统，再发业务命令",
        body: [
          "底部「启动」发 START_WORKING：初始化并进入可接收命令的状态。「重置」清调度记忆并休眠。",
          "开着 Mock 时，启动连的是模拟机器人。没开 Mock 才会去连配置里的真机。"
        ],
        targets: [".run-dock"],
        needRun: true,
        before: function (done) { ensureRunControlForTour(done); }
      },
      {
        phase: 3,
        title: "用面板模拟下发命令",
        body: [
          "「模拟 HTTP 发送」等同 WCS / curl 打到命令端口，用来触发业务流程（如开始作业、取箱）。",
          "发的是真实调度指令。结合教程时请先打开 Mock，这样命令会打到模拟机器人而不是现场设备。"
        ],
        targets: [".send-panel"],
        needRun: true,
        before: function (done) { ensureRunControlForTour(done); }
      },
      {
        phase: 3,
        title: "主程序终端输出",
        body: [
          "「🖥 主程序输出」另开一页，显示 main.py 打到终端的详细日志（导航反馈、动作记录等）。",
          "需要 main.py 已在跑。只开了本编辑器、没起主程序时，页面会提示还没有日志。"
        ],
        target: "#btn-console"
      },
      {
        phase: 3,
        title: "可以上手了",
        body: [
          "建议路径：选项目 → 新建或打开流程 → 拖块并连线 → 保存 / 校验 → ▶ 演练（自动 mock）。",
          "确认图没问题后，打开运行控制，新手打开 Mock，再启动系统、发送命令。随时可再点「使用说明」。"
        ]
      }
    ];
  }

  function firstPaletteItem() {
    return document.querySelector(".palette-item");
  }

  function expandPanelForTour(sel, restoreId) {
    var panel = $(sel);
    if (panel && panel.classList.contains("collapsed")) {
      var btn = document.getElementById(restoreId);
      if (btn) btn.click();
    }
  }

  function ensureRunControlForTour(done) {
    if (document.querySelector(".run-mock-switch")) {
      afterLayout(done);
      return;
    }
    openRunControl();
    var n = 0;
    (function wait() {
      n += 1;
      if (document.querySelector(".run-mock-switch") || n > 50) {
        afterLayout(done);
        return;
      }
      setTimeout(wait, 40);
    })();
  }

  function closeTourRunControl() {
    if (runControlCloser) runControlCloser();
  }

  function dismissEditorModals() {
    if (runControlCloser) runControlCloser();
    $all(".modal-mask").forEach(function (m) {
      if (m.parentNode) m.parentNode.removeChild(m);
    });
  }

  function afterLayout(fn) {
    requestAnimationFrame(function () {
      requestAnimationFrame(fn);
    });
  }

  function markGuideDone() {
    try { window.localStorage.setItem(GUIDE_STORAGE_KEY, "1"); } catch (e) {}
  }

  function maybeAutoStartTour() {
    if (tourAutoTried) return;
    tourAutoTried = true;
    try {
      if (window.localStorage.getItem(GUIDE_STORAGE_KEY) === "1") return;
    } catch (e) {}
    setTimeout(function () {
      if (!tour) startGuide(true);
    }, 700);
  }

  function startGuide(fromAuto) {
    if (tour) stopGuide(false);
    dismissEditorModals();
    var steps = guideSteps();
    var root = document.createElement("div");
    root.id = "tour-root";

    var hit = document.createElement("div");
    hit.className = "tour-hit";
    var spot = document.createElement("div");
    spot.className = "tour-spotlight tour-full";
    var wire = document.createElement("div");
    wire.className = "tour-wire";
    wire.style.display = "none";
    var corners = ["tl", "tr", "bl", "br"].map(function (pos) {
      var c = document.createElement("div");
      c.className = "tour-corner " + pos;
      c.style.display = "none";
      return c;
    });
    var card = document.createElement("div");
    card.className = "tour-card";

    root.appendChild(hit);
    root.appendChild(spot);
    root.appendChild(wire);
    corners.forEach(function (c) { root.appendChild(c); });
    root.appendChild(card);
    document.body.appendChild(root);
    var guideBtn = $("#btn-guide");
    if (guideBtn) guideBtn.classList.add("tour-on");

    tour = { root: root, step: 0, steps: steps, spot: spot, wire: wire, corners: corners, card: card };

    function onKey(e) {
      if (!tour) return;
      if (e.key === "Escape") {
        e.preventDefault(); e.stopPropagation(); stopGuide(true);
      } else if (e.key === "ArrowRight" || e.key === "Enter") {
        e.preventDefault(); e.stopPropagation(); tourNext();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault(); e.stopPropagation(); tourPrev();
      }
    }
    function onReflow() {
      if (tour) layoutTourStep(tour.steps[tour.step], true);
    }
    tour.onKey = onKey;
    tour.onReflow = onReflow;
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onReflow);
    window.addEventListener("scroll", onReflow, true);

    showTourStep(0);
    if (!fromAuto) markGuideDone();
  }

  function stopGuide(remember) {
    if (!tour) return;
    closeTourRunControl();
    document.removeEventListener("keydown", tour.onKey, true);
    window.removeEventListener("resize", tour.onReflow);
    window.removeEventListener("scroll", tour.onReflow, true);
    if (tour.root && tour.root.parentNode) tour.root.parentNode.removeChild(tour.root);
    var guideBtn = $("#btn-guide");
    if (guideBtn) guideBtn.classList.remove("tour-on");
    tour = null;
    if (remember) markGuideDone();
  }

  function tourPrev() {
    if (!tour || tour.step <= 0) return;
    showTourStep(tour.step - 1);
  }

  function tourNext() {
    if (!tour) return;
    if (tour.step >= tour.steps.length - 1) {
      stopGuide(true);
      return;
    }
    showTourStep(tour.step + 1);
  }

  function showTourStep(idx) {
    if (!tour) return;
    tour.step = idx;
    var step = tour.steps[idx];
    if (!step.needRun) closeTourRunControl();
    renderTourCard(step, idx, tour.steps.length);
    var go = function () { layoutTourStep(step, false); };
    if (step.before) step.before(go);
    else afterLayout(go);
  }

  function renderTourCard(step, idx, total) {
    var card = tour.card;
    card.innerHTML = "";

    var kicker = document.createElement("div");
    kicker.className = "tour-kicker";
    var phases = document.createElement("div");
    phases.className = "tour-phases";
    ["建流程", "拖块", "演练", "实机"].forEach(function (name, i) {
      var s = document.createElement("span");
      s.textContent = name;
      var cur = typeof step.phase === "number" ? step.phase : 0;
      if (i < cur) s.className = "done";
      else if (i === cur) s.className = "now";
      phases.appendChild(s);
    });
    var count = document.createElement("span");
    count.textContent = (idx + 1) + " / " + total;
    kicker.appendChild(phases);
    kicker.appendChild(count);
    card.appendChild(kicker);

    var h = document.createElement("h4");
    h.textContent = step.title;
    card.appendChild(h);

    if (step.welcome) {
      var mini = document.createElement("div");
      mini.className = "tour-steps-mini";
      [
        ["1. 建流程", "选项目、新建或打开、激活"],
        ["2. 拖功能块", "从左侧拖到画布并连线"],
        ["3. 演练", "▶ 演练自动走 mock"],
        ["4. 运行控制", "新手打开 Mock 再启动"]
      ].forEach(function (row) {
        var chip = document.createElement("div");
        chip.className = "tour-chip";
        var b = document.createElement("b");
        b.textContent = row[0];
        chip.appendChild(b);
        chip.appendChild(document.createTextNode(row[1]));
        mini.appendChild(chip);
      });
      card.appendChild(mini);
    }

    var body = document.createElement("div");
    body.className = "tour-body";
    (step.body || []).forEach(function (text) {
      var p = document.createElement("p");
      p.textContent = text;
      body.appendChild(p);
    });
    card.appendChild(body);

    var nav = document.createElement("div");
    nav.className = "tour-nav";
    var skip = document.createElement("button");
    skip.className = "tour-skip";
    skip.textContent = "跳过";
    skip.title = "关闭说明（Esc）";
    skip.addEventListener("click", function () { stopGuide(true); });
    nav.appendChild(skip);

    var prev = document.createElement("button");
    prev.textContent = "上一步";
    prev.disabled = idx === 0;
    prev.addEventListener("click", tourPrev);
    nav.appendChild(prev);

    var next = document.createElement("button");
    next.className = "primary";
    next.textContent = idx >= total - 1 ? "完成" : "下一步";
    next.addEventListener("click", tourNext);
    nav.appendChild(next);
    card.appendChild(nav);
  }

  function collectTourEls(step) {
    var els = [];
    if (step.targetFn) {
      var one = step.targetFn();
      if (one) els.push(one);
    }
    var sels = step.targets || (step.target ? [step.target] : []);
    sels.forEach(function (sel) {
      var el = document.querySelector(sel);
      if (el) els.push(el);
    });
    return els;
  }

  function unionRect(els) {
    var r = null;
    els.forEach(function (el) {
      var b = el.getBoundingClientRect();
      if (!b.width && !b.height) return;
      if (!r) {
        r = { top: b.top, left: b.left, right: b.right, bottom: b.bottom };
      } else {
        r.top = Math.min(r.top, b.top);
        r.left = Math.min(r.left, b.left);
        r.right = Math.max(r.right, b.right);
        r.bottom = Math.max(r.bottom, b.bottom);
      }
    });
    if (!r) return null;
    return { top: r.top, left: r.left, width: r.right - r.left, height: r.bottom - r.top,
             bottom: r.bottom, right: r.right };
  }

  function layoutTourStep(step, fromReflow) {
    if (!tour) return;
    var els = collectTourEls(step);
    if (els.length && !fromReflow) {
      try { els[0].scrollIntoView({ block: "nearest", inline: "nearest" }); } catch (e) {}
    }
    var hole = unionRect(els);
    var pad = 8;
    if (hole) {
      hole.top -= pad; hole.left -= pad;
      hole.width += pad * 2; hole.height += pad * 2;
      hole.bottom = hole.top + hole.height;
      hole.right = hole.left + hole.width;
    }
    applySpotlight(hole);
    placeTourCard(tour.card, hole);
  }

  function applySpotlight(hole) {
    var spot = tour.spot, wire = tour.wire;
    if (!hole) {
      spot.className = "tour-spotlight tour-full";
      spot.style.top = "0px";
      spot.style.left = "0px";
      spot.style.width = "0px";
      spot.style.height = "0px";
      wire.style.display = "none";
      tour.corners.forEach(function (c) { c.style.display = "none"; });
      return;
    }
    spot.className = "tour-spotlight";
    spot.style.top = hole.top + "px";
    spot.style.left = hole.left + "px";
    spot.style.width = Math.max(hole.width, 8) + "px";
    spot.style.height = Math.max(hole.height, 8) + "px";
    wire.style.display = "block";
    wire.style.top = hole.top + "px";
    wire.style.left = hole.left + "px";
    wire.style.width = Math.max(hole.width, 8) + "px";
    wire.style.height = Math.max(hole.height, 8) + "px";
    var inset = 1;
    var size = 13;
    var map = {
      tl: [hole.top - inset, hole.left - inset],
      tr: [hole.top - inset, hole.right - size + inset],
      bl: [hole.bottom - size + inset, hole.left - inset],
      br: [hole.bottom - size + inset, hole.right - size + inset]
    };
    tour.corners.forEach(function (c) {
      var pos = c.className.replace("tour-corner ", "");
      var xy = map[pos];
      if (!xy) return;
      c.style.display = "block";
      c.style.top = xy[0] + "px";
      c.style.left = xy[1] + "px";
    });
  }

  function placeTourCard(card, hole) {
    var vw = window.innerWidth, vh = window.innerHeight;
    var cw = card.offsetWidth || 420;
    var ch = card.offsetHeight || 220;
    var gap = 14, margin = 10;
    var x, y;
    if (!hole) {
      x = Math.max(margin, (vw - cw) / 2);
      y = Math.max(margin, (vh - ch) / 2);
      card.style.left = x + "px";
      card.style.top = y + "px";
      return;
    }
    var space = {
      bottom: vh - hole.bottom - gap,
      top: hole.top - gap,
      right: vw - hole.right - gap,
      left: hole.left - gap
    };
    var side = "bottom";
    if (space.bottom >= ch + margin) side = "bottom";
    else if (space.top >= ch + margin) side = "top";
    else if (space.right >= cw + margin) side = "right";
    else if (space.left >= cw + margin) side = "left";
    else {
      var best = "bottom", bestVal = space.bottom;
      ["top", "right", "left"].forEach(function (k) {
        if (space[k] > bestVal) { bestVal = space[k]; best = k; }
      });
      side = best;
    }
    if (side === "bottom") {
      x = hole.left;
      y = hole.bottom + gap;
    } else if (side === "top") {
      x = hole.left;
      y = hole.top - ch - gap;
    } else if (side === "right") {
      x = hole.right + gap;
      y = hole.top;
    } else {
      x = hole.left - cw - gap;
      y = hole.top;
    }
    x = Math.max(margin, Math.min(x, vw - cw - margin));
    y = Math.max(margin, Math.min(y, vh - ch - margin));
    card.style.left = x + "px";
    card.style.top = y + "px";
  }

  // 直接关标签页/刷新时浏览器自带的"离开此网站？"确认（自定义文案已被现代浏览器忽略，
  // 只要 preventDefault 就会弹出浏览器标准提示）
  window.addEventListener("beforeunload", function (e) {
    if (!state.dirty) return;
    e.preventDefault();
    e.returnValue = "";
  });

  document.addEventListener("DOMContentLoaded", init);
})();
