/* 吧味学习机 · 插件可视化视图
 *
 * 通过 AstrBot 插件视图 bridge（window.AstrBotPluginView）与后端 Web API 通信：
 * bridge.apiGet(endpoint, params)  /  bridge.apiPost(endpoint, body)
 * endpoint 不带插件名前缀（后端注册时为 /astrbot_plugin_tieba_tone/<endpoint>）。
 */
(function () {
    "use strict";

    var TABS = [
        { id: "overview", label: "概览" },
        { id: "account", label: "百度账号" },
        { id: "config", label: "设置" },
        { id: "corpus", label: "语料库" },
        { id: "logs", label: "运行日志" },
    ];

    var state = {
        tab: "overview",
        status: null,
        corpus: null,
        config: null,
        providers: null,
        qr: { sid: "", data: "", url: "", state: "idle", message: "" },
        pollTimer: null,
        autoRefresh: true,
        refreshTimer: null,
        logoutArmed: false,
        resetArmed: false,
    };

    var appEl = null;

    /* ------------------------------------------------------------------ utils */
    function h(tag, attrs, children) {
        var el = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (k) {
                if (k === "class") el.className = attrs[k];
                else if (k.startsWith("on")) el.addEventListener(k.slice(2), attrs[k]);
                else if (k === "disabled" || k === "checked" || k === "selected") {
                    // 布尔属性：只有真值才设置，避免 setAttribute("disabled","false") 依然禁用
                    if (attrs[k]) el.setAttribute(k, "");
                } else if (attrs[k] != null) el.setAttribute(k, attrs[k]);
            });
        }
        if (children != null) {
            // 展开嵌套数组（条件渲染可能返回数组或 null），跳过 null
            var flat = [];
            (function walk(items) {
                items.forEach(function (c) {
                    if (Array.isArray(c)) walk(c);
                    else if (c != null) flat.push(c);
                });
            })(Array.isArray(children) ? children : [children]);
            flat.forEach(function (c) {
                el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
            });
        }
        return el;
    }

    function esc(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
    }

    function fmtTime(ts) {
        if (!ts) return "";
        try {
            var d = new Date(ts * 1000);
            return (
                d.getFullYear() +
                "-" +
                String(d.getMonth() + 1).padStart(2, "0") +
                "-" +
                String(d.getDate()).padStart(2, "0") +
                " " +
                String(d.getHours()).padStart(2, "0") +
                ":" +
                String(d.getMinutes()).padStart(2, "0") +
                ":" +
                String(d.getSeconds()).padStart(2, "0")
            );
        } catch (e) {
            return "";
        }
    }

    /* ------------------------------------------------------------------ bridge */
    function waitBridge(timeoutMs) {
        return new Promise(function (resolve) {
            var t0 = Date.now();
            var iv = setInterval(function () {
                if (window.AstrBotPluginView) {
                    clearInterval(iv);
                    resolve(window.AstrBotPluginView);
                } else if (Date.now() - t0 > (timeoutMs || 8000)) {
                    clearInterval(iv);
                    resolve(null);
                }
            }, 100);
        });
    }

    var bridge = null;

    function unwrap(resp) {
        // 兼容不同 bridge 版本的返回包装
        if (resp && typeof resp === "object" && !Array.isArray(resp)) {
            if (resp.status === "ok" && resp.data !== undefined) return resp.data;
            if (resp.code === 0 && resp.data !== undefined) return resp.data;
            if (resp.success === false && resp.message) throw new Error(resp.message);
        }
        return resp;
    }

    function apiGet(endpoint, params) {
        if (!bridge || !bridge.apiGet) throw new Error("bridge 不可用");
        return bridge.apiGet(endpoint, params).then(unwrap);
    }

    function apiPost(endpoint, body) {
        if (!bridge || !bridge.apiPost) throw new Error("bridge 不可用");
        return bridge.apiPost(endpoint, body || {}).then(unwrap);
    }

    /* ------------------------------------------------------------------ 页内提示
       插件视图运行在 sandbox iframe 中（无 allow-modals），
       原生 alert/confirm 会被静默拦截，必须使用页内交互。 */
    function showToast(message, kind) {
        var old = document.getElementById("ts-toast");
        if (old && old.parentNode) old.remove();
        var t = h(
            "div",
            {
                id: "ts-toast",
                class: "ts-toast " + (kind === "ok" ? "ok" : kind === "err" ? "err" : ""),
            },
            String(message == null ? "" : message)
        );
        document.body.appendChild(t);
        setTimeout(function () {
            if (t.parentNode) t.remove();
        }, 3500);
    }

    /* ------------------------------------------------------------------ render */
    function render() {
        if (!appEl) return;
        appEl.innerHTML = "";
        var st = state.status;

        // header
        var badge = h(
            "span",
            { class: "ts-badge" + (st ? (st.running ? " run" : st.has_corpus ? " ok" : "") : "") },
            [
                h("span", { class: "dot" }),
                st ? (st.running ? "正在学习" : st.has_corpus ? "已就绪" : "未学习") : "加载中",
            ]
        );
        appEl.appendChild(
            h("div", { class: "ts-header" }, [
                h("div", null, [
                    h("h1", null, "吧味学习机"),
                    h("div", { class: "ts-sub" }, "刷指定贴吧的帖子与评论，学习吧友的说话风格"),
                ]),
                badge,
            ])
        );

        // tabs
        var tabs = h("div", { class: "ts-tabs" });
        TABS.forEach(function (t) {
            tabs.appendChild(
                h(
                    "button",
                    {
                        class: "ts-tab" + (state.tab === t.id ? " active" : ""),
                        onclick: function () {
                            state.tab = t.id;
                            render();
                            refreshTab();
                        },
                    },
                    t.label
                )
            );
        });
        appEl.appendChild(tabs);

        if (!st) {
            appEl.appendChild(h("div", { class: "ts-empty" }, "正在加载状态…"));
            return;
        }

        if (state.tab === "overview") renderOverview(st);
        else if (state.tab === "account") renderAccount(st);
        else if (state.tab === "config") renderConfig();
        else if (state.tab === "corpus") renderCorpus();
        else if (state.tab === "logs") renderLogs();
    }

    function renderOverview(st) {
        var grid = h("div", { class: "ts-grid" });

        // 账号卡片
        grid.appendChild(
            h("div", { class: "ts-card" }, [
                h("h3", null, "百度账号"),
                h(
                    "div",
                    { class: "ts-kv" },
                    [h("span", { class: "k" }, "状态"), h("span", { class: "v" },
                        st.account.logged_in
                            ? "已登录 · " + (st.account.username || "百度用户")
                            : "未登录")]
                ),
                st.account.logged_in
                    ? h(
                          "div",
                          { class: "ts-kv" },
                          [
                              h("span", { class: "k" }, "BDUSS"),
                              h("span", { class: "v" }, st.account.bduss || "****"),
                          ]
                      )
                    : null,
                h(
                    "div",
                    { class: "ts-mt16" },
                    h(
                        "button",
                        {
                            class: "ts-btn",
                            onclick: function () {
                                state.tab = "account";
                                render();
                            },
                        },
                        "前往登录"
                    )
                ),
            ])
        );

        // 学习配置卡片
        var cfg = st.config || {};
        grid.appendChild(
            h("div", { class: "ts-card" }, [
                h("h3", null, "学习配置"),
                h("div", { class: "ts-kv" }, [
                    h("span", { class: "k" }, "目标贴吧"),
                    h("span", { class: "v" }, (cfg.forums || []).join("、") || "未配置"),
                ]),
                h("div", { class: "ts-kv" }, [
                    h("span", { class: "k" }, "定时学习"),
                    h(
                        "span",
                        { class: "v" },
                        cfg.enabled
                            ? (cfg.schedule_mode === "hourly" ? "每小时" : "每天 " + cfg.daily_hour + ":00")
                            : "未启用"
                    ),
                ]),
                h("div", { class: "ts-kv" }, [
                    h("span", { class: "k" }, "每次刷帖"),
                    h("span", { class: "v" }, cfg.posts_per_crawl + " 帖 × " + cfg.comments_per_post + " 楼"),
                ]),
                h("div", { class: "ts-kv" }, [
                    h("span", { class: "k" }, "自动应用"),
                    h("span", { class: "v" }, cfg.auto_apply ? "开" : "关"),
                ]),
                h("div", { class: "ts-mt16 ts-hint" }, "在 AstrBot 插件配置页可修改全部参数。"),
            ])
        );

        // 统计卡片
        var s = st.stats || {};
        grid.appendChild(
            h("div", { class: "ts-card" }, [
                h("h3", null, "学习成果"),
                h("div", { class: "ts-stat-row" }, [
                    h("div", { class: "ts-stat" }, [
                        h("div", { class: "num" }, String(s.total_threads || 0)),
                        h("div", { class: "lbl" }, "累计帖子"),
                    ]),
                    h("div", { class: "ts-stat" }, [
                        h("div", { class: "num" }, String(s.total_comments || 0)),
                        h("div", { class: "lbl" }, "累计楼层"),
                    ]),
                    h("div", { class: "ts-stat" }, [
                        h("div", { class: "num" }, String(s.learn_count || 0)),
                        h("div", { class: "lbl" }, "风格更新"),
                    ]),
                ]),
                h("div", { class: "ts-mt16" }, [
                    h("div", { class: "ts-kv" }, [
                        h("span", { class: "k" }, "上次执行"),
                        h("span", { class: "v" }, st.last_run_str || "从未执行"),
                    ]),
                    h("div", { class: "ts-kv" }, [
                        h("span", { class: "k" }, "下次执行"),
                        h("span", { class: "v" }, st.next_run_hint || "-"),
                    ]),
                    h("div", { class: "ts-kv" }, [
                        h("span", { class: "k" }, "语录数"),
                        h("span", { class: "v" }, String(st.quote_count || 0)),
                    ]),
                ]),
            ])
        );

        // 操作卡片
        grid.appendChild(
            h("div", { class: "ts-card full" }, [
                h("h3", null, "操作"),
                h(
                    "button",
                    {
                        class: "ts-btn primary",
                        disabled: st.running,
                        onclick: function () {
                            runLearn();
                        },
                    },
                    st.running ? h("span", null, [h("span", { class: "ts-spinner" }), "学习中…"]) : "立即刷帖学习"
                ),
                h(
                    "button",
                    {
                        class: "ts-btn",
                        onclick: function () {
                            state.tab = "corpus";
                            render();
                            refreshTab();
                        },
                    },
                    "查看语料"
                ),
                st.last_error
                    ? h("div", { class: "ts-mt8 ts-hint", style: "color: var(--ts-err)" }, "上次错误：" + esc(st.last_error))
                    : null,
            ])
        );

        appEl.appendChild(grid);
    }

    function renderAccount(st) {
        var grid = h("div", { class: "ts-grid" });
        var acc = st.account || {};

        // 扫码登录卡片
        var qrBox = h("div", { class: "ts-qr" });
        if (state.qr.data) {
            qrBox.appendChild(h("img", { src: state.qr.data, alt: "登录二维码" }));
        } else if (state.qr.url) {
            qrBox.appendChild(h("img", { src: state.qr.url, alt: "登录二维码" }));
        } else {
            qrBox.appendChild(
                h("div", { class: "ts-empty" }, "点击下方按钮获取百度登录二维码")
            );
        }
        var qrStatus = h(
            "div",
            {
                class:
                    "ts-qr-status " +
                    (state.qr.state === "success"
                        ? "ok"
                        : state.qr.state === "error"
                        ? "err"
                        : state.qr.state === "scanned"
                        ? "warn"
                        : ""),
            },
            state.qr.message || ""
        );
        grid.appendChild(
            h("div", { class: "ts-card" }, [
                h("h3", null, "扫码登录（推荐）"),
                qrBox,
                qrStatus,
                h("div", { class: "ts-mt8 ts-hint" }, [
                    "打开百度 App 或手机扫码，确认后插件会自动获取登录信息（BDUSS），全程不保存密码。",
                ]),
                h("div", { class: "ts-mt8" }, [
                    h(
                        "button",
                        {
                            class: "ts-btn primary",
                            disabled: state.qr.state === "polling",
                            onclick: startQrLogin,
                        },
                        state.qr.state === "polling"
                            ? h("span", null, [h("span", { class: "ts-spinner" }), "等待扫码…"])
                            : "获取登录二维码"
                    ),
                    h(
                        "button",
                        {
                            class: "ts-btn",
                            onclick: function () {
                                stopQrPoll();
                                state.qr = { sid: "", data: "", url: "", state: "idle", message: "" };
                                render();
                            },
                        },
                        "取消"
                    ),
                ]),
            ])
        );

        // 手动 BDUSS 登录卡片
        var input = h("input", {
            class: "ts-input",
            placeholder: "粘贴 BDUSS（从浏览器 Cookie 中复制）",
            id: "ts-bduss-input",
        });
        grid.appendChild(
            h("div", { class: "ts-card" }, [
                h("h3", null, "手动 BDUSS 登录"),
                input,
                h("div", { class: "ts-mt8" }, [
                    h(
                        "button",
                        {
                            class: "ts-btn primary",
                            onclick: function () {
                                var v = (document.getElementById("ts-bduss-input") || {}).value || "";
                                manualBduss(v.trim());
                            },
                        },
                        "保存并登录"
                    ),
                ]),
                h("div", { class: "ts-mt8 ts-hint" }, [
                    "在浏览器登录 tieba.baidu.com 后，按 F12 打开开发者工具 → Application/存储 → Cookies，",
                    h("br"),
                    "找到 BDUSS 字段复制其值填入即可（与 TiebaLite 的「网页登录后自动取 Cookie」是同一思路）。",
                ]),
            ])
        );

        // 账号信息卡片
        grid.appendChild(
            h("div", { class: "ts-card" }, [
                h("h3", null, "当前账号"),
                h("div", { class: "ts-kv" }, [
                    h("span", { class: "k" }, "状态"),
                    h(
                        "span",
                        { class: "v" },
                        acc.logged_in ? "已登录" + (acc.source === "qrcode" ? "（扫码）" : "（手动）") : "未登录"
                    ),
                ]),
                acc.logged_in
                    ? [
                          h("div", { class: "ts-kv" }, [
                              h("span", { class: "k" }, "账号"),
                              h("span", { class: "v" }, esc(acc.username || "未知")),
                          ]),
                          h("div", { class: "ts-kv" }, [
                              h("span", { class: "k" }, "BDUSS"),
                              h("span", { class: "v" }, esc(acc.bduss || "")),
                          ]),
                          h("div", { class: "ts-mt16" }, [
                              h(
                                  "button",
                                  {
                                      class: "ts-btn danger" + (state.logoutArmed ? " armed" : ""),
                                      onclick: doLogout,
                                  },
                                  state.logoutArmed ? "确认退出？再次点击" : "退出登录"
                              ),
                          ]),
                      ]
                    : null,
            ])
        );

        appEl.appendChild(grid);
    }

    function renderCorpus() {
        var c = state.corpus;
        if (!c) {
            appEl.appendChild(h("div", { class: "ts-empty" }, "正在加载语料…"));
            return;
        }
        var grid = h("div", { class: "ts-grid" });

        grid.appendChild(
            h("div", { class: "ts-card full" }, [
                h("h3", null, "语言风格指南（LLM 提炼，会注入对话的系统提示词）"),
                c.profile
                    ? h("pre", { class: "ts-pre" }, esc(c.profile))
                    : h("div", { class: "ts-empty" }, "还没有学到风格，先去「概览」点击立即学习。"),
            ])
        );

        var quoteList = h("div", null);
        if (c.quotes && c.quotes.length) {
            c.quotes.forEach(function (q) {
                var t = typeof q === "string" ? q : q.text;
                var meta =
                    typeof q === "object" && q.forum
                        ? "—— " + q.forum + (q.author ? " · " + q.author : "")
                        : "";
                quoteList.appendChild(
                    h("div", { class: "ts-quote" }, [
                        esc(t),
                        meta ? h("div", { class: "meta" }, esc(meta)) : null,
                    ])
                );
            });
        } else {
            quoteList.appendChild(h("div", { class: "ts-empty" }, "暂无代表性语录"));
        }
        grid.appendChild(
            h("div", { class: "ts-card full" }, [
                h("h3", null, "代表性语录（" + (c.quotes ? c.quotes.length : 0) + " 条）"),
                quoteList,
            ])
        );

        grid.appendChild(
            h("div", { class: "ts-card full" }, [
                h("h3", null, "将注入系统提示词的内容预览"),
                h("pre", { class: "ts-pre" }, esc(c.preview || "（空）")),
                h("div", { class: "ts-mt8" }, [
                    h(
                        "button",
                        {
                            class: "ts-btn danger" + (state.resetArmed ? " armed" : ""),
                            onclick: resetCorpus,
                        },
                        state.resetArmed ? "确认清空？再次点击" : "清空语料"
                    ),
                ]),
            ])
        );

        appEl.appendChild(grid);
    }

    /* ------------------------------------------------------------------ 设置 */
    function formField(label, el, hint) {
        return h("div", { class: "ts-form-field" }, [
            h("label", { class: "ts-label" }, label),
            el,
            hint ? h("div", { class: "ts-hint" }, hint) : null,
        ]);
    }

    function sel(options, current, extraAttrs) {
        var el = h("select", extraAttrs || { class: "ts-input" });
        options.forEach(function (o) {
            el.appendChild(h("option", { value: o[0] }, o[1]));
        });
        el.value = current;
        return el;
    }

    function numInput(id, value, attrs) {
        var el = h(
            "input",
            Object.assign({ class: "ts-input", id: id, type: "number", value: value }, attrs || {})
        );
        return el;
    }

    function renderConfig() {
        if (!state.config) {
            appEl.appendChild(h("div", { class: "ts-empty" }, "正在加载配置…"));
            return;
        }
        var cfg = state.config;
        var grid = h("div", { class: "ts-grid" });

        // ---- 基本设置卡片
        var enabledSel = sel(
            [
                ["1", "启用"],
                ["0", "停用"],
            ],
            cfg.enabled ? "1" : "0",
            { class: "ts-input", id: "ts-cfg-enabled" }
        );
        var forumsInput = h("input", {
            class: "ts-input",
            id: "ts-cfg-forums",
            placeholder: "多个吧用英文逗号分隔，如：孙笑川吧,原神吧",
            value: (cfg.forums || []).join(","),
        });
        var scheduleSel = sel(
            [
                ["daily", "每天固定时间"],
                ["hourly", "每小时一次"],
            ],
            cfg.schedule_mode || "daily",
            { class: "ts-input", id: "ts-cfg-schedule_mode" }
        );
        var autoApplySel = sel(
            [
                ["1", "自动应用"],
                ["0", "不应用"],
            ],
            cfg.auto_apply ? "1" : "0",
            { class: "ts-input", id: "ts-cfg-auto_apply" }
        );
        grid.appendChild(
            h("div", { class: "ts-card" }, [
                h("h3", null, "基本设置"),
                formField("定时学习", enabledSel, "启用后按下面的频率自动刷帖学习"),
                formField("目标贴吧", forumsInput, "插件只读不发言；多个吧用英文逗号分隔"),
                formField("调度模式", scheduleSel, "「每天」时在下方指定时间执行"),
                formField(
                    "每天执行时间（0-23 点）",
                    numInput("ts-cfg-daily_hour", cfg.daily_hour, { min: 0, max: 23 }),
                    "仅在调度模式为「每天」时生效"
                ),
                formField(
                    "自动应用到对话",
                    autoApplySel,
                    "开启后，学到的语言风格会注入后续所有 AI 对话的系统提示词"
                ),
            ])
        );

        // ---- 抓取与学习卡片
        grid.appendChild(
            h("div", { class: "ts-card" }, [
                h("h3", null, "抓取与学习"),
                formField(
                    "每次抓取帖子数（1-100）",
                    numInput("ts-cfg-posts_per_crawl", cfg.posts_per_crawl, { min: 1, max: 100 })
                ),
                formField(
                    "每帖抓取评论数（0-200）",
                    numInput("ts-cfg-comments_per_post", cfg.comments_per_post, {
                        min: 0,
                        max: 200,
                    })
                ),
                formField(
                    "注入语录条数（1-200）",
                    numInput("ts-cfg-max_quotes", cfg.max_quotes, { min: 1, max: 200 }),
                    "每次对话注入系统提示词的代表性语录上限"
                ),
                formField(
                    "最短评论长度（0-100 字）",
                    numInput("ts-cfg-min_comment_length", cfg.min_comment_length, {
                        min: 0,
                        max: 100,
                    }),
                    "过短的评论会被过滤，不作为学习样本"
                ),
                formField(
                    "最长评论长度（10-5000 字）",
                    numInput("ts-cfg-max_comment_length", cfg.max_comment_length, {
                        min: 10,
                        max: 5000,
                    })
                ),
                formField(
                    "请求间隔（0.5-60 秒）",
                    numInput("ts-cfg-request_interval", cfg.request_interval, {
                        min: 0.5,
                        max: 60,
                        step: 0.5,
                    }),
                    "抓取请求之间的间隔，避免过快被百度限流"
                ),
            ])
        );

        // ---- 模型与身份卡片
        var provLoading = state.providers === null;
        var provSel = sel(
            [["", "使用当前默认对话模型"]],
            cfg.provider_id || "",
            { class: "ts-input", id: "ts-cfg-provider_id" }
        );
        (state.providers || []).forEach(function (p) {
            provSel.appendChild(h("option", { value: p.id || "" }, p.label || p.id || ""));
        });
        provSel.value = cfg.provider_id || "";
        if (provLoading) provSel.disabled = true;
        var uaInput = h("input", {
            class: "ts-input",
            id: "ts-cfg-user_agent",
            placeholder: "留空使用内置默认 UA",
            value: cfg.user_agent || "",
        });
        grid.appendChild(
            h("div", { class: "ts-card" }, [
                h("h3", null, "模型与身份"),
                formField(
                    "学习用模型",
                    provSel,
                    provLoading
                        ? "正在读取 AstrBot 已配置的模型…"
                        : "即 AstrBot「提供商 - 对话」里的模型；选择后用指定模型学习，留空用默认对话模型"
                ),
                formField("User-Agent", uaInput, "抓取贴吧时使用的 UA，一般无需修改"),
            ])
        );

        appEl.appendChild(grid);
        appEl.appendChild(
            h("div", { class: "ts-mt16" }, [
                h(
                    "button",
                    { class: "ts-btn primary", onclick: saveConfig },
                    "保存配置"
                ),
                h(
                    "span",
                    { class: "ts-hint ts-ml12" },
                    "保存后立即生效并写入插件配置文件"
                ),
            ])
        );
    }

    function renderLogs() {
        var box = h("div", { class: "ts-card full" });
        box.appendChild(
            h("div", { class: "ts-row" }, [
                h(
                    "button",
                    {
                        class: "ts-btn",
                        onclick: function () {
                            state.autoRefresh = !state.autoRefresh;
                            renderLogs();
                        },
                    },
                    state.autoRefresh ? "暂停自动刷新" : "开启自动刷新"
                ),
                h(
                    "button",
                    { class: "ts-btn", onclick: refreshLogs },
                    "刷新"
                ),
            ])
        );
        var list = h("div", { id: "ts-log-list" });
        box.appendChild(list);
        appEl.appendChild(box);
        refreshLogs();
    }

    function renderLogList(logs) {
        var list = document.getElementById("ts-log-list");
        if (!list) return;
        list.innerHTML = "";
        if (!logs || !logs.length) {
            list.appendChild(h("div", { class: "ts-empty" }, "暂无日志"));
            return;
        }
        logs.forEach(function (lg) {
            list.appendChild(
                h("div", { class: "ts-log " + (lg.level || "info") }, [
                    h("span", { class: "t" }, fmtTime(lg.ts)),
                    h("span", { class: "lv" }, (lg.level || "info").toUpperCase()),
                    h("span", null, esc(lg.msg)),
                ])
            );
        });
    }

    /* ------------------------------------------------------------------ actions */
    function refreshStatus() {
        return apiGet("status")
            .then(function (st) {
                state.status = st;
                render();
            })
            .catch(function (e) {
                if (!state.status && appEl) {
                    appEl.innerHTML = "";
                    appEl.appendChild(
                        h(
                            "div",
                            { class: "ts-empty" },
                            "状态加载失败：" + (e && e.message ? e.message : e) + "，请稍后重试"
                        )
                    );
                }
            });
    }

    function refreshCorpus() {
        return apiGet("corpus")
            .then(function (c) {
                state.corpus = c;
                if (state.tab === "corpus") render();
            })
            .catch(function () {});
    }

    function refreshLogs() {
        apiGet("logs", { limit: 60 })
            .then(function (r) {
                renderLogList(r && r.logs ? r.logs : []);
            })
            .catch(function () {});
    }

    function refreshConfig() {
        Promise.all([apiGet("config"), apiGet("models")])
            .then(function (res) {
                state.config = res[0];
                state.providers = (res[1] && res[1].providers) || [];
                if (state.tab === "config") render();
            })
            .catch(function (e) {
                if (state.tab === "config") {
                    appEl.appendChild(
                        h(
                            "div",
                            { class: "ts-empty" },
                            "配置加载失败：" + (e && e.message ? e.message : e) + "，请稍后重试"
                        )
                    );
                }
            });
    }

    function saveConfig() {
        function val(id) {
            var el = document.getElementById(id);
            return el ? el.value : "";
        }
        function num(id, dflt) {
            var v = parseFloat(val(id));
            return isNaN(v) ? dflt : v;
        }
        var payload = {
            enabled: val("ts-cfg-enabled") === "1",
            forums: String(val("ts-cfg-forums") || "")
                .split(/[,，]/)
                .map(function (s) {
                    return s.trim();
                })
                .filter(Boolean),
            schedule_mode: val("ts-cfg-schedule_mode") || "daily",
            daily_hour: num("ts-cfg-daily_hour", 3),
            posts_per_crawl: num("ts-cfg-posts_per_crawl", 10),
            comments_per_post: num("ts-cfg-comments_per_post", 15),
            auto_apply: val("ts-cfg-auto_apply") === "1",
            max_quotes: num("ts-cfg-max_quotes", 30),
            min_comment_length: num("ts-cfg-min_comment_length", 4),
            max_comment_length: num("ts-cfg-max_comment_length", 300),
            request_interval: num("ts-cfg-request_interval", 2),
            provider_id: val("ts-cfg-provider_id"),
            user_agent: val("ts-cfg-user_agent"),
        };
        apiPost("config", payload)
            .then(function (r) {
                state.config = (r && r.config) || payload;
                state.status = null;
                showToast("配置已保存", "ok");
                refreshStatus();
                render();
            })
            .catch(function (e) {
                showToast("保存失败: " + (e && e.message ? e.message : e), "err");
            });
    }

    function refreshTab() {
        if (state.tab === "corpus") refreshCorpus();
        else if (state.tab === "logs") refreshLogs();
        else if (state.tab === "config") refreshConfig();
        else refreshStatus();
    }

    function runLearn() {
        apiPost("crawl/run", {})
            .then(function () {
                state.status && (state.status.running = true);
                render();
            })
            .catch(function (e) {
                showToast("启动失败: " + (e && e.message ? e.message : e), "err");
            });
    }

    function startQrLogin() {
        stopQrPoll();
        state.qr = { sid: "", data: "", url: "", state: "loading", message: "正在获取二维码…" };
        render();
        apiPost("login/qr/start", {})
            .then(function (r) {
                state.qr.sid = r.sid;
                state.qr.data = r.qrcode || "";
                state.qr.url = r.qrcode_url || "";
                state.qr.state = "polling";
                state.qr.message = "请使用百度 App 扫描二维码";
                render();
                startQrPoll();
            })
            .catch(function (e) {
                state.qr.state = "error";
                state.qr.message = "获取二维码失败: " + (e && e.message ? e.message : e);
                render();
            });
    }

    function startQrPoll() {
        stopQrPoll();
        state.pollTimer = setInterval(function () {
            if (!state.qr.sid || state.qr.state === "success" || state.qr.state === "error") {
                stopQrPoll();
                return;
            }
            apiGet("login/qr/poll", { sid: state.qr.sid })
                .then(function (r) {
                    if (!r) return;
                    state.qr.state = r.state;
                    if (r.state === "success") {
                        state.qr.message = r.message || "登录成功";
                        stopQrPoll();
                        refreshStatus();
                    } else if (r.state === "scanned") {
                        state.qr.message = r.message || "已扫码，请在手机上确认登录";
                    } else if (r.state === "error") {
                        state.qr.message = r.message || "登录失败";
                        stopQrPoll();
                    } else {
                        state.qr.message = r.message || "等待扫码…";
                    }
                    renderAccountOnly();
                })
                .catch(function () {});
        }, 1500);
    }

    function renderAccountOnly() {
        if (state.tab === "account") render();
    }

    function stopQrPoll() {
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
    }

    function manualBduss(bduss) {
        if (!bduss) {
            showToast("请先粘贴 BDUSS", "err");
            return;
        }
        apiPost("login/bduss", { bduss: bduss })
            .then(function () {
                showToast("登录成功", "ok");
                refreshStatus();
            })
            .catch(function (e) {
                showToast("登录失败: " + (e && e.message ? e.message : e), "err");
            });
    }

    function resetCorpus() {
        if (!state.resetArmed) {
            state.resetArmed = true;
            render();
            setTimeout(function () {
                state.resetArmed = false;
                if (state.tab === "corpus") render();
            }, 4000);
            return;
        }
        state.resetArmed = false;
        apiPost("corpus/reset", {})
            .then(function () {
                showToast("语料已清空", "ok");
                refreshCorpus();
                refreshStatus();
            })
            .catch(function (e) {
                showToast("清空失败: " + (e && e.message ? e.message : e), "err");
                render();
            });
    }

    function doLogout() {
        if (!state.logoutArmed) {
            // 页内二次确认（iframe 沙箱没有 allow-modals，原生 confirm 不可用）
            state.logoutArmed = true;
            render();
            setTimeout(function () {
                state.logoutArmed = false;
                renderAccountOnly();
            }, 4000);
            return;
        }
        state.logoutArmed = false;
        apiPost("login/logout", {})
            .then(function () {
                showToast("已退出登录", "ok");
                refreshStatus();
            })
            .catch(function (e) {
                showToast("退出失败: " + (e && e.message ? e.message : e), "err");
                renderAccountOnly();
            });
    }

    /* ------------------------------------------------------------------ boot */
    document.addEventListener("DOMContentLoaded", function () {
        appEl = document.getElementById("app");
        waitBridge(8000).then(function (b) {
            bridge = b || null;
            if (bridge && bridge.registerView) {
                try {
                    bridge.registerView({
                        props: {
                            title: "吧味学习机",
                            description: "刷百度贴吧指定吧的帖子并学习语言风格",
                            author: "Chescy20",
                            version: "1.0.0",
                            tags: ["贴吧", "语言风格", "学习"],
                        },
                    });
                } catch (e) {
                    /* 忽略 */
                }
            }
            if (!bridge || !bridge.apiGet) {
                appEl.innerHTML = "";
                appEl.appendChild(
                    h("div", { class: "ts-empty" }, "未能获取 AstrBot 视图桥接（bridge），请确认插件在 WebUI 内打开。")
                );
                return;
            }
            // 初次加载 + 轮询（仅在概览页自动刷新，避免重渲染打断账号页输入）
            refreshStatus();
            state.refreshTimer = setInterval(function () {
                if (state.autoRefresh && state.tab === "overview") refreshStatus();
            }, 8000);
        });
    });
})();
