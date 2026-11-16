(() => {
  const main = document.getElementById("main");
  let view = "policies";

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.detail || data.error || "请求失败");
      err.payload = data;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function esc(v) {
    return String(v ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行"
        ? "warn"
        : state === "已完成" || state === "已归档" || state === "已销毁"
          ? "ok"
          : state === "已失败" || state === "裁撤中"
            ? "bad"
            : "";
    return `<span class="tag ${cls}">${esc(state)}</span>`;
  }

  function alignTag(aligned) {
    if (aligned === 1) return `<span class="tag ok">对齐</span>`;
    if (aligned === 0) return `<span class="tag bad">不对齐</span>`;
    return `<span class="tag">—</span>`;
  }

  async function renderPolicies() {
    const [policies, deps] = await Promise.all([
      api("/api/policies"),
      api("/api/dependencies"),
    ]);
    // 策略详情：按主题域聚合依赖对象，呈现跨域冻结面
    const domainDeps = {};
    deps.items.forEach((d) => {
      (domainDeps[d.upstream_domain] ||= []).push(d);
      (domainDeps[d.downstream_domain] ||= []).push(d);
    });
    main.innerHTML = `
      <section class="panel">
        <h2>现行留存策略</h2>
        <p class="sub">每个主题域一条现行版本：保留天数与到期动作（归档或销毁）。下方展开各域的跨域依赖处置面。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>保留天</th><th>到期动作</th><th>版本</th><th>关联依赖</th></tr></thead>
          <tbody>
            ${policies.items
              .map((p) => {
                const related = domainDeps[p.domain_code] || [];
                return `<tr>
              <td>${esc(p.domain_name)}</td>
              <td class="mono">${esc(p.domain_code)}</td>
              <td>${p.keep_days}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td>v${p.version}</td>
              <td>${related.length} 条</td>
            </tr>
            <tr class="detail-row">
              <td colspan="6">
                <div class="dep-face">
                  <span class="face-title">跨域依赖处置面（${esc(p.domain_code)}）</span>
                  ${
                    related.length
                      ? `<ul class="dep-list">${related
                          .map(
                            (d) => `<li>
                        <span class="mono">${esc(d.downstream_domain)}/${esc(d.downstream_biz_date)}</span>
                        <em>引用</em>
                        <span class="mono">${esc(d.upstream_domain)}/${esc(d.upstream_biz_date)}</span>
                        ${tag(d.upstream_state)}${tag(d.downstream_state)}
                      </li>`
                          )
                          .join("")}</ul>`
                      : `<span class="empty">该域暂无跨域依赖，销毁不受依赖冻结约束</span>`
                  }
                </div>
              </td>
            </tr>`;
              })
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderPartitions() {
    const data = await api("/api/partitions");
    main.innerHTML = `
      <section class="panel">
        <h2>实物分区 · 分区队列</h2>
        <p class="sub">在线 / 已归档 / 已销毁 / 裁撤中。被在线下游引用的上游分区，销毁会被整单拒绝（见依赖列）。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>归档地址</th><th>下游引用</th><th>上游依赖</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td class="mono">${esc(p.domain_code)}</td>
              <td class="mono">${esc(p.biz_date)}</td>
              <td>${p.row_count}</td>
              <td>${tag(p.storage_state)}</td>
              <td class="mono">${esc(p.archive_uri || "—")}</td>
              <td>${
                p.downstream_ref_count > 0
                  ? `<span class="tag bad">被引 ${p.downstream_ref_count}</span>`
                  : "—"
              }</td>
              <td>${p.upstream_ref_count > 0 ? `${p.upstream_ref_count} 条` : "—"}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderDependencies() {
    const [deps, parts] = await Promise.all([
      api("/api/dependencies"),
      api("/api/partitions"),
    ]);
    const online = parts.items.filter((p) => p.storage_state !== "已销毁");
    main.innerHTML = `
      <section class="panel">
        <h2>跨域依赖卡片</h2>
        <p class="sub">登记「下游分区 → 上游分区」的依赖对象。销毁上游时若仍被在线下游引用，整单拒绝并落拒绝原因；断链即拦。</p>
        <div class="toolbar">
          <label class="field">下游分区
            <select id="dep-down">
              ${online.map((p) => `<option value="${p.id}">${esc(p.domain_code)}/${esc(p.biz_date)}（${esc(p.storage_state)}）</option>`).join("")}
            </select>
          </label>
          <label class="field">上游分区
            <select id="dep-up">
              ${online.map((p) => `<option value="${p.id}">${esc(p.domain_code)}/${esc(p.biz_date)}（${esc(p.storage_state)}）</option>`).join("")}
            </select>
          </label>
          <button type="button" id="btn-add-dep">登记依赖</button>
        </div>
        <p id="dep-msg" class="err" hidden></p>
        <table>
          <thead><tr><th>编号</th><th>下游分区</th><th>下游态</th><th></th><th>上游分区</th><th>上游态</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            ${
              deps.items.length
                ? deps.items
                    .map(
                      (d) => `<tr>
                <td>${d.id}</td>
                <td class="mono">${esc(d.downstream_domain)}/${esc(d.downstream_biz_date)}</td>
                <td>${tag(d.downstream_state)}</td>
                <td class="arrow">→</td>
                <td class="mono">${esc(d.upstream_domain)}/${esc(d.upstream_biz_date)}</td>
                <td>${tag(d.upstream_state)}</td>
                <td>${d.active ? `<span class="tag warn">有效</span>` : `<span class="tag">已解除</span>`}</td>
                <td>${
                  d.active
                    ? `<button type="button" class="secondary" data-rel="${d.id}">解除</button>`
                    : "—"
                }</td>
              </tr>`
                    )
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无跨域依赖，请先登记</td></tr>`
            }
          </tbody>
        </table>
      </section>`;

    const msg = document.getElementById("dep-msg");
    document.getElementById("btn-add-dep").onclick = async () => {
      msg.hidden = true;
      try {
        await api("/api/dependencies", {
          method: "POST",
          body: JSON.stringify({
            downstream_partition_id: Number(document.getElementById("dep-down").value),
            upstream_partition_id: Number(document.getElementById("dep-up").value),
          }),
        });
        await renderDependencies();
      } catch (e) {
        msg.hidden = false;
        msg.textContent = e.message;
      }
    };
    main.querySelectorAll("[data-rel]").forEach((btn) => {
      btn.onclick = async () => {
        await api(`/api/dependencies/${btn.dataset.rel}/deactivate`, { method: "POST" });
        await renderDependencies();
      };
    });
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-16" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <section class="panel">
        <h2>扫描任务</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>状态</th><th>到期数</th><th>说明</th></tr></thead>
          <tbody>
            ${
              jobs.items.length
                ? jobs.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${esc(j.as_of_date)}</td>
              <td>${tag(j.status)}</td>
              <td>${j.expired_count}</td>
              <td>${esc(j.message)}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="5" class="empty">尚无扫描任务</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      await api("/api/scan-jobs", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderScan();
    };
  }

  async function renderJudgments() {
    const data = await api("/api/judgments");
    main.innerHTML = `
      <section class="panel">
        <h2>裁决清单 · 依赖守卫队列</h2>
        <p class="sub">销毁前同源校验依赖集合与上游分区态；不对齐则整单拒绝（只读卡片展示拒绝原因，解除依赖后可重试）。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>策略</th><th>动作</th><th>状态</th><th>依赖对齐</th><th>拒绝原因 / 操作</th></tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map((j) => {
                      const blocked = j.dep_aligned === 0 && j.status === "待执行";
                      return `<tr class="${blocked ? "blocked-row" : ""}">
              <td>${j.id}</td>
              <td class="mono">${esc(j.domain_code)}</td>
              <td class="mono">${esc(j.biz_date)}</td>
              <td>v${j.policy_version} / ${j.keep_days}天</td>
              <td>${esc(j.decided_action)}</td>
              <td>${tag(j.status)}</td>
              <td>${alignTag(j.dep_aligned)}</td>
              <td>
                ${
                  blocked
                    ? `<div class="reject-card">
                        <span class="tag bad">整单拒绝 · 只读冻结</span>
                        <p class="reject-reason">${esc(j.dep_block_reason || j.fail_reason)}</p>
                        <button type="button" data-exec="${j.id}">依赖解除后重试</button>
                      </div>`
                    : j.status === "待执行" || j.status === "已失败"
                      ? `<button type="button" data-exec="${j.id}">执行</button>`
                      : j.fail_reason
                        ? esc(j.fail_reason)
                        : "—"
                }
              </td>
            </tr>`;
                    })
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无裁决，请先跑到期扫描</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/judgments/${btn.dataset.exec}/execute`, { method: "POST" });
        } catch (e) {
          // 整单拒绝：409 已带拒绝原因，刷新队列让只读卡片呈现
          if (e.status !== 409) throw e;
        }
        await renderJudgments();
      };
    });
  }

  async function renderReconcile() {
    const reports = await api("/api/reconcile-reports");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-16" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
      </div>
      <section class="panel">
        <h2>对账报告</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th><th>待裁决</th><th>错位</th><th>断链依赖</th><th>摘要</th></tr></thead>
          <tbody>
            ${
              reports.items.length
                ? reports.items
                    .map(
                      (r) => `<tr>
              <td>${r.id}</td>
              <td class="mono">${esc(r.as_of_date)}</td>
              <td>${r.online_count}</td>
              <td>${r.archived_count}</td>
              <td>${r.destroyed_count}</td>
              <td>${r.pending_judgment_count}</td>
              <td>${r.mismatch_count}</td>
              <td>${
                r.broken_dependency_count > 0
                  ? `<span class="tag bad">${r.broken_dependency_count}</span>`
                  : "0"
              }</td>
              <td>${esc(r.summary)}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="9" class="empty">尚无对账报告</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-rec").onclick = async () => {
      const as_of_date = document.getElementById("as-of-r").value.trim();
      await api("/api/reconcile-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderReconcile();
    };
  }

  const renderers = {
    policies: renderPolicies,
    partitions: renderPartitions,
    dependencies: renderDependencies,
    scan: renderScan,
    judgments: renderJudgments,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    await renderers[name]();
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
  });

  show(view);
})();
