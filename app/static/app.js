const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { raw: text }; }
  if (!res.ok) {
    const msg = body?.error?.message || body?.detail || res.statusText;
    const code = body?.error?.code ? `[${body.error.code}] ` : "";
    throw new Error(`${res.status} ${code}${msg}`);
  }
  return body;
}

function showResult(value, ok = true) {
  const el = $("result");
  el.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  el.className = "result " + (ok ? "ok" : "err");
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("public-free").textContent = s.public_free_blocks;
    $("reserved-free").textContent = s.reserved_blocks;
    $("live-blocks").textContent = s.live_blocks;
    $("total-blocks").textContent = s.total_blocks;
    const snap = s.snapshot;
    const info = $("snapshot-info");
    if (snap) {
      info.classList.add("active");
      info.innerHTML =
        `快照存在（只读）· 预留 ${snap.reserved_blocks} 块，` +
        `已消耗 ${snap.reservation_used_blocks} 块，` +
        `剩余 ${snap.reservation_remaining_blocks} 块`;
    } else {
      info.classList.remove("active");
      info.textContent = "当前没有快照。";
    }
    $("status-note").textContent =
      `公共余量 = 总容量 ${s.total_blocks} − 已占用 ${s.live_blocks} − 快照预留 ${s.reserved_blocks}` +
      ` = ${s.public_free_blocks} 块（每块 ${s.block_size} 字节）。` +
      `系统盘已用 ${s.disks.sys.allocated_blocks} 块，数据盘已用 ${s.disks.data.allocated_blocks} 块。`;
  } catch (err) {
    $("status-note").textContent = `读取状态失败：${err.message}`;
  }
}

async function readBlock() {
  const disk = $("disk-select").value;
  const lba = $("lba-input").value;
  try {
    const b = await api(`/api/disks/${disk}/blocks/${lba}`);
    showResult(
      `磁盘 ${b.disk} 块 ${b.lba} → 物理块 ${b.pblock_id}（${b.size} 字节）\n` +
      `共享标记：${b.shared_with_snapshot ? "是（只读共享）" : "否"}\n` +
      `文本：${b.text}`
    );
  } catch (err) {
    showResult(err.message, false);
  }
}

async function writeBlock() {
  const disk = $("disk-select").value;
  const lba = $("lba-input").value;
  const text = $("text-input").value;
  if (disk === "snapshot") {
    showResult("快照是只读的，不能写入。", false);
    return;
  }
  try {
    const r = await api(`/api/disks/${disk}/blocks/${lba}`, {
      method: "PUT",
      body: JSON.stringify({ text }),
    });
    showResult(
      `写入成功：${r.disk} 块 ${r.lba} → 物理块 ${r.pblock_id}\n` +
      `类型：${r.action}，计费：${r.charged}\n` +
      `公共余量 ${r.public_free_blocks} 块，预留余量 ${r.reservation_remaining_blocks} 块`
    );
    refreshStatus();
  } catch (err) {
    showResult(`写入失败：${err.message}`, false);
    refreshStatus();
  }
}

async function createSnapshot() {
  const reserved = parseInt($("reserve-input").value, 10);
  try {
    const r = await api("/api/snapshot", {
      method: "POST",
      body: JSON.stringify({ reserved_blocks: reserved }),
    });
    showResult(
      `快照创建成功：共享系统盘 ${r.shared_blocks} 个块，` +
      `预留 ${r.snapshot.reserved_blocks} 块，公共余量剩余 ${r.public_free_blocks} 块`
    );
    refreshStatus();
  } catch (err) {
    showResult(`创建快照失败：${err.message}`, false);
    refreshStatus();
  }
}

async function deleteSnapshot() {
  try {
    const r = await api("/api/snapshot", { method: "DELETE" });
    showResult(
      `快照已删除：释放仅快照引用的 ${r.freed_blocks} 个块，` +
      `退回未用预留 ${r.released_reservation_blocks} 块，` +
      `公共余量现为 ${r.public_free_blocks} 块`
    );
    refreshStatus();
  } catch (err) {
    showResult(`删除快照失败：${err.message}`, false);
    refreshStatus();
  }
}

async function listBlocks(disk) {
  try {
    const r = await api(`/api/disks/${disk}/blocks?limit=512`);
    const tbody = $("blocks-table").querySelector("tbody");
    tbody.innerHTML = "";
    if (!r.blocks.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted">没有已分配的块。</td></tr>`;
      return;
    }
    for (const b of r.blocks) {
      const tr = document.createElement("tr");
      if (b.shared_with_snapshot) tr.classList.add("shared");
      tr.innerHTML =
        `<td>${r.disk}</td>` +
        `<td class="mono">${b.lba}</td>` +
        `<td class="mono">${b.pblock_id}</td>` +
        `<td>${b.size}</td>` +
        `<td>${escapeHtml(b.preview)}</td>` +
        `<td>${b.shared_with_snapshot
          ? '<span class="badge shared">与快照共享</span>'
          : '<span class="badge">独占</span>'}</td>`;
      tbody.appendChild(tr);
    }
  } catch (err) {
    showResult(err.message, false);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

async function runDiagnostics() {
  try {
    const d = await api("/api/diagnostics");
    const el = $("diag-result");
    el.textContent = JSON.stringify(d, null, 2);
    el.className = "result " + (d.ok ? "ok" : "err");
  } catch (err) {
    $("diag-result").textContent = err.message;
    $("diag-result").className = "result err";
  }
}

$("read-btn").addEventListener("click", readBlock);
$("write-btn").addEventListener("click", writeBlock);
$("create-snapshot").addEventListener("click", createSnapshot);
$("delete-snapshot").addEventListener("click", deleteSnapshot);
$("list-sys").addEventListener("click", () => listBlocks("sys"));
$("list-data").addEventListener("click", () => listBlocks("data"));
$("list-snapshot").addEventListener("click", () => listBlocks("snapshot"));
$("diag-btn").addEventListener("click", runDiagnostics);

refreshStatus();
