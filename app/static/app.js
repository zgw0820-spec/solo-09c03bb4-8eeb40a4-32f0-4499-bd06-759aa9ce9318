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
    $("packed-pairs").textContent = s.packed_pairs;
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
      `系统盘已用 ${s.disks.sys.allocated_blocks} 块，数据盘已用 ${s.disks.data.allocated_blocks} 块` +
      `（其中 ${s.disks.data.packed_blocks} 块处于合装，共 ${s.packed_pairs} 对）。`;
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
      `合装：${b.packed ? `是（第 ${b.pack_half} 半，压缩后 ${b.compressed_size} 字节）` : "否"}\n` +
      `文本：${b.text}`
    );
  } catch (err) {
    showResult(err.message, false);
  }
}

const ACTION_LABELS = {
  first_write: "首次写入（占公共新槽）",
  overwrite_private: "覆写私有块（原地，不占新槽）",
  overwrite_shared: "覆写共享块（写时复制）",
  overwrite_packed: "覆写合装块（仍可合装，留在原槽）",
  overwrite_packed_split: "覆写合装块（压不回 2 KiB，拆到公共新槽）",
};

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
      `类型：${ACTION_LABELS[r.action] || r.action}，计费：${r.charged}\n` +
      `公共余量 ${r.public_free_blocks} 块，预留余量 ${r.reservation_remaining_blocks} 块`
    );
    refreshStatus();
  } catch (err) {
    showResult(`写入失败：${err.message}`, false);
    refreshStatus();
  }
}

async function packBlocks() {
  const start = parseInt($("pack-start").value, 10);
  const count = parseInt($("pack-count").value, 10);
  try {
    const r = await api("/api/disks/data/pack", {
      method: "POST",
      body: JSON.stringify({ start_lba: start, count }),
    });
    const sk = r.skipped;
    const lines = [
      `合装完成：成功 ${r.packed_pairs} 对，实际释放 ${r.freed_slots} 个槽（已归还公共余量）`,
      `公共余量现为 ${r.public_free_blocks} 块，已占用 ${r.live_blocks} 块`,
    ];
    if (r.pairs.length) {
      lines.push("合装明细：" + r.pairs.map(
        (p) => `块${p.lbas[0]}+块${p.lbas[1]}→槽${p.pblock_id}` +
               `（${p.compressed_sizes[0]}B+${p.compressed_sizes[1]}B）`
      ).join("；"));
    }
    const skips = [];
    if (sk.unallocated) skips.push(`未分配 ${sk.unallocated} 对`);
    if (sk.already_packed) skips.push(`已合装 ${sk.already_packed} 对`);
    if (sk.incompressible) skips.push(`压缩后超 2 KiB ${sk.incompressible} 对`);
    if (sk.shared) skips.push(`受保护 ${sk.shared} 对`);
    if (skips.length) lines.push(`跳过：${skips.join("，")}`);
    showResult(lines.join("\n"));
    refreshStatus();
  } catch (err) {
    showResult(`合装失败：${err.message}`, false);
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
      if (b.packed) tr.classList.add("packed");
      const badges = [];
      if (b.shared_with_snapshot) {
        badges.push('<span class="badge shared">与快照共享</span>');
      }
      if (b.packed) {
        badges.push(
          `<span class="badge packed">合装·半${b.pack_half}·` +
          `${b.compressed_size}B·配对块${b.pack_partner_lba}</span>`
        );
      }
      if (!badges.length) badges.push('<span class="badge">独占</span>');
      tr.innerHTML =
        `<td>${r.disk}</td>` +
        `<td class="mono">${b.lba}</td>` +
        `<td class="mono">${b.pblock_id}</td>` +
        `<td>${b.size}</td>` +
        `<td>${escapeHtml(b.preview)}</td>` +
        `<td>${badges.join(" ")}</td>`;
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
$("pack-btn").addEventListener("click", packBlocks);
$("create-snapshot").addEventListener("click", createSnapshot);
$("delete-snapshot").addEventListener("click", deleteSnapshot);
$("list-sys").addEventListener("click", () => listBlocks("sys"));
$("list-data").addEventListener("click", () => listBlocks("data"));
$("list-snapshot").addEventListener("click", () => listBlocks("snapshot"));
$("diag-btn").addEventListener("click", runDiagnostics);

refreshStatus();
